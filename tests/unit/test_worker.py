"""Worker 的单测：分派、ack/nack、失败分类。

用假 queue / 假服务，验证"取到什么、调了什么、回执怎么打"，
不碰真实 LLM 与 PDF。
"""

import pytest

from app.adapters.queue import SqliteTaskQueue
from app.adapters.repo.db import Database
from app.ports.document_parser import DocumentParseError
from app.ports.embedding import EmbeddingError
from app.ports.llm import LLMError
from app.ports.task_queue import Task, TaskKind
from app.worker import Worker, _is_transient


# ============================================================
# 假实现
# ============================================================

class FakeQueue:
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])
        self.acked = []
        self.nacked = []

    def enqueue(self, task):
        self.tasks.append(task)

    def dequeue(self, timeout):
        return self.tasks.pop(0) if self.tasks else None

    def ack(self, task_id):
        self.acked.append(task_id)

    def nack(self, task_id, retry, *, error=None):
        self.nacked.append((task_id, retry, error))


class FakeIngestion:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def ingest(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


class FakeScreening:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def run(self, query_text, *, profile=None, run_id=None):
        self.calls.append((query_text, profile, run_id))
        if self.error:
            raise self.error


def _parse_task():
    return Task(
        id="t1",
        kind=TaskKind.PARSE_RESUME,
        payload=(
            ("resume_id", "r1"),
            ("filename", "张三.pdf"),
            ("blob_path", "data/blobs/abc.pdf"),
            ("content_sha256", "abc"),
        ),
    )


def _screen_task():
    return Task(
        id="t2",
        kind=TaskKind.SCREEN_RUN,
        payload=(
            ("run_id", "run1"),
            ("query_text", "招 Python 后端"),
            ("profile", "tech"),
        ),
    )


def _worker(queue, ingestion=None, screening=None):
    return Worker(
        queue=queue,
        ingestion=ingestion or FakeIngestion(),
        screening=screening or FakeScreening(),
    )


# ============================================================
# 分派
# ============================================================

def test_empty_queue_returns_false():
    assert _worker(FakeQueue()).process_one() is False


def test_parse_resume_is_dispatched_and_acked():
    queue = FakeQueue([_parse_task()])
    ingestion = FakeIngestion()

    assert _worker(queue, ingestion=ingestion).process_one() is True

    assert ingestion.calls == [{
        "filename": "张三.pdf",
        "blob_path": "data/blobs/abc.pdf",
        "content_sha256": "abc",
        "resume_id": "r1",
    }]
    assert queue.acked == ["t1"]
    assert queue.nacked == []


def test_screen_run_is_dispatched_with_run_id():
    queue = FakeQueue([_screen_task()])
    screening = FakeScreening()

    _worker(queue, screening=screening).process_one()

    assert screening.calls == [("招 Python 后端", "tech", "run1")]
    assert queue.acked == ["t2"]


def test_empty_profile_is_passed_as_none():
    queue = FakeQueue([Task(id="t3", kind=TaskKind.SCREEN_RUN,
                            payload=(("query_text", "招人"), ("profile", "")))])
    screening = FakeScreening()

    _worker(queue, screening=screening).process_one()

    assert screening.calls == [("招人", None, None)]


# ============================================================
# 失败分类
# ============================================================

def test_parse_error_goes_straight_to_dead_letter():
    queue = FakeQueue([_parse_task()])
    worker = _worker(queue, ingestion=FakeIngestion(DocumentParseError("PDF 损坏")))

    worker.process_one()

    assert queue.acked == []
    (task_id, retry, error), = queue.nacked
    assert task_id == "t1"
    assert retry is False
    assert "PDF 损坏" in error


def test_llm_error_is_retried():
    queue = FakeQueue([_parse_task()])
    worker = _worker(queue, ingestion=FakeIngestion(LLMError("超时")))

    worker.process_one()

    assert queue.nacked[0][1] is True


def test_embedding_error_is_retried():
    queue = FakeQueue([_parse_task()])
    worker = _worker(queue, ingestion=FakeIngestion(EmbeddingError("维度不对")))

    worker.process_one()

    assert queue.nacked[0][1] is True


def test_missing_payload_key_is_permanent():
    """payload 少字段是入队方的 bug，重试不会变好。"""
    queue = FakeQueue([Task(id="t4", kind=TaskKind.PARSE_RESUME,
                            payload=(("filename", "a.pdf"),))])

    _worker(queue).process_one()

    assert queue.nacked[0][1] is False


@pytest.mark.parametrize("exc,retry", [
    (DocumentParseError("坏"), False),
    (LLMError("超时"), True),
    (EmbeddingError("断连"), True),
    (KeyError("x"), False),
    (ValueError("x"), False),
    (RuntimeError("未知"), True),
])
def test_transient_classification(exc, retry):
    assert _is_transient(exc) is retry


# ============================================================
# 与真实队列配合
# ============================================================

def test_real_queue_end_to_end():
    """真 SQLite 队列 + 假服务：处理完一条，队里就少一条。"""
    db = Database(":memory:")
    db.initialize()
    queue = SqliteTaskQueue(db)
    queue.enqueue(_parse_task())

    worker = _worker(queue)
    assert worker.process_one(timeout=0) is True
    assert worker.process_one(timeout=0) is False

    assert db.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0
    db.close()


def test_retry_puts_task_back_for_next_round():
    db = Database(":memory:")
    db.initialize()
    queue = SqliteTaskQueue(db, max_attempts=2)
    queue.enqueue(_parse_task())

    worker = _worker(queue, ingestion=FakeIngestion(LLMError("超时")))
    assert worker.process_one(timeout=0) is True     # 第一次失败 → nack retry

    worker._ingestion = FakeIngestion()              # 第二次恢复正常
    assert worker.process_one(timeout=0) is True
    assert db.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0
    db.close()
