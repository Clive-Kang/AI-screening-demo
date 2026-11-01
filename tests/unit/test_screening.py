"""ScreeningService 的单测。

用**真实的 SQLite 仓储**（:memory:）+ 假的 LLM / 嵌入 / 向量库。
这样验证的是"编排 + 落库 + 幂等"的完整行为，但零外部依赖、零 API 费用。
"""

from pathlib import Path

import pytest

from app.adapters.repo.db import Database
from app.adapters.repo.sqlite_repo import SqliteResumeRepository, SqliteRunRepository
from app.domain.models import (
    DateRange,
    Education,
    Resume,
    ResumeMetadata,
    ResumeStatus,
    RunStatus,
    WorkExperience,
)
from app.domain.policies import load_scoring_config
from app.ports.llm import LLMError
from app.ports.vector_store import Hit
from app.prompts import Prompt
from app.services.schemas import ExtractedJobRequirement
from app.services.screening import ScreeningService, idempotency_key

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring.yaml"

REQ_PROMPT = Prompt(name="job_requirement", version="v1", text="需求：${query_text}")
REPORT_PROMPT = Prompt(name="candidate_report", version="v1", text="报告")


# ============================================================
# 假实现
# ============================================================

class FakeEmbedder:
    def embed(self, texts):
        return [tuple([0.1] * 4) for _ in texts]


class FakeVectors:
    """按 id 顺序返回预置的命中结果。"""

    def __init__(self, hits=None):
        self.hits = hits or []

    def search(self, vector, *, top_k, min_similarity):
        return [h for h in self.hits if h.similarity >= min_similarity][:top_k]

    def upsert(self, docs):
        pass

    def delete(self, resume_ids):
        pass

    def drop_and_rebuild(self):
        pass


class FakeLLM:
    def __init__(self, requirement=None, *, fail_report=False):
        self.requirement = requirement or ExtractedJobRequirement(
            required_skills=["Python"], min_degree_level=2
        )
        self.fail_report = fail_report
        self.extract_calls = 0
        self.report_calls = 0

    def extract(self, prompt, schema):
        self.extract_calls += 1
        return self.requirement

    def generate(self, prompt):
        if self.fail_report:
            raise LLMError("模型超时")
        self.report_calls += 1
        return "## 结论\n推荐"


# ============================================================
# 环境
# ============================================================

def _resume(rid, *, degree=2, skills=("Python", "FastAPI")):
    return Resume(
        id=rid,
        filename=f"{rid}.pdf",
        content_sha256=f"sha-{rid}",
        metadata=ResumeMetadata(
            name=rid,
            skills=skills,
            educations=(Education(institution="某大学", degree_level=degree,
                                   period=DateRange(start_ym="2014-09", end_ym="2018-06")),),
            experiences=(WorkExperience(company="某公司", title="后端工程师",
                                        period=DateRange(start_ym="2020-01", is_current=True)),),
            preferred_locations=("北京",),
        ),
        status=ResumeStatus.INDEXED,
    )


@pytest.fixture
def env():
    db = Database(":memory:")
    db.initialize()
    resumes = SqliteResumeRepository(db)
    runs = SqliteRunRepository(db)
    config = load_scoring_config(CONFIG_PATH)
    yield db, resumes, runs, config
    db.close()


def _service(env, hits, llm=None, top_n=None):
    _, resumes, runs, config = env
    if top_n is not None:
        # 直接改配置对象不行（frozen），这里用一个只影响本次的副本
        config = config.model_copy(
            update={
                "profiles": {
                    **config.profiles,
                    "tech": config.profiles["tech"].model_copy(
                        update={"analyze_top_n": top_n}
                    ),
                }
            }
        )
    return ScreeningService(
        resumes=resumes,
        runs=runs,
        vectors=FakeVectors(hits),
        embedder=FakeEmbedder(),
        llm=llm or FakeLLM(),
        config=config,
        requirement_prompt=REQ_PROMPT,
        report_prompt=REPORT_PROMPT,
        today_ym="2026-10-01",
    )


# ============================================================
# 全流程
# ============================================================

def test_happy_path_produces_ranked_results(env):
    db, resumes, runs, _ = env
    resumes.add(_resume("r1"))
    resumes.add(_resume("r2"))

    result = _service(env, [Hit("r1", 0.9), Hit("r2", 0.5)]).run("招 Python 后端，本科")

    assert result.run.status is RunStatus.SUCCEEDED
    assert [r.resume_id for r in result.rows] == ["r1", "r2"]
    assert result.rows[0].rank == 1
    assert result.rows[0].analysis_text.startswith("## 结论")
    assert result.rows[0].analysis_prompt_version == "v1"
    # 全部结果都落了库，不是只存在内存里
    assert len(runs.list_results(result.run.id)) == 2


