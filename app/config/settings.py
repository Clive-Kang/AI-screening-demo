"""启动配置：从环境变量 / .env 读，读不到就在启动时炸掉。

与参考项目的区别（ARCHITECTURE.md 附录 A 缺陷 4）
--------------------------------------------------
参考项目的 config.py 是手写类 + 导入期单例：import 的那一刻就构造 LLM 客户端，
缺 key 直接让整个包 import 失败，测试连收集用例都做不到。

这里做成两段：
    1. 定义（本文件）只描述字段与默认值 —— import 时不读环境变量
    2. 构造（settings()）在组合根显式调用 —— 测试想造几个就造几个

字段命名规则
------------
pydantic-settings 默认把 `llm_api_key` 映射到环境变量 `LLM_API_KEY`，
所以 .env 里写大写下划线即可，不用任何额外配置。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：app/config/settings.py → app/config → app → 根
ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        # .env 里常有参考项目遗留的变量，不认识就跳过，不要报错
        extra="ignore",
    )

    # ---- LLM（对话 / 结构化抽取）----
    llm_api_key: SecretStr                      # 必填：没有它服务不该启动
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2

    # ---- Embedding（本地 Ollama）----
    embedding_api_key: SecretStr = SecretStr("ollama")
    # 必须是 http:// —— Ollama 的 OpenAI 兼容层不走 TLS（CLAUDE.md §4 第 3 条）
    embedding_base_url: str = "http://localhost:11434/v1"
    embedding_model: str = "bge-m3"
    embedding_dimensions: int = Field(default=1024, gt=0)

    # ---- 存储 ----
    db_path: Path = ROOT_DIR / "data" / "app.db"
    blob_dir: Path = ROOT_DIR / "data" / "blobs"   # 原始 PDF 落盘处
    max_upload_mb: int = Field(default=10, gt=0, le=100)

    # ---- 服务 ----
    log_level: str = "INFO"
    allowed_origins: str = "*"                  # 逗号分隔；* 仅用于本地开发

    # ---- 筛选 ----
    analyze_top_n: int = Field(default=10, gt=0)   # 只给前 N 名生成 LLM 报告

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def vectors_path(self) -> Path:
        """向量索引文件：与真源分开放，因为它是可重建的派生数据。"""
        return self.db_path.parent / "vectors.db"

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def settings() -> Settings:
    """进程内单例。

    lru_cache 让"读 .env"只发生一次。为什么不在 import 时构造？
        那样缺 key 会让 import 失败 —— 正是参考项目的毛病。
    测试时想要不同配置：直接 `Settings(_env_file=None, llm_api_key="x")`，
    绕开这个缓存。
    """
    return Settings()
