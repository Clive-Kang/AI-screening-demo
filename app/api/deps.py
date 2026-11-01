"""依赖注入：把 adapters 接到路由上。

为什么用 `Depends` 而不是参考项目的模块级单例
---------------------------------------------
参考项目在 import 期就构造 LLM 客户端，缺 key 连 `import app` 都失败。
这里两件事被拆开：

    import 本模块        → 什么都不做（不开数据库、不建客户端）
    第一次收到请求时     → get_context() 才真正构造

测试里用 `app.dependency_overrides[get_context] = 假上下文` 就能整条换掉，
不用碰环境变量、不用建真文件。
"""

from dataclasses import dataclass
from functools import lru_cache

from app.adapters.parser.pdf_parser import PdfParser
from app.adapters.queue.sqlite_queue import SqliteTaskQueue
from app.adapters.repo.db import Database
from app.adapters.repo.sqlite_repo import SqliteResumeRepository, SqliteRunRepository
from app.adapters.vector_store.sqlite_vec_adapter import SqliteVecStore
from app.config.settings import ROOT_DIR, Settings, settings
from app.domain.policies import ScoringConfig, load_scoring_config
from app.ports.document_parser import DocumentParserPort
from app.ports.repository import ResumeRepository, RunRepository
from app.ports.task_queue import TaskQueuePort
from app.ports.vector_store import VectorStorePort

SCORING_CONFIG = ROOT_DIR / "config" / "scoring.yaml"


@dataclass(frozen=True)
class AppContext:
    """一次请求需要的全部依赖。

    打包成一个对象而不是五六个 Depends 参数：
    路由签名保持干净，测试也只需要覆盖这一个依赖。
    """

    settings: Settings
    db: Database
    resumes: ResumeRepository
    runs: RunRepository
    queue: TaskQueuePort
    parser: DocumentParserPort
    # 删除简历时要连带删向量；这是 API 唯一需要向量库的地方
    vectors: VectorStorePort
    scoring: ScoringConfig


@lru_cache(maxsize=1)
def get_context() -> AppContext:
    """进程内只构造一次。

    `lru_cache` 在这里是"单例"的显式写法 —— 但它发生在**第一次调用时**，
    而不是 import 时。
    """
    cfg = settings()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.blob_dir.mkdir(parents=True, exist_ok=True)

    db = Database(cfg.db_path)
    db.initialize()

    return AppContext(
        settings=cfg,
        db=db,
        resumes=SqliteResumeRepository(db),
        runs=SqliteRunRepository(db),
        queue=SqliteTaskQueue(db),
        parser=PdfParser(),
        vectors=SqliteVecStore(cfg.vectors_path, cfg.embedding_dimensions),
        scoring=load_scoring_config(SCORING_CONFIG),
    )