def test_scores_greater_for_better_semantic_match(env):
    _, resumes, _, _ = env
    resumes.add(_resume("r1"))
    resumes.add(_resume("r2"))

    rows = _service(env, [Hit("r1", 0.9), Hit("r2", 0.4)]).run("招 Python").rows
    assert rows[0].overall > rows[1].overall


def test_rejected_candidate_is_still_persisted(env):
    _, resumes, _, _ = env
    resumes.add(_resume("r1", degree=2))
    resumes.add(_resume("r2", degree=1))  # 大专，不满足本科

    rows = _service(env, [Hit("r1", 0.9), Hit("r2", 0.8)]).run("招本科").rows

    rejected = [r for r in rows if not r.passed]
    assert len(rejected) == 1
    assert rejected[0].resume_id == "r2"
    assert "学历" in rejected[0].reject_reason
    # 淘汰的排在通过的人后面
    assert rows[-1].resume_id == "r2"


def test_missing_metadata_is_skipped_not_fatal(env):
    """向量库里有、库里没有（删简历漏删向量）不该让整次筛选失败。"""
    _, resumes, _, _ = env
    resumes.add(_resume("r1"))

    result = _service(env, [Hit("r1", 0.9), Hit("ghost", 0.8)]).run("招 Python")

    assert result.run.status is RunStatus.SUCCEEDED
    assert [r.resume_id for r in result.rows] == ["r1"]


# ============================================================
# 幂等
# ============================================================

def test_same_query_hits_idempotency(env):
    _, resumes, _, _ = env
    resumes.add(_resume("r1"))
    llm = FakeLLM()
    svc = _service(env, [Hit("r1", 0.9)], llm=llm)

    first = svc.run("招 Python")
    second = svc.run("招 Python")

    assert first.run.id == second.run.id
    assert llm.extract_calls == 1  # 第二次没有重新解析需求
    assert llm.report_calls == 1


def test_corpus_change_invalidates_idempotency(env):
    _, resumes, _, _ = env
    resumes.add(_resume("r1"))
    svc = _service(env, [Hit("r1", 0.9)])

    first = svc.run("招 Python")
    resumes.bump_corpus_version()  # 简历库变了
    second = svc.run("招 Python")

    assert first.run.id != second.run.id


def test_idempotency_key_depends_on_all_three_factors():
    base = idempotency_key("招 Python", "tech", 1)
    assert base != idempotency_key("招 Go", "tech", 1)
    assert base != idempotency_key("招 Python", "sales", 1)
    assert base != idempotency_key("招 Python", "tech", 2)


# ============================================================
# API 预建 run + worker 执行（异步模式）
# ============================================================

def test_create_run_is_idempotent(env):
    svc = _service(env, [])

    first, existed_first = svc.create_run("招 Python")
    second, existed_second = svc.create_run("招 Python")

    assert existed_first is False
    assert existed_second is True
    assert first.id == second.id


def test_run_with_prebuilt_run_id_uses_that_run(env):
    _, resumes, runs, _ = env
    resumes.add(_resume("r1"))
    svc = _service(env, [Hit("r1", 0.9)])

    run, _ = svc.create_run("招 Python")
    result = svc.run("招 Python", run_id=run.id)

    assert result.run.id == run.id
    assert result.run.status is RunStatus.SUCCEEDED
    assert runs.list_results(run.id)[0].resume_id == "r1"


def test_run_on_finished_run_does_not_call_llm_again(env):
    """任务被重复投递（可见性锁过期）：已有结果直接返回，不重新计费。"""
    _, resumes, _, _ = env
    resumes.add(_resume("r1"))
    llm = FakeLLM()
    svc = _service(env, [Hit("r1", 0.9)], llm=llm)

    run, _ = svc.create_run("招 Python")
    svc.run("招 Python", run_id=run.id)
    svc.run("招 Python", run_id=run.id)

    assert llm.extract_calls == 1
    assert llm.report_calls == 1


def test_run_with_unknown_run_id_raises(env):
    with pytest.raises(ValueError, match="run 不存在"):
        _service(env, []).run("招 Python", run_id="不存在")


# ============================================================
# 报告阶段
# ============================================================

def test_analysis_failure_yields_partial_not_failed(env):
    _, resumes, _, _ = env
    resumes.add(_resume("r1"))

    result = _service(env, [Hit("r1", 0.9)], llm=FakeLLM(fail_report=True)).run("招 Python")

    assert result.run.status is RunStatus.PARTIAL
    assert result.rows[0].analysis_text is None
    # 分数照样有 —— 报告失败不影响排序结果
    assert result.rows[0].overall is not None


def test_only_top_n_candidates_get_reports(env):
    _, resumes, _, _ = env
    for rid in ("r1", "r2", "r3"):
        resumes.add(_resume(rid))

    llm = FakeLLM()
    hits = [Hit("r1", 0.9), Hit("r2", 0.8), Hit("r3", 0.7)]
    result = _service(env, hits, llm=llm, top_n=2).run("招 Python")

    assert llm.report_calls == 2
    assert sum(1 for r in result.rows if r.analysis_text) == 2
