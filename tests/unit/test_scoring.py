"""app/domain/scoring.py 的单测：单维打分。

每个维度都要覆盖三种结果：
    有数据且匹配  → 具体分数
    有数据不匹配  → 0.0 附近
    没数据        → None（不是 0！）
"""

import pytest

from app.domain.models import DateRange, Education, JobRequirement, ResumeMetadata, WorkExperience
from app.domain.policies import DimWeight, ScoringProfile
from app.domain.scoring import (
    combine,
    score_education,
    score_experience,
    score_industry,
    score_location,
    score_salary,
    score_skill,
    score_tag,
)

TODAY = "2026-10"

# 与 config/scoring.yaml 的 tech 档案同构，直接构造省去读文件。
PROFILE = ScoringProfile(
    dims={
        "skill": DimWeight(weight=0.40),
        "education": DimWeight(weight=0.15),
        "location": DimWeight(weight=0.15),
        "industry": DimWeight(weight=0.10),
        "salary": DimWeight(weight=0.10),
        "tag": DimWeight(weight=0.10),
    },
    semantic=DimWeight(weight=0.20),
)


# ============================================================
# score_skill
# ============================================================

def test_skill_returns_none_when_resume_has_no_skills():
    assert score_skill(ResumeMetadata(), JobRequirement(required_skills=("Python",))) is None


def test_skill_returns_none_when_requirement_has_no_skills():
    resume = ResumeMetadata(skills=("Python",))
    assert score_skill(resume, JobRequirement()) is None


def test_skill_all_required_matched():
    resume = ResumeMetadata(skills=("Python", "FastAPI", "PostgreSQL"))
    req = JobRequirement(required_skills=("Python", "FastAPI"))
    assert score_skill(resume, req) == pytest.approx(1.0)


def test_skill_partial_match():
    resume = ResumeMetadata(skills=("Python",))
    req = JobRequirement(required_skills=("Python", "FastAPI", "Redis", "Docker"))
    assert score_skill(resume, req) == pytest.approx(0.25)


def test_skill_required_and_preferred_weighted():
    """必需 2 个中命中 2 个，优先 2 个中命中 0 个 → 1.0×0.8 + 0.0×0.2。"""
    resume = ResumeMetadata(skills=("Python", "FastAPI"))
    req = JobRequirement(
        required_skills=("Python", "FastAPI"),
        preferred_skills=("Kubernetes", "Go"),
    )
    assert score_skill(resume, req) == pytest.approx(0.8)


def test_skill_only_preferred_uses_full_scale():
    """只写了优先技能时，它就是 100% —— 不能被 0.2 的系数压到 0.2 满分。"""
    resume = ResumeMetadata(skills=("Go",))
    req = JobRequirement(preferred_skills=("Go",))
    assert score_skill(resume, req) == pytest.approx(1.0)


def test_skill_go_does_not_match_django():
    """回归测试：参考项目 `"go" in "django"` 为 True，把所有 Django 简历判成会 Go。"""
    resume = ResumeMetadata(skills=("Django",))
    req = JobRequirement(required_skills=("Go",))
    assert score_skill(resume, req) == pytest.approx(0.0)


def test_skill_java_does_not_match_javascript():
    resume = ResumeMetadata(skills=("JavaScript",))
    req = JobRequirement(required_skills=("Java",))
    assert score_skill(resume, req) == pytest.approx(0.0)


def test_skill_fuzzy_suffix_match():
    """'Django' 应命中简历里的 'Django框架'。"""
    resume = ResumeMetadata(skills=("Django框架",))
    req = JobRequirement(required_skills=("Django",))
    assert score_skill(resume, req) == pytest.approx(1.0)


def test_skill_case_and_space_insensitive():
    resume = ResumeMetadata(skills=("Python 3", "node.js"))
    req = JobRequirement(required_skills=("python3", "Node.js"))
    assert score_skill(resume, req) == pytest.approx(1.0)


# ============================================================
# score_education
# ============================================================

def test_education_none_when_requirement_unlimited():
    resume = ResumeMetadata(educations=(Education(degree_level=2),))
    assert score_education(resume, JobRequirement()) is None


def test_education_none_when_resume_has_none():
    req = JobRequirement(min_degree_level=2)
    assert score_education(ResumeMetadata(), req) is None


def test_education_none_when_level_unknown():
    """有教育记录但识别不出等级 → 没数据，不是 0 分。"""
    resume = ResumeMetadata(educations=(Education(degree_raw="某学位"),))
    assert score_education(resume, JobRequirement(min_degree_level=2)) is None


def test_education_exact_requirement():
    resume = ResumeMetadata(educations=(Education(degree_level=2),))
    assert score_education(resume, JobRequirement(min_degree_level=2)) == pytest.approx(1.0)


def test_education_overqualified_is_capped():
    resume = ResumeMetadata(educations=(Education(degree_level=4),))
    assert score_education(resume, JobRequirement(min_degree_level=2)) == pytest.approx(1.0)


