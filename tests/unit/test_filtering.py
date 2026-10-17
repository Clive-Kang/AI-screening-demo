"""app/domain/filtering.py 的单测。

每个维度都要覆盖三种结果：
    pass     满足，或岗位没提这个条件
    fail     明确不满足（一定淘汰）
    unknown  简历没这项数据（怎么处理由策略决定）
"""

import pytest

from app.domain.filtering import (
    _check_education,
    _check_experience,
    _check_location,
    _check_salary,
    filter_resume,
)
from app.domain.models import DateRange, Education, JobRequirement, ResumeMetadata, WorkExperience
from app.domain.policies import UnknownFieldPolicy

TODAY = "2026-10"


def _resume_with_months(start, end):
    return ResumeMetadata(experiences=(
        WorkExperience("A", "后端", DateRange(start, end)),
    ))


# ============================================================
# 经验
# ============================================================

def test_experience_pass_when_not_required():
    assert _check_experience(ResumeMetadata(), JobRequirement(), TODAY).status == "pass"


def test_experience_unknown_when_resume_empty():
    result = _check_experience(ResumeMetadata(), JobRequirement(min_experience_months=36), TODAY)
    assert result.status == "unknown"


def test_experience_fail_when_short():
    result = _check_experience(
        _resume_with_months("2020-01", "2021-01"),   # 12 个月
        JobRequirement(min_experience_months=36), TODAY,
    )
    assert result.status == "fail"
    assert "12" in result.detail and "36" in result.detail


def test_experience_pass_when_enough():
    result = _check_experience(
        _resume_with_months("2018-01", "2023-01"),   # 60 个月
        JobRequirement(min_experience_months=36), TODAY,
    )
    assert result.status == "pass"


# ============================================================
# 学历
# ============================================================

def test_education_pass_when_not_required():
    assert _check_education(ResumeMetadata(), JobRequirement()).status == "pass"


def test_education_unknown_when_missing():
    assert _check_education(ResumeMetadata(), JobRequirement(min_degree_level=2)).status == "unknown"


def test_education_fail_when_lower():
    resume = ResumeMetadata(educations=(Education(degree_level=1),))
    result = _check_education(resume, JobRequirement(min_degree_level=2))
    assert result.status == "fail"
    assert "大专" in result.detail and "本科" in result.detail


def test_education_pass_when_higher():
    resume = ResumeMetadata(educations=(Education(degree_level=4),))
    assert _check_education(resume, JobRequirement(min_degree_level=2)).status == "pass"


# ============================================================
# 地点
# ============================================================

def test_location_pass_when_not_required():
    assert _check_location(ResumeMetadata(), JobRequirement()).status == "pass"


def test_location_unknown_when_resume_empty():
    resume = ResumeMetadata()
    assert _check_location(resume, JobRequirement(locations=("北京",))).status == "unknown"


def test_location_fail_when_no_overlap():
    resume = ResumeMetadata(preferred_locations=("杭州",))
    result = _check_location(resume, JobRequirement(locations=("北京",)))
    assert result.status == "fail"
    assert "杭州" in result.detail


def test_location_pass_when_fuzzy_match():
    resume = ResumeMetadata(preferred_locations=("北京市",))
    assert _check_location(resume, JobRequirement(locations=("北京",))).status == "pass"


def test_location_pass_when_any_one_matches():
    """岗位要求两个城市，候选人接受其中一个就够了 —— 不必全都接受。"""
    resume = ResumeMetadata(preferred_locations=("北京",))
    assert _check_location(resume, JobRequirement(locations=("北京", "上海"))).status == "pass"


# ============================================================
# 薪资
# ============================================================

def test_salary_pass_when_no_upper_bound():
    resume = ResumeMetadata(expected_salary="50K")
    assert _check_salary(resume, JobRequirement(salary_min=20000)).status == "pass"


def test_salary_unknown_when_unparseable():
    resume = ResumeMetadata(expected_salary="面议")
    assert _check_salary(resume, JobRequirement(salary_max=30000)).status == "unknown"


