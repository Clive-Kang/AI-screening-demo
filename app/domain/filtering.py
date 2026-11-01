"""(Hard filtering)硬性过滤：不满足条件的候选人直接淘汰，不进入排序。

一句话定位：硬性过滤是「准入闸门」，在评分排序之前执行。
过滤回答：候选人能不能进入候选池；后面的六维评分只负责：进池之后谁排前面。

过滤处理的四个维度：
    经验 / 学历 / 地点 / 薪资

为什么技能不做硬性过滤？
    "必需技能"看起来像硬条件，但技能名有大量别名（JS / JavaScript /
    ECMAScript），硬匹配的误杀成本远高于漏杀——漏掉的人还能靠评分排上来，
    误杀的人直接从结果里消失，HR 根本不知道他的存在。技能交给评分去区分。

本模块是纯函数层：不碰数据库、不碰 LLM、不 import 任何框架。
"""

from dataclasses import dataclass

from app.domain.models import DEGREE_NAMES, FilterResult, JobRequirement, ResumeMetadata
from app.domain.policies import UnknownFieldPolicy
from app.domain.scoring import score_location
from app.domain.text import parse_salary_range


@dataclass(frozen=True)
class _CheckResult:
    """单个维度的检查结果。

    status 只有三种：
        pass     满足要求，或该维度不适用（岗位没提）
        fail     明确不满足 —— 一定淘汰，与 unknown_field_policy 无关
        unknown  简历里没有这项数据 —— 交给策略决定放行还是淘汰
    """

    status: str
    detail: str = ""


_PASS = _CheckResult("pass")


def _fail(reason: str) -> _CheckResult:
    return _CheckResult("fail", reason)


def _unknown(what: str) -> _CheckResult:
    return _CheckResult("unknown", what)


# ============================================================
# 四个维度
# ============================================================

def _check_experience(
    resume: ResumeMetadata,
    req: JobRequirement,
    today_ym: str,
) -> _CheckResult:
    """工作年限不足 → 淘汰。

    注意：这里用和评分**同一个** total_experience_months，
    所以不会出现"评分为 3 年、过滤算成 5 年"的分裂。
    """
    need = req.min_experience_months
    if not need:                       # 岗位没写年限要求
        return _PASS

    actual = resume.total_experience_months(today_ym)
    if actual <= 0:
        return _unknown("工作经历")

    if actual < need:
        return _fail(
            f"工作年限 {actual} 个月，低于要求的 {need} 个月"
        )
    return _PASS


def _check_education(resume: ResumeMetadata, req: JobRequirement) -> _CheckResult:
    """学历低于要求 → 淘汰。"""
    need = req.min_degree_level
    if need <= 0:
        return _PASS

    level = resume.highest_degree_level
    if level <= 0:
        return _unknown("学历")

    if level < need:
        return _fail(
            f"最高学历 {DEGREE_NAMES[level]}，低于要求的 {DEGREE_NAMES[need]}"
        )
    return _PASS


def _check_location(resume: ResumeMetadata, req: JobRequirement) -> _CheckResult:
    """期望地点与岗位地点完全没有交集 → 淘汰。

    复用 score_location 而不是另写一套匹配：
    它已经处理了归一化和"北京/北京市"这类模糊匹配，
    另写一份就意味着两处规则会慢慢漂移。
    """
    if not req.locations:
        return _PASS

    ratio = score_location(resume, req)
    if ratio is None:
        return _unknown("期望地点")

    if ratio <= 0:
        wanted = "/".join(resume.preferred_locations)
        offered = "/".join(req.locations)
        return _fail(f"期望地点 {wanted}，与岗位地点 {offered} 无交集")
    return _PASS


def _check_salary(resume: ResumeMetadata, req: JobRequirement) -> _CheckResult:
    """期望薪资的下限超过预算上限 → 淘汰（谈不拢）。

    只在**高于**预算时淘汰。期望远低于预算不是问题：
    公司不会因为候选人要得少而拒绝他。
    """
    if req.salary_max is None:
        # 预算没有上限（或压根没写），不存在谈不拢
        return _PASS

    expected = parse_salary_range(resume.expected_salary)
    if expected is None:
        return _unknown("期望薪资")

    exp_low, _ = expected
    if exp_low > req.salary_max:
        return _fail(
            f"期望薪资下限 {exp_low} 元，高于预算上限 {req.salary_max} 元"
        )
    return _PASS


# ============================================================
# 汇总：四个维度 + 缺失字段策略
# ============================================================

def filter_resume(
    resume: ResumeMetadata,
    req: JobRequirement,
    *,
    policy: UnknownFieldPolicy,
    today_ym: str,
) -> FilterResult:
    """跑完四个硬性条件，返回是否通过以及全部理由。

    三种策略只影响「简历没数据」这一种情况：

        KEEP        没数据 → 放行，但**有数据且不满足时照样淘汰**
        REJECT      没数据 → 淘汰，理由写清缺的是哪一项
        SCORE_ONLY  整个硬性过滤不生效，所有维度只参与打分

    第三种的语义来自 ARCHITECTURE.md §6.4：它描述的是"这一维是软性条件"，
    所以整个档案选它，就等于"不淘汰任何人，只排序"。
    """
    if policy is UnknownFieldPolicy.SCORE_ONLY:
        return FilterResult(passed=True)

    checks = (
        _check_experience(resume, req, today_ym),
        _check_education(resume, req),
        _check_location(resume, req),
        _check_salary(resume, req),
    )

    reasons = [c.detail for c in checks if c.status == "fail"]
    unknown = [c.detail for c in checks if c.status == "unknown"]

    if policy is UnknownFieldPolicy.REJECT:
        reasons.extend(f"缺少{what}信息，无法判断" for what in unknown)

    return FilterResult(
        passed=not reasons,
        reasons=tuple(reasons),
        unknown=tuple(unknown),
    )
