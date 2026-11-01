"""results 表与 corpus_versions 的仓储测试。

跑在 sqlite 的 :memory: 上，但要注意 results 有指向 runs / resumes 的外键，
所以每条用例都得先造出对应的 run 和 resume 行。
"""

import pytest

from app.adapters.repo.db import Database
from app.adapters.repo.sqlite_repo import SqliteResumeRepository, SqliteRunRepository
from app.domain.models import (
    DimScore,
    Resume,
    ResumeStatus,
    Run,
    RunStatus,
)
from app.ports.repository import ResultRow


@pytest.fixture
def db():
    d = Database(":memory:")
    d.initialize()
    yield d
    d.close()


@pytest.fixture
def repos(db):
    return SqliteResumeRepository(db), SqliteRunRepository(db)


def _seed(db, repos, resume_ids=("r1",)):
    resume_repo, run_repo = repos
    for rid in resume_ids:
        resume_repo.add(
            Resume(
                id=rid,
                filename=f"{rid}.pdf",
                content_sha256=f"sha-{rid}",
                # 用 queued：indexed 要求必须有 metadata，这里不关心状态
                status=ResumeStatus.QUEUED,
            )
        )
    run_repo.create(Run(id="run1", query_text="招 Python", profile="tech"))


def _dims():
    return (
        DimScore(name="skill", score=0.8, weight=0.3, reason="命中 2/3"),
        DimScore(name="education", score=None, weight=0.2, reason="无数据"),
    )


# ============================================================
# results
# ============================================================

def test_save_and_list_roundtrip(db, repos):
    _seed(db, repos)
    _, run_repo = repos
    run_repo.save_results(
        "run1",
        [
            ResultRow(resume_id="r1", rank=1, overall=0.75, semantic=0.6,
                      dims=_dims(), analysis_text="推荐", analysis_prompt_version="v1"),
        ],
    )

    (row,) = run_repo.list_results("run1")
    assert row.rank == 1
    assert row.overall == pytest.approx(0.75)
    assert row.dims[0].name == "skill"
    assert row.dims[0].score == pytest.approx(0.8)
    # score=None 的维度也要原样读回来（剔除分母依赖它）
    assert row.dims[1].score is None
    assert row.analysis_text == "推荐"


def test_rejected_candidates_are_kept(db, repos):
    """被淘汰的人必须留一行 —— 否则"他为什么没出现"永远答不了。"""
    _seed(db, repos)
    _, run_repo = repos
    run_repo.save_results(
        "run1",
        [ResultRow(resume_id="r1", rank=1, passed=False, reject_reason="学历不足")],
    )

    (row,) = run_repo.list_results("run1")
    assert row.passed is False
    assert row.reject_reason == "学历不足"


def test_save_results_overwrites_previous_batch(db, repos):
    _seed(db, repos)
    _, run_repo = repos
    run_repo.save_results("run1", [ResultRow(resume_id="r1", rank=1)])
    run_repo.save_results("run1", [ResultRow(resume_id="r1", rank=7)])

    rows = run_repo.list_results("run1")
    assert len(rows) == 1
    assert rows[0].rank == 7


def test_list_results_is_ordered_by_rank(db, repos):
    _seed(db, repos, resume_ids=("r1", "r2"))
    _, run_repo = repos
    run_repo.save_results(
        "run1",
        [ResultRow(resume_id="r2", rank=2), ResultRow(resume_id="r1", rank=1)],
    )

    assert [r.resume_id for r in run_repo.list_results("run1")] == ["r1", "r2"]


def test_results_of_unknown_run_are_empty(db, repos):
    _, run_repo = repos
    assert run_repo.list_results("不存在") == []


def test_deleting_resume_cascades_results(db, repos):
    _seed(db, repos)
    resume_repo, run_repo = repos
    run_repo.save_results("run1", [ResultRow(resume_id="r1", rank=1)])

    resume_repo.delete("r1")
    assert run_repo.list_results("run1") == []


# ============================================================
# corpus_versions
# ============================================================

def test_empty_corpus_version_is_zero(db, repos):
    resume_repo, _ = repos
    assert resume_repo.corpus_version() == 0


def test_bump_increases_version(db, repos):
    _seed(db, repos, resume_ids=("r1", "r2"))
    resume_repo, _ = repos

    assert resume_repo.bump_corpus_version() == 1
    assert resume_repo.bump_corpus_version() == 2
    assert resume_repo.corpus_version() == 2


def test_bump_records_count_and_fingerprint(db, repos):
    _seed(db, repos, resume_ids=("r1", "r2"))
    resume_repo, _ = repos
    resume_repo.bump_corpus_version()

    row = db.query_one("SELECT resume_count, content_hash FROM corpus_versions")
    assert row["resume_count"] == 2
    assert len(row["content_hash"]) == 64  # sha256 hex


def test_fingerprint_changes_when_content_changes(db, repos):
    """删一份又加一份、数量不变，指纹必须变 —— 否则幂等键会误判"库没变"。"""
    resume_repo, _ = repos
    _seed(db, repos, resume_ids=("r1",))
    resume_repo.bump_corpus_version()
    first = db.query_one("SELECT content_hash FROM corpus_versions")["content_hash"]

    resume_repo.delete("r1")
    resume_repo.add(
        Resume(
            id="r9",
            filename="r9.pdf",
            content_sha256="sha-r9",
            status=ResumeStatus.QUEUED,
        )
    )
    resume_repo.bump_corpus_version()
    second = db.query_one(
        "SELECT content_hash FROM corpus_versions ORDER BY version DESC LIMIT 1"
    )["content_hash"]

    assert first != second
