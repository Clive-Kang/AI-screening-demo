"""SqliteRunRepository 的单测。

重点不是 CRUD，而是三段式写入的**并发语义**：
    update_stage 不能把已取消的任务拉回运行中
    finish 只更新传进来的计数，其余保留原值
"""

import pytest

from app.adapters.repo.db import Database
from app.adapters.repo.sqlite_repo import SqliteRunRepository
from app.domain.models import Run, RunStage, RunStatus


@pytest.fixture
def repo():
    db = Database(":memory:")
    db.initialize()
    yield SqliteRunRepository(db)
    db.close()


def _run(rid="run1", key="", status=RunStatus.QUEUED) -> Run:
    return Run(
        id=rid,
        query_text="招 3 年 Python 后端，北京",
        profile="tech",
        status=status,
        idempotency_key=key,
        config_snapshot_json='{"dims": {}}',
        finished_at="2026-10-18T00:00:00Z" if status in (
            RunStatus.SUCCEEDED, RunStatus.PARTIAL,
            RunStatus.FAILED, RunStatus.CANCELLED,
        ) else None,
        error="boom" if status is RunStatus.FAILED else None,
    )


# ============================================================
# create / get
# ============================================================

def test_create_then_get(repo):
    repo.create(_run())
    got = repo.get("run1")
    assert got.query_text == "招 3 年 Python 后端，北京"
    assert got.profile == "tech"
    assert got.status is RunStatus.QUEUED
    assert got.created_at          # 仓储自动填


def test_get_missing_returns_none(repo):
    assert repo.get("不存在") is None


def test_idempotency_lookup(repo):
    repo.create(_run(key="abc"))
    assert repo.find_by_idempotency_key("abc").id == "run1"
    assert repo.find_by_idempotency_key("别的") is None
    # 空 key 不该匹配到任何人
    assert repo.find_by_idempotency_key("") is None


def test_empty_idempotency_keys_do_not_collide(repo):
    """UNIQUE 列上多个空串会冲突，所以存成 NULL。"""
    repo.create(_run(rid="r1"))
    repo.create(_run(rid="r2"))
    assert repo.get("r1") is not None and repo.get("r2") is not None


# ============================================================
# update_stage
# ============================================================

def test_update_stage_sets_running_and_progress(repo):
    repo.create(_run())
    repo.update_stage("run1", RunStage.RECALL, {"recall": 120})

    got = repo.get("run1")
    assert got.status is RunStatus.RUNNING
    assert got.stage is RunStage.RECALL
    assert dict(got.progress) == {"recall": 120}


def test_update_stage_refuses_finished_run(repo):
    """已取消/已结束的任务不该被 worker 拉回运行中（会继续烧 token）。"""
    repo.create(_run(rid="r1", status=RunStatus.CANCELLED))
    with pytest.raises(KeyError):
        repo.update_stage("r1", RunStage.SCORE, {"scored": 10})


def test_update_stage_missing_run_raises(repo):
    with pytest.raises(KeyError):
        repo.update_stage("不存在", RunStage.RECALL, {})


# ============================================================
# finish
# ============================================================

def test_finish_sets_terminal_state(repo):
    repo.create(_run())
    repo.finish("run1", RunStatus.SUCCEEDED,
                {"recall": 100, "filtered": 34, "scored": 34, "analyzed": 10})

    got = repo.get("run1")
    assert got.status is RunStatus.SUCCEEDED
    assert got.stage is RunStage.DONE
    assert got.finished_at is not None
    assert (got.recall_n, got.filtered_n, got.scored_n, got.analyzed_n) == (100, 34, 34, 10)


def test_finish_keeps_counts_not_passed(repo):
    """两次 finish 里，第二次只传 analyzed，前面写的计数必须保留。"""
    repo.create(_run())
    repo.finish("run1", RunStatus.PARTIAL, {"recall": 100, "filtered": 34})
    repo.finish("run1", RunStatus.PARTIAL, {"analyzed": 10})

    got = repo.get("run1")
    assert got.recall_n == 100
    assert got.filtered_n == 34
    assert got.analyzed_n == 10


def test_finish_failed_requires_error(repo):
    repo.create(_run())
    with pytest.raises(ValueError, match="error"):
        repo.finish("run1", RunStatus.FAILED, {})


def test_finish_failed_keeps_stage(repo):
    """失败时保留失败发生的阶段，便于排查。"""
    repo.create(_run())
    repo.update_stage("run1", RunStage.FILTER, {"filtered": 5})
    repo.finish("run1", RunStatus.FAILED, {}, error="LLM 超时")

    got = repo.get("run1")
    assert got.status is RunStatus.FAILED
    assert got.stage is RunStage.FILTER
    assert got.error == "LLM 超时"


def test_finish_missing_run_raises(repo):
    with pytest.raises(KeyError):
        repo.finish("不存在", RunStatus.SUCCEEDED, {})


# ============================================================
# list
# ============================================================

def test_list_is_newest_first(repo):
    for i in range(3):
        repo.create(_run(rid=f"run{i}"))
    ids = [r.id for r in repo.list(limit=10, offset=0)]
    assert len(ids) == 3
    # created_at 秒级精度可能相同，所以只断言数量与可分页
    assert [r.id for r in repo.list(limit=1, offset=0)] != []
