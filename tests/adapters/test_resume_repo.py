"""SqliteResumeRepository 的单测。

用 :memory: 库，不碰磁盘。
重点验证「一份 Resume 存进去再读出来，还是同一个对象」——
这是仓储唯一的职责。
"""

import pytest

from app.adapters.repo.db import Database
from app.adapters.repo.sqlite_repo import SqliteResumeRepository
from app.domain.models import (
    DateRange,
    Education,
    Resume,
    ResumeMetadata,
    ResumeStatus,
    WorkExperience,
)


@pytest.fixture
def repo():
    db = Database(":memory:")
    db.initialize()
    yield SqliteResumeRepository(db)
    db.close()


def _meta() -> ResumeMetadata:
    return ResumeMetadata(
        name="张三",
        email="zhang@example.com",
        skills=("Python", "FastAPI", "Python"),       # 故意重复一次
        preferred_locations=("北京",),
        educations=(
            Education(institution="某大学", major="计算机",
                      degree_raw="本科", degree_level=2,
                      period=DateRange("2014-09", "2018-06")),
        ),
        experiences=(
            WorkExperience("A 公司", "后端",
                           DateRange("2018-07", None, is_current=True)),
            WorkExperience("B 公司", "实习生",
                           DateRange("2017-07", "2017-12")),
        ),
        expected_salary="20K-30K",
    )


def _resume(rid="r1", sha="sha1", status=ResumeStatus.EXTRACTED) -> Resume:
    return Resume(
        id=rid,
        filename="张三.pdf",
        content_sha256=sha,
        raw_text="张三 2018 年至今在 A 公司",
        metadata=_meta(),
        status=status,
        extractor_prompt_version="v1",
    )


# ============================================================
# add / get
# ============================================================

def test_add_then_get_roundtrips(repo):
    repo.add(_resume())
    got = repo.get("r1")

    assert got is not None
    assert got.filename == "张三.pdf"
    assert got.status is ResumeStatus.EXTRACTED
    assert got.extractor_prompt_version == "v1"
    assert got.metadata is not None
    assert got.metadata.name == "张三"
    assert got.metadata.skills == ("Python", "FastAPI", "Python")
    assert got.created_at          # 仓储自动填了时间


def test_roundtrip_preserves_is_current(repo):
    """「至今」的语义必须原样保留：end_ym 为空 + is_current=True。"""
    repo.add(_resume())
    exp = repo.get("r1").metadata.experiences[0]
    assert exp.period.is_current is True
    assert exp.period.end_ym is None


def test_roundtrip_preserves_education_level(repo):
    repo.add(_resume())
    edu = repo.get("r1").metadata.educations[0]
    assert edu.degree_level == 2
    assert edu.degree_raw == "本科"


def test_get_missing_returns_none(repo):
    assert repo.get("不存在") is None


def test_add_without_metadata(repo):
    """queued 状态的简历还没抽取 —— metadata 为 None 也必须能存。"""
    repo.add(Resume(id="r2", filename="a.pdf", content_sha256="sha2"))
    got = repo.get("r2")
    assert got.status is ResumeStatus.QUEUED
    assert got.metadata is None


# ============================================================
# 去重查询
# ============================================================

def test_find_by_content_hash(repo):
    repo.add(_resume())
    assert repo.find_by_content_hash("sha1").id == "r1"
    assert repo.find_by_content_hash("另一个哈希") is None


def test_content_hash_is_unique(repo):
    import sqlite3
    repo.add(_resume(rid="r1", sha="same"))
    with pytest.raises(sqlite3.IntegrityError):
        repo.add(_resume(rid="r2", sha="same"))


# ============================================================
# 拆表
# ============================================================

def test_skills_are_written_normalized(repo):
    repo.add(_resume())
    rows = repo._db.query(
        "SELECT skill_raw, skill_norm FROM resume_skills WHERE resume_id='r1'"
        " ORDER BY skill_norm"
    )
    assert [r["skill_norm"] for r in rows] == ["fastapi", "python"]


def test_duplicate_skills_do_not_break_insert(repo):
    """"Python" 与 "python" 归一后撞主键，应当是忽略而不是整个上传失败。"""
    repo.add(_resume())
    count = repo._db.query_one(
        "SELECT COUNT(*) AS n FROM resume_skills WHERE resume_id='r1'"
    )["n"]
    assert count == 2


def test_experience_rows_are_written(repo):
    repo.add(_resume())
    rows = repo._db.query(
        "SELECT * FROM resume_experience WHERE resume_id='r1' ORDER BY start_ym"
    )
    assert len(rows) == 2
    assert rows[0]["is_current"] == 0
    assert rows[1]["is_current"] == 1
    # months 故意留空：不能把"至今"固化成写入当天的月份
    assert rows[1]["months"] is None


# ============================================================
# update / delete
# ============================================================

def test_update_changes_status(repo):
    repo.add(_resume(status=ResumeStatus.QUEUED))
    resume = repo.get("r1")
    # frozen dataclass 没有 setter，用 dataclasses.replace 造新对象
    from dataclasses import replace
    repo.update(replace(resume, status=ResumeStatus.PARSED, raw_text="新文本"))
    assert repo.get("r1").status is ResumeStatus.PARSED


def test_update_missing_raises(repo):
    with pytest.raises(KeyError):
        repo.update(_resume(rid="不存在"))


def test_update_replaces_child_rows(repo):
    """重抽一次简历，旧的技能行不能残留。"""
    repo.add(_resume())
    from dataclasses import replace
    new_meta = ResumeMetadata(name="张三", skills=("Go",))
    repo.update(replace(repo.get("r1"), metadata=new_meta))
    norms = [r["skill_norm"] for r in repo._db.query(
        "SELECT skill_norm FROM resume_skills WHERE resume_id='r1'"
    )]
    assert norms == ["go"]


def test_delete_removes_children(repo):
    repo.add(_resume())
    repo.delete("r1")
    assert repo.get("r1") is None
    assert repo._db.query("SELECT * FROM resume_skills") == []
    assert repo._db.query("SELECT * FROM resume_experience") == []
    assert repo._db.query("SELECT * FROM resume_metadata") == []


# ============================================================
# list
# ============================================================

def test_list_paginates(repo):
    for i in range(5):
        repo.add(_resume(rid=f"r{i}", sha=f"sha{i}"))
    page = repo.list(limit=2, offset=0)
    assert len(page) == 2


def test_list_filters_by_status(repo):
    repo.add(_resume(rid="r1", sha="s1", status=ResumeStatus.EXTRACTED))
    repo.add(Resume(id="r2", filename="b.pdf", content_sha256="s2"))
    assert [r.id for r in repo.list(limit=10, offset=0,
                                    status=ResumeStatus.QUEUED)] == ["r2"]
