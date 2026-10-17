"""app/domain/models.py 的单测。

模型层只测两件事：
  1) 不变量成立时能正常构造；
  2) 违反不变量的输入必须在构造时就报错，而不是带着脏数据流到下游。
"""

import pytest

from app.domain.models import (
    DateRange,
    DimScore,
    Education,
    ResumeMetadata,
    ScoreBreakdown,
    WorkExperience,
)
from app.domain.text import merge_experience_months


# ============================================================
# DateRange
# ============================================================

def test_date_range_finished():
    period = DateRange("2020-07", "2023-12", False)
    assert period.as_period() == ("2020-07", "2023-12", False)


def test_date_range_current():
    period = DateRange("2020-07", None, True)
    assert period.as_period() == ("2020-07", None, True)


def test_date_range_end_unknown_is_allowed():
    """结束时间未知且不是在職：合法状态，交给上层按 unknown_field_policy 决定。"""
    assert DateRange("2020-07", None, False).is_current is False


def test_date_range_current_with_end_is_rejected():
    with pytest.raises(ValueError, match="is_current=True"):
        DateRange("2020-07", "2023-12", True)


@pytest.mark.parametrize("bad", ["2020.07", "2020-7", "2020年7月", "abc", "2020-13"])
def test_date_range_rejects_non_normalized_dates(bad):
    """模型只收 YYYY-MM —— 归一化是 text.parse_year_month 的活，不能绕过去。"""
    with pytest.raises(ValueError, match="YYYY-MM"):
        DateRange(bad, None, False)


def test_date_range_is_immutable():
    period = DateRange("2020-07", "2023-12", False)
    with pytest.raises(Exception):
        period.start_ym = "2021-01"  # type: ignore[misc]


# ============================================================
# WorkExperience
# ============================================================

def test_work_experience_defaults():
    exp = WorkExperience()
    assert exp.company is None
    assert exp.title is None
    assert exp.period == DateRange()


def test_work_experience_feeds_merge():
    """模型对象能直接喂给 R3 的归并函数 —— 这就是 5a 存在的意义。"""
    experiences = [
        WorkExperience("A 公司", "后端", DateRange("2020-01", "2020-12", False)),
        WorkExperience("B 公司", "后端", DateRange("2020-06", "2021-06", False)),
    ]
    total = merge_experience_months(
        [e.period.as_period() for e in experiences], "2026-10"
    )
    assert total == 17  # 重叠部分只算一次


# ============================================================
# Education
# ============================================================

def test_education_keeps_raw_and_level():
    edu = Education(
        institution="某大学", major="计算机",
        degree_raw="Bachelor", degree_level=2,
    )
    assert edu.degree_raw == "Bachelor"
    assert edu.degree_level == 2


@pytest.mark.parametrize("bad", [-1, 5, 99])
def test_education_rejects_degree_level_out_of_range(bad):
    with pytest.raises(ValueError, match="degree_level"):
        Education(degree_level=bad)


# ============================================================
# ResumeMetadata
# ============================================================

def test_empty_metadata_is_valid():
    """全字段缺失是合法的 —— 缺失不是错误，怎么处理由策略决定。"""
    meta = ResumeMetadata()
    assert meta.highest_degree_level == 0
    assert meta.total_experience_months("2026-10") == 0


def test_highest_degree_level_picks_max():
    meta = ResumeMetadata(educations=(
        Education(degree_raw="本科", degree_level=2),
        Education(degree_raw="硕士", degree_level=3),
        Education(degree_raw="大专", degree_level=1),
    ))
    assert meta.highest_degree_level == 3


def test_total_experience_months_merges_overlaps():
    meta = ResumeMetadata(experiences=(
        WorkExperience("A", "后端", DateRange("2020-01", "2020-12")),
        WorkExperience("B", "后端", DateRange("2020-06", "2021-06")),
        WorkExperience("C", "后端", DateRange("2023-01", None, True)),
    ))
    # 2020-01~2021-06 = 17 个月，2023-01~2026-10 = 45 个月
    assert meta.total_experience_months("2026-10") == 62


def test_metadata_collections_are_immutable():
    """tuple 而不是 list：frozen 的壳里装 list 等于假冻结。"""
    meta = ResumeMetadata(skills=("Python",))
    assert isinstance(meta.skills, tuple)
    with pytest.raises(AttributeError):
        meta.skills.append("Java")  # type: ignore[attr-defined]


# ============================================================
# DimScore / ScoreBreakdown
# ============================================================

def test_dim_score_with_data():
    d = DimScore("skill", 0.8, 0.4, "命中必需技能 3/4")
    assert d.missing is False


def test_dim_score_none_means_missing():
    """None 是「没有数据」，0.0 是「完全不匹配」—— 两者不能混。"""
    d = DimScore("salary", None, 0.10, "简历未写期望薪资")
    assert d.missing is True


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_dim_score_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        DimScore("skill", bad, 0.4, "x")


def test_breakdown_coverage_drops_missing_dims():
    """缺失维度不占分母，coverage 告诉你这份分数基于多少信息。"""
    breakdown = ScoreBreakdown(
        overall=0.876,
        dims=(
            DimScore("skill", 0.9, 0.40, "ok"),
            DimScore("education", 0.9, 0.15, "ok"),
            DimScore("tag", 0.8, 0.10, "ok"),
            DimScore("location", None, 0.15, "未写期望地点"),
            DimScore("industry", None, 0.10, "无法识别"),
            DimScore("salary", None, 0.10, "未写期望薪资"),
        ),
        semantic=DimScore("semantic", 0.75, 0.20, "相似度 0.75"),
        profile="tech",
        config_hash="abc123",
    )
    assert breakdown.coverage == pytest.approx(0.65)


def test_breakdown_coverage_full_when_all_dims_present():
    breakdown = ScoreBreakdown(
        overall=0.7,
        dims=tuple(
            DimScore(n, 0.7, w, "ok")
            for n, w in (("skill", 0.4), ("education", 0.15), ("location", 0.15),
                         ("industry", 0.1), ("salary", 0.1), ("tag", 0.1))
        ),
        semantic=None,
        profile="tech",
        config_hash="abc123",
    )
    assert breakdown.coverage == pytest.approx(1.0)


def test_breakdown_rejects_duplicate_dim_names():
    with pytest.raises(ValueError, match="重复"):
        ScoreBreakdown(
            overall=0.5,
            dims=(DimScore("skill", 0.5, 0.5, "a"), DimScore("skill", 0.5, 0.5, "b")),
            semantic=None, profile="tech", config_hash="x",
        )


@pytest.mark.parametrize("bad", [-0.01, 1.01])
def test_breakdown_rejects_out_of_range_overall(bad):
    with pytest.raises(ValueError, match="overall"):
        ScoreBreakdown(overall=bad, dims=(), semantic=None, profile="tech", config_hash="x")