def test_education_underqualified_is_proportional():
    """大专(1) 对本科(2) → 0.5，不是 0。"""
    resume = ResumeMetadata(educations=(Education(degree_level=1),))
    assert score_education(resume, JobRequirement(min_degree_level=2)) == pytest.approx(0.5)


# ============================================================
# score_experience
# ============================================================

def _resume_with_months(start, end, current=False):
    return ResumeMetadata(experiences=(
        WorkExperience("A", "后端", DateRange(start, end, current)),
    ))


def test_experience_none_when_requirement_unlimited():
    resume = _resume_with_months("2020-01", "2023-01")
    assert score_experience(resume, JobRequirement(), TODAY) is None


def test_experience_none_when_resume_has_no_experience():
    req = JobRequirement(min_experience_months=36)
    assert score_experience(ResumeMetadata(), req, TODAY) is None


def test_experience_exact_match():
    """2020-01 ~ 2023-01 = 36 个月，要求 36 个月。"""
    assert score_experience(
        _resume_with_months("2020-01", "2023-01"),
        JobRequirement(min_experience_months=36), TODAY,
    ) == pytest.approx(1.0)


def test_experience_more_than_required_is_capped():
    assert score_experience(
        _resume_with_months("2010-01", "2023-01"),
        JobRequirement(min_experience_months=36), TODAY,
    ) == pytest.approx(1.0)


def test_experience_proportional():
    """18 个月 / 要求 36 个月 = 0.5。"""
    assert score_experience(
        _resume_with_months("2020-01", "2021-07"),
        JobRequirement(min_experience_months=36), TODAY,
    ) == pytest.approx(0.5)


def test_experience_current_job_uses_today():
    """2020-07 至今（今天 2026-10）= 75 个月，要求 60 → 1.0。"""
    assert score_experience(
        _resume_with_months("2020-07", None, current=True),
        JobRequirement(min_experience_months=60), TODAY,
    ) == pytest.approx(1.0)


# ============================================================
# score_location
# ============================================================

def test_location_none_without_requirement_or_data():
    assert score_location(ResumeMetadata(preferred_locations=("北京",)), JobRequirement()) is None
    assert score_location(ResumeMetadata(), JobRequirement(locations=("北京",))) is None


def test_location_fuzzy_city_match():
    """'北京' 应命中 '北京市' —— 城市名的 min_len 是 2，不是 3。"""
    resume = ResumeMetadata(preferred_locations=("北京市",))
    assert score_location(resume, JobRequirement(locations=("北京",))) == pytest.approx(1.0)


def test_location_mismatch():
    resume = ResumeMetadata(preferred_locations=("杭州",))
    assert score_location(resume, JobRequirement(locations=("北京",))) == pytest.approx(0.0)


def test_location_partial_rate():
    """岗位两个城市，候选人只期望其中一个 → 0.5。"""
    resume = ResumeMetadata(preferred_locations=("北京",))
    req = JobRequirement(locations=("北京", "上海"))
    assert score_location(resume, req) == pytest.approx(0.5)


# ============================================================
# score_industry
# ============================================================

def test_industry_none_without_requirement_or_data():
    assert score_industry(ResumeMetadata(industries=("电商",)), JobRequirement()) is None
    assert score_industry(ResumeMetadata(), JobRequirement(industries=("电商",))) is None


def test_industry_match_and_mismatch():
    assert score_industry(
        ResumeMetadata(industries=("互联网", "电商")),
        JobRequirement(industries=("电商",)),
    ) == pytest.approx(1.0)
    assert score_industry(
        ResumeMetadata(industries=("制造业",)),
        JobRequirement(industries=("电商",)),
    ) == pytest.approx(0.0)


# ============================================================
# score_tag
# ============================================================

def test_tag_none_without_requirement_or_data():
    assert score_tag(ResumeMetadata(tags=("大厂背景",)), JobRequirement()) is None
    assert score_tag(ResumeMetadata(), JobRequirement(tags=("大厂背景",))) is None


def test_tag_partial():
    resume = ResumeMetadata(tags=("大厂背景", "带过团队"))
    req = JobRequirement(tags=("大厂背景", "开源项目", "带过团队", "海外经验"))
    assert score_tag(resume, req) == pytest.approx(0.5)


# ============================================================
# score_salary
# ============================================================

def test_salary_none_without_budget():
    resume = ResumeMetadata(expected_salary="25K")
    assert score_salary(resume, JobRequirement()) is None


@pytest.mark.parametrize("raw", [None, "", "面议", "年薪 40 万"])
def test_salary_none_when_expected_unparseable(raw):
    """没写 / 面议 / 年薪 → 没数据。不写薪资不该被扣分。"""
    resume = ResumeMetadata(expected_salary=raw)
    assert score_salary(resume, JobRequirement(salary_min=20000, salary_max=30000)) is None


def test_salary_inside_budget_is_full():
    resume = ResumeMetadata(expected_salary="25K")
    assert score_salary(resume, JobRequirement(salary_min=20000, salary_max=30000)) == pytest.approx(1.0)


