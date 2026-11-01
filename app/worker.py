"""后台 worker：独立进程消费任务队列。

启动方式（必须是模块方式，否则 app 包导入失败，见 CLAUDE.md §4 第 1 条）：

    D:/conda_envs/ai_resume_screening/python.exe -m app.worker

它消费两类任务：

    parse_resume   解析 PDF → LLM 抽取 → 落库 → 嵌入 → 建索引（IngestionService）
    screen_run     召回 → 硬过滤 → 评分 → Top-N 报告（ScreeningService）

为什么要有独立进程
------------------
一份简历的解析 + 抽取 + 嵌入要 10-30 秒，10 份就是 2-5 分钟
（ARCHITECTURE.md §5.5）。放在 HTTP 请求里同步跑，浏览器早就超时了。
API 只做"登记 + 入队 + 返回 202"，重活全在这里。

这个文件是**组合根**（composition root）之一：只有 main.py 和 worker.py
可以 import adapters。换 LLM 供应商、换向量库，改的是这里的装配，不是业务代码。

失败怎么分类
------------
    永久失败（PDF 损坏、任务参数缺失） → nack(retry=False)，直接进死信
    瞬态失败（LLM 超时、Ollama 没启动） → nack(retry=True)，回到队列等下一轮

分不清的异常按"瞬态"处理：最多重试 max_attempts 次后自动进死信，
不会把 worker 卡死，也不会让任务凭空消失。
"""

import logging
import signal
import threading

from app.adapters.embedding.openai_compat import OpenAICompatibleEmbedding
from app.adapters.llm.openai_compat import OpenAICompatibleLLM
from app.adapters.parser.pdf_parser import PdfParser
from app.adapters.queue.sqlite_queue import SqliteTaskQueue
from app.adapters.repo.db import Database
from app.adapters.repo.sqlite_repo import SqliteResumeRepository, SqliteRunRepository
from app.adapters.vector_store.sqlite_vec_adapter import SqliteVecStore
from app.config.settings import ROOT_DIR, Settings, settings
from app.domain.policies import load_scoring_config
from app.ports.document_parser import DocumentParseError
from app.ports.embedding import EmbeddingError
from app.ports.llm import LLMError
from app.ports.task_queue import Task, TaskKind, TaskQueuePort
from app.services.ingestion import IngestionService
from app.services.screening import ScreeningService

log = logging.getLogger("worker")

SCORING_CONFIG = ROOT_DIR / "config" / "scoring.yaml"


def _is_transient(exc: Exception) -> bool:
    """这个异常值不值得再试一次？

    DocumentParseError 必须排在前面：它表示"这个文件永远解析不了"，
    重试一万次结果一样，应该立刻进死信让人去看文件。
    """
    if isinstance(exc, DocumentParseError):
        return False
    if isinstance(exc, (LLMError, EmbeddingError)):
        return True
    if isinstance(exc, (KeyError, ValueError, TypeError)):
        return False        # 参数缺失 / 类型不对：重试不会变好
    return True             # 未知异常：保守重试，超上限自然进死信


class Worker:
    """队列消费循环。依赖全是端口，测试里可注入假实现。"""

    def __init__(
        self,
        *,
        queue: TaskQueuePort,
        ingestion: IngestionService,
        screening: ScreeningService,
        dequeue_timeout: float = 1.0,
    ) -> None:
        self._queue = queue
        self._ingestion = ingestion
        self._screening = screening
        self._dequeue_timeout = dequeue_timeout

    # ------------------------------------------------------------

    def process_one(self, timeout: float | None = None) -> bool:
        """取一条任务并处理。返回 False 表示这次没取到任务。

        拆成单步是为了可测：测试直接调 process_one，不用起线程。
        """
        task = self._queue.dequeue(self._dequeue_timeout if timeout is None else timeout)
        if task is None:
            return False

        try:
            self._dispatch(task)
        except Exception as exc:
            retry = _is_transient(exc)
            log.warning(
                "任务 %s (%s) 第 %d 次失败：%s；%s",
                task.id, task.kind.value, task.attempts, exc,
                "将重试" if retry else "进死信",
            )
            self._queue.nack(task.id, retry=retry, error=str(exc))
        else:
            self._queue.ack(task.id)
            log.info("任务 %s (%s) 完成", task.id, task.kind.value)

        return True

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """一直消费直到 stop 被置位（Ctrl+C / SIGTERM）。"""
        stop = stop or threading.Event()
        log.info("worker 启动，等待任务")

        while not stop.is_set():
            try:
                self.process_one()
            except Exception:
                # 队列本身出问题（database is locked 之类）不该让循环直接死掉
                log.exception("worker 主循环异常，1 秒后重试")
                stop.wait(1.0)

        log.info("worker 退出")

    # ------------------------------------------------------------

    def _dispatch(self, task: Task) -> None:
        """按任务类型分派。

        payload 的键就是 API 层入队时写进去的键，两边必须对齐：
            parse_resume  resume_id / filename / blob_path / content_sha256
            screen_run    run_id / query_text / profile
        """
        params = task.params

        if task.kind is TaskKind.PARSE_RESUME:
            self._ingestion.ingest(
                filename=params["filename"],
                blob_path=params["blob_path"],
                content_sha256=params["content_sha256"],
                resume_id=params.get("resume_id"),
            )
            return

        if task.kind is TaskKind.SCREEN_RUN:
            self._screening.run(
                params["query_text"],
                profile=params.get("profile") or None,
                run_id=params.get("run_id") or None,
            )
            return

        raise ValueError(f"未知任务类型：{task.kind}")


# ============================================================
# 组合根：构造真实依赖
# ============================================================

def build_worker(cfg: Settings) -> Worker:
    """把 settings 里的配置接成一条可跑的流水线。"""
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    db = Database(cfg.db_path)
    db.initialize()

    resumes = SqliteResumeRepository(db)
    runs = SqliteRunRepository(db)
    queue = SqliteTaskQueue(db)

    llm = OpenAICompatibleLLM(cfg)
    embedder = OpenAICompatibleEmbedding(cfg)
    # 向量索引放单独的文件：它是可重建的派生数据，不该和真源混在一起
    vectors = SqliteVecStore(cfg.vectors_path, cfg.embedding_dimensions)

    config = load_scoring_config(SCORING_CONFIG)

    return Worker(
        queue=queue,
        ingestion=IngestionService(
            repo=resumes,
            parser=PdfParser(),
            llm=llm,
            embedder=embedder,
            vectors=vectors,
        ),
        screening=ScreeningService(
            resumes=resumes,
            runs=runs,
            vectors=vectors,
            embedder=embedder,
            llm=llm,
            config=config,
        ),
    )


def main() -> int:
    cfg = settings()
    logging.basicConfig(
        level=cfg.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    worker = build_worker(cfg)

    stop = threading.Event()

    def _handle(signum, _frame):
        log.info("收到信号 %s，处理完当前任务后退出", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle)

    worker.run_forever(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
