"""API 测试的公共环境：进程内 SQLite + 临时目录 + TestClient。

每个 context 用独立子目录：同一用例里可能要造两个 context
（比如测上传上限），共用 vectors.db 会互相抢锁。
"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.parser.pdf_parser import PdfParser
from app.adapters.queue.sqlite_queue import SqliteTaskQueue
from app.adapters.repo.db import Database
from app.adapters.repo.sqlite_repo import SqliteResumeRepository, SqliteRunRepository
from app.adapters.vector_store.sqlite_vec_adapter import SqliteVecStore
from app.api.deps import AppContext, get_context
from app.config.settings import Settings
from app.domain.policies import load_scoring_config
from app.main import create_app

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring.yaml"
PDF = b"%PDF-1.4\n% test pdf\n"


def build_context(root: Path, **overrides) -> AppContext:
    base = root / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    db = Database(":memory:")
    db.initialize()
    cfg = Settings(
        _env_file=None,
        llm_api_key="test-key",
        db_path=base / "app.db",
        blob_dir=base / "blobs",
        **overrides,
    )
    return AppContext(
        settings=cfg,
        db=db,
        resumes=SqliteResumeRepository(db),
        runs=SqliteRunRepository(db),
        queue=SqliteTaskQueue(db),
        parser=PdfParser(),
        vectors=SqliteVecStore(base / "vectors.db", 8),
        scoring=load_scoring_config(CONFIG_PATH),
    )


@pytest.fixture
def make_context(tmp_path):
    created: list[AppContext] = []

    def _make(**overrides) -> AppContext:
        ctx = build_context(tmp_path, **overrides)
        created.append(ctx)
        return ctx

    yield _make

    for ctx in created:
        ctx.db.close()
        ctx.vectors.close()


@pytest.fixture
def ctx(make_context):
    return make_context()


@pytest.fixture
def client(ctx):
    app = create_app()
    app.dependency_overrides[get_context] = lambda: ctx
    with TestClient(app) as c:
        yield c