def test_salary_fail_when_expected_above_budget():
    resume = ResumeMetadata(expected_salary="35K-40K")
    result = _check_salary(resume, JobRequirement(salary_max=30000))
    assert result.status == "fail"
    assert "35000" in result.detail and "30000" in result.detail


def test_salary_pass_when_inside_budget():
    resume = ResumeMetadata(expected_salary="25K")
    assert _check_salary(resume, JobRequirement(salary_max=30000)).status == "pass"


def test_salary_pass_when_expected_below_budget():
    """要得比预算少不是问题 —— 公司不会因此拒绝候选人。"""
    resume = ResumeMetadata(expected_salary="10K")
    assert _check_salary(resume, JobRequirement(salary_min=20000, salary_max=30000)).status == "pass"


# ============================================================
# filter_resume：三种 unknown_field_policy
# ============================================================

EMPTY_RESUME = ResumeMetadata()
DEMANDING_REQ = JobRequirement(
    min_degree_level=2,
    min_experience_months=36,
    locations=("北京",),
    salary_max=30000,
)


def test_keep_passes_missing_data_but_records_it():
    """KEEP：四项全缺 → 放行，但 unknown 里记下缺了什么。"""
    result = filter_resume(
        EMPTY_RESUME, DEMANDING_REQ,
        policy=UnknownFieldPolicy.KEEP, today_ym=TODAY,
    )
    assert result.passed is True
    assert result.reasons == ()
    assert set(result.unknown) == {"工作经历", "学历", "期望地点", "期望薪资"}


def test_reject_drops_missing_data_with_reasons():
    """REJECT：四项全缺 → 淘汰，每条缺失都要有原因。"""
    result = filter_resume(
        EMPTY_RESUME, DEMANDING_REQ,
        policy=UnknownFieldPolicy.REJECT, today_ym=TODAY,
    )
    assert result.passed is False
    assert len(result.reasons) == 4
    assert any("学历" in r for r in result.reasons)


def test_keep_does_not_excuse_explicit_mismatch():
    """关键：keep 只放过"没数据"，不放过"有数据但不够"。"""
    resume = ResumeMetadata(educations=(Education(degree_level=1),))
    result = filter_resume(
        resume, JobRequirement(min_degree_level=2),
        policy=UnknownFieldPolicy.KEEP, today_ym=TODAY,
    )
    assert result.passed is False
    assert "大专" in result.reasons[0]


def test_score_only_skips_hard_filtering_entirely():
    """SCORE_ONLY：连"明确不满足"也不淘汰 —— 这一维是软性条件。"""
    resume = ResumeMetadata(educations=(Education(degree_level=1),))
    result = filter_resume(
        resume, JobRequirement(min_degree_level=4),
        policy=UnknownFieldPolicy.SCORE_ONLY, today_ym=TODAY,
    )
    assert result.passed is True
    assert result.reasons == ()
    assert result.unknown == ()


def test_multiple_failures_are_all_reported():
    """同时不满足多项时，所有原因都要给出，而不是只报第一条。"""
    resume = ResumeMetadata(
        educations=(Education(degree_level=1),),
        preferred_locations=("杭州",),
        expected_salary="40K",
    )
    result = filter_resume(
        resume, DEMANDING_REQ,
        policy=UnknownFieldPolicy.KEEP, today_ym=TODAY,
    )
    assert result.passed is False
    assert len(result.reasons) == 3   # 学历 + 地点 + 薪资，经验是 unknown 不算淘汰
    assert any("大专" in r for r in result.reasons)
    assert any("杭州" in r for r in result.reasons)
    assert any("40000" in r for r in result.reasons)


def test_all_pass():
    resume = ResumeMetadata(
        educations=(Education(degree_level=2),),
        experiences=(WorkExperience("A", "后端", DateRange("2018-01", "2023-01")),),
        preferred_locations=("北京",),
        expected_salary="25K",
    )
    result = filter_resume(
        resume, DEMANDING_REQ,
        policy=UnknownFieldPolicy.REJECT, today_ym=TODAY,
    )
    assert result.passed is True
    assert result.reasons == ()
    assert result.unknown == ()
