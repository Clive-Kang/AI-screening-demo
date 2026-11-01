"""SqliteTaskQueue 的测试：真实 SQLite + 极短锁时长，不 sleep 等真实超时。"""

import pytest

from app.adapters.queue import SqliteTaskQueue
from app.adapters.repo.db import Database
from app.ports.task_queue import Task, TaskKind, TaskQueuePort


@pytest.fixture
def db():
    d = Database(":memory:")
    d.initialize()
    yield d
    d.close()


@pytest.fixture
def queue(db):
    return SqliteTaskQueue(db, visibility_timeout_s=30, max_attempts=2)


def _task(tid="t1", kind=TaskKind.PARSE_RESUME, **params):
    payload = tuple(params.items()) if params else (("resume_id", "r1"),)
    return Task(id=tid, kind=kind, payload=payload)


def _expire_lock(db):
    """把锁改成已过期 —— 比 sleep 60 秒快得多，且确定性。"""
    db.execute("UPDATE tasks SET locked_until = '2000-01-01T00:00:00Z'")


# ============================================================
# 基本出入队
# ============================================================

def test_enqueue_then_dequeue_roundtrip(queue):
    queue.enqueue(_task(kind=TaskKind.SCREEN_RUN, run_id="run1"))

    task = queue.dequeue(timeout=0)
    assert task.id == "t1"
    assert task.kind is TaskKind.SCREEN_RUN
    assert task.params == {"run_id": "run1"}


def test_empty_queue_returns_none(queue):
    assert queue.dequeue(timeout=0) is None


def test_enqueue_is_idempotent(queue):
    """同一个 id 入队两次，队列里只有一条 —— 重试入队不该造出重复任务。"""
    queue.enqueue(_task())
    queue.enqueue(_task())

    queue.dequeue(timeout=0)
    assert queue.dequeue(timeout=0) is None


def test_fifo_order(queue):
    queue.enqueue(_task("t1"))
    queue.enqueue(_task("t2"))

    assert queue.dequeue(timeout=0).id == "t1"
    queue.ack("t1")
    assert queue.dequeue(timeout=0).id == "t2"


def test_ack_removes_task(queue):
    queue.enqueue(_task())
    queue.dequeue(timeout=0)
    queue.ack("t1")

    assert queue.dequeue(timeout=0) is None


# ============================================================
# 可见性锁
# ============================================================

def test_locked_task_is_not_handed_out_twice(queue):
    """领取后、ack 前，别的 worker 不能拿到同一条。"""
    queue.enqueue(_task())
    queue.dequeue(timeout=0)

    assert queue.dequeue(timeout=0) is None


def test_expired_lock_makes_task_visible_again(queue, db):
    """worker 崩溃没 ack：锁过期后任务自动回到队列。"""
    queue.enqueue(_task())
    queue.dequeue(timeout=0)
    _expire_lock(db)

    again = queue.dequeue(timeout=0)
    assert again is not None
    assert again.id == "t1"
    assert again.attempts == 2  # 领取时就计数


# ============================================================
# 失败与死信
# ============================================================

def test_nack_retry_makes_task_immediately_available(queue):
    queue.enqueue(_task())
    queue.dequeue(timeout=0)

    queue.nack("t1", retry=True, error="模型超时")

    again = queue.dequeue(timeout=0)
    assert again.id == "t1"
    # attempts 在 dequeue 时 +1 → 现在是第二次
    assert again.attempts == 2


def test_nack_without_retry_goes_dead(queue, db):
    queue.enqueue(_task())
    queue.dequeue(timeout=0)

    queue.nack("t1", retry=False, error="PDF 损坏")

    assert queue.dequeue(timeout=0) is None
    row = db.query_one("SELECT status, last_error FROM tasks WHERE id = 't1'")
    assert row["status"] == "dead"
    assert row["last_error"] == "PDF 损坏"


def test_retry_exhaustion_goes_dead(queue, db):
    """max_attempts=2：第三次失败时即使 retry=True 也必须进死信。"""
    queue.enqueue(_task())

    queue.dequeue(timeout=0)          # attempts=1
    queue.nack("t1", retry=True)
    queue.dequeue(timeout=0)          # attempts=2
    queue.nack("t1", retry=True)      # 已达上限

    assert queue.dequeue(timeout=0) is None
    assert db.query_one("SELECT status FROM tasks")["status"] == "dead"


def test_nack_unknown_task_is_noop(queue):
    queue.nack("不存在", retry=True)  # 不抛异常


# ============================================================
# 契约
# ============================================================

def test_queue_satisfies_port(queue):
    assert isinstance(queue, TaskQueuePort)


def test_task_kind_stored_as_plain_string(queue, db):
    queue.enqueue(_task(kind=TaskKind.SCREEN_RUN))
    assert db.query_one("SELECT kind FROM tasks")["kind"] == "screen_run"