def test_salary_half_overlap():
    """期望 20-40K，预算 20-30K → 重叠 10K / 跨度 20K = 0.5。"""
    resume = ResumeMetadata(expected_salary="20K-40K")
    assert score_salary(resume, JobRequirement(salary_min=20000, salary_max=30000)) == pytest.approx(0.5)


def test_salary_no_overlap():
    resume = ResumeMetadata(expected_salary="35K-40K")
    assert score_salary(resume, JobRequirement(salary_min=20000, salary_max=30000)) == pytest.approx(0.0)


def test_salary_open_ended_budget():
    """预算只写上限 30K，期望 20K 在范围内。"""
    resume = ResumeMetadata(expected_salary="20K")
    assert score_salary(resume, JobRequirement(salary_max=30000)) == pytest.approx(1.0)


# ============================================================
# combine：六维 + 语义 → ScoreBreakdown
# ============================================================

def _perfect_match():
    """六维全部满分的一组输入。"""
    resume = ResumeMetadata(
        skills=("Python",),
        educations=(Education(degree_level=2),),
        industries=("互联网",),
        preferred_locations=("北京",),
        expected_salary="25K",
        tags=("大厂背景",),
    )
    req = JobRequirement(
        required_skills=("Python",),
        min_degree_level=2,
        industries=("互联网",),
        locations=("北京",),
        salary_min=20000,
        salary_max=30000,
        tags=("大厂背景",),
    )
    return resume, req


def test_combine_full_match_with_semantic():
    """六维全 1.0，语义 0.5 → 1.0×0.8 + 0.5×0.2 = 0.9。"""
    resume, req = _perfect_match()
    b = combine(resume, req, PROFILE, profile_name="tech", semantic_score=0.5)
    assert b.coverage == pytest.approx(1.0)
    assert b.overall == pytest.approx(0.9)
    assert b.profile == "tech"
    assert b.semantic is not None


def test_combine_without_semantic_uses_dims_only():
    resume, req = _perfect_match()
    b = combine(resume, req, PROFILE, profile_name="tech")
    assert b.overall == pytest.approx(1.0)
    assert b.semantic is None


def test_combine_reports_fixed_dim_order():
    resume, req = _perfect_match()
    b = combine(resume, req, PROFILE, profile_name="tech")
    assert [d.name for d in b.dims] == [
        "skill", "education", "industry", "location", "salary", "tag",
    ]


def test_combine_reason_is_human_readable():
    resume, req = _perfect_match()
    req = JobRequirement(required_skills=("Python", "FastAPI"))  # 只命中 1/2
    b = combine(resume, req, PROFILE, profile_name="tech")
    skill = next(d for d in b.dims if d.name == "skill")
    assert "必需技能命中 1/2" in skill.reason


def test_combine_marks_absent_requirement():
    """岗位没提学历 → 该维 None，理由是「岗位未要求」而不是「简历没有」。"""
    resume, req = _perfect_match()
    req = JobRequirement(required_skills=("Python",))
    b = combine(resume, req, PROFILE, profile_name="tech")
    edu = next(d for d in b.dims if d.name == "education")
    assert edu.score is None
    assert edu.reason == "岗位未要求"


def test_combine_marks_missing_resume_data():
    resume = ResumeMetadata(skills=("Python",))
    req = JobRequirement(locations=("北京",))
    b = combine(resume, req, PROFILE, profile_name="tech")
    loc = next(d for d in b.dims if d.name == "location")
    assert loc.reason == "简历无此类数据"


def test_combine_missing_dims_do_not_penalize():
    """只有技能有数据且满分 → 1.0，而不是被其他空维度拖到 0.40。

    这就是「剔除分母」：有效权重 0.40，分母也是 0.40。
    """
    resume = ResumeMetadata(skills=("Python",))
    req = JobRequirement(required_skills=("Python",), locations=("北京",))
    b = combine(resume, req, PROFILE, profile_name="tech")
    assert b.coverage == pytest.approx(0.40)
    assert b.overall == pytest.approx(1.0)


def test_combine_partial_coverage_weighted_correctly():
    """技能 1.0（w=0.40）、学历 0.5（w=0.15）→ (0.4 + 0.075) / 0.55。"""
    resume = ResumeMetadata(
        skills=("Python",),
        educations=(Education(degree_level=1),),
    )
    req = JobRequirement(required_skills=("Python",), min_degree_level=2)
    b = combine(resume, req, PROFILE, profile_name="tech")
    assert b.coverage == pytest.approx(0.55)
    assert b.overall == pytest.approx((1.0 * 0.40 + 0.5 * 0.15) / 0.55)


def test_combine_semantic_only_when_no_dims():
    b = combine(ResumeMetadata(), JobRequirement(), PROFILE,
                profile_name="tech", semantic_score=0.6)
    assert b.coverage == pytest.approx(0.0)
    assert b.overall == pytest.approx(0.6)


def test_combine_nothing_available():
    b = combine(ResumeMetadata(), JobRequirement(), PROFILE, profile_name="tech")
    assert b.overall is None
    assert b.coverage == pytest.approx(0.0)
