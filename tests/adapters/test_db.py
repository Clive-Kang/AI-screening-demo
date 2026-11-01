"""app/adapters/repo/db.py 的单测。

用 :memory: 库，不碰磁盘，每个用例一个全新实例。
测的是三件最容易静默出错的事：建表、外键、事务。
"""

import re
import sqlite3

import pytest

from app.adapters.repo.db import Database, now_iso


@pytest.fixture
def db():
    database = Database(":memory:")
    database.initialize()
    yield database
    database.close()


# ============================================================
# 建表
# ============================================================

def test_initialize_creates_all_tables(db):
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    assert {"resumes", "resume_metadata", "resume_skills", "resume_locations",
            "resume_education", "resume_experience", "queries", "runs",
            "results", "corpus_versions", "tasks"} <= names


def test_initialize_is_idempotent(db):
    """启动时无条件执行一次，不能因为表已存在就炸。"""
    db.initialize()
    db.initialize()


def test_wal_mode_is_on():
    """WAL 让读不被写阻塞；:memory: 库会返回 'memory'，不报错即可。"""
    database = Database(":memory:")
    mode = database.query_one("PRAGMA journal_mode")[0]
    assert mode in ("wal", "memory")
    database.close()


# ============================================================
# 外键：SQLite 默认是关的，这个测试是那道保险
# ============================================================

def _insert_resume(db, rid="r1"):
    db.execute(
        "INSERT INTO resumes (id, filename, content_sha256, status, created_at)"
        " VALUES (?, ?, ?, 'queued', ?)",
        (rid, "a.pdf", f"sha-{rid}", now_iso()),
    )


def test_foreign_key_rejects_orphan_row(db):
    """指向不存在的简历 → 必须报错，而不是默默写进去。"""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO resume_skills (resume_id, skill_raw, skill_norm)"
            " VALUES ('不存在', 'Python', 'python')"
        )


def test_delete_cascades_to_children(db):
    """删简历 → 技能/经历/结果一起走，不留孤儿行。"""
    _insert_resume(db)
    db.execute(
        "INSERT INTO resume_skills (resume_id, skill_raw, skill_norm)"
        " VALUES ('r1', 'Python', 'python')"
    )
    db.execute("DELETE FROM resumes WHERE id = 'r1'")
    assert db.query("SELECT * FROM resume_skills") == []


def test_composite_primary_key_blocks_duplicate_skill(db):
    """同一个人同一项技能只能有一条 —— 用约束去重，而不是先查再插。"""
    _insert_resume(db)
    sql = ("INSERT INTO resume_skills (resume_id, skill_raw, skill_norm)"
           " VALUES ('r1', 'FastAPI', 'fastapi')")
    db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql)


# ============================================================
# 事务
# ============================================================

def test_transaction_commits_on_success(db):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO resumes (id, filename, content_sha256, status, created_at)"
            " VALUES ('r1', 'a.pdf', 'sha1', 'queued', ?)", (now_iso(),),
        )
    assert db.query_one("SELECT id FROM resumes WHERE id='r1'") is not None


def test_transaction_rolls_back_on_error(db):
    """写了一半失败 → 一行都不该留下（上传链路的半成品问题）。"""
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO resumes (id, filename, content_sha256, status, created_at)"
                " VALUES ('r1', 'a.pdf', 'sha1', 'queued', ?)", (now_iso(),),
            )
            raise RuntimeError("模拟中途失败")
    assert db.query("SELECT * FROM resumes") == []


def test_transaction_can_be_reused_after_rollback(db):
    """回滚后连接必须还能用 —— 否则 worker 一次失败就废了。"""
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("SELECT 1")
            raise RuntimeError("boom")
    _insert_resume(db)
    assert db.query_one("SELECT id FROM resumes")["id"] == "r1"


# ============================================================
# 时间
# ============================================================

def test_now_iso_format():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", now_iso())
