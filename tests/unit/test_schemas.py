"""wire 层（pydantic DTO → 领域对象）的单测。

这一层的职责是"接住模型的各种坏习惯"。所以用例几乎全是
**模型真实会返回的脏数据**，而不是理想输入。
"""

import pytest
from pydantic import ValidationError

from app.domain.models import DateRange, JobRequirement, ResumeMetadata
from app.services.schemas import (
    ExtractedJobRequirement,
    ExtractedResume,
    WireEducation,
    WirePeriod,
)


# ============================================================
# 日期清洗：这是本层最要紧的事
# ============================================================

def test_current_marker_moves_into_is_current():
    """模型把"至今"塞进 end_ym 是最常见的输出形态，要顺着它改写。"""
    p = WirePeriod(start_ym="2020-07", end_ym="至今")

    assert p.is_current is True
    assert p.end_ym is None


def test_year_only_is_padded_to_january():
    assert WirePeriod(start_ym="2020").start_ym == "2020-01"


def test_dot_date_is_normalized():
    assert WirePeriod(start_ym="2020.07").start_ym == "2020-07"


@pytest.mark.parametrize("bad", ["不是日期", "abc-2020"])
def test_unparseable_date_raises(bad):
    """认不出来就报错 → extract 会重试，而不是把脏数据放进领域对象。"""
    with pytest.raises(ValidationError):
        WirePeriod(start_ym=bad)


def test_current_with_end_raises():
    with pytest.raises(ValidationError):
        WirePeriod(start_ym="2020-01", end_ym="2023-12", is_current=True)


def test_empty_strings_become_none():
    p = WirePeriod(start_ym=" ", end_ym="")
    assert p.start_ym is None and p.end_ym is None


# ============================================================
# 列表清洗
# ============================================================

def test_null_lists_become_empty_tuples():
    m = ExtractedResume(name=None, skills=None, educations=None, experiences=None)

    assert m.skills == ()
    assert m.educations == ()


def test_comma_string_is_split_and_deduped():
    m = ExtractedResume(skills="Python, FastAPI, Python,  ")
    assert m.skills == ("Python", "FastAPI")


def test_empty_string_fields_become_none():
    m = ExtractedResume(name="", email="  ")
    assert m.name is None
    assert m.email is None


def test_degree_level_out_of_range_raises():
    with pytest.raises(ValidationError):
        WireEducation(degree_level=9)


# ============================================================
# to_domain
# ============================================================

def test_resume_to_domain_produces_domain_objects():
    m = ExtractedResume(
        name="张三",
        skills=["Python"],
        educations=[{"institution": "某大学", "degree_level": 2}],
        experiences=[
            {"company": "某公司", "title": "后端", "period": {"start_ym": "2020.07", "end_ym": "至今"}}
        ],
    ).to_domain()

    assert isinstance(m, ResumeMetadata)
    assert m.skills == ("Python",)
    assert m.highest_degree_level == 2

    exp = m.experiences[0]
    assert isinstance(exp.period, DateRange)
    assert exp.period.start_ym == "2020-07"
    assert exp.period.is_current is True


def test_missing_period_uses_default():
    m = ExtractedResume(experiences=[{"company": "某公司"}]).to_domain()
    assert m.experiences[0].period == DateRange()


def test_requirement_to_domain():
    req = ExtractedJobRequirement(
        required_skills=["Python"],
        min_degree_level=2,
        salary_min=15000,
        salary_max=20000,
        min_experience_months=36,
    ).to_domain()

    assert isinstance(req, JobRequirement)
    assert req.salary_min == 15000


def test_swapped_salary_range_is_fixed_not_rejected():
    """上下限填反的信息量不在顺序上，交换比重试划算。"""
    req = ExtractedJobRequirement(salary_min=20000, salary_max=15000)
    assert (req.salary_min, req.salary_max) == (15000, 20000)


def test_negative_experience_raises():
    with pytest.raises(ValidationError):
        ExtractedJobRequirement(min_experience_months=-1)
