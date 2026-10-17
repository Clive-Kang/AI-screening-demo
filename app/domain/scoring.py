"""六维评分：简历 × 岗位需求 → 每个维度一个得分。

统一约定 —— 所有 score_* 函数返回 float | None：

    0.0 ~ 1.0   有数据、可评分
    None        这一维没有可比的信息（简历没写，或需求未提）

「没数据」必须和「不匹配」分开。混成一个数，写得少的人就天然吃亏，
这正是 ARCHITECTURE.md §6.3 规则 4 要解决的问题。

本模块只算**单维得分**，不做加权汇总；
汇总与剔除分母在 combine() 里（R6d），那里才知道权重。
"""

from app.domain.models import (
    DimScore,
    JobRequirement,
    ResumeMetadata,
    ScoreBreakdown,
)
from app.domain.policies import ScoringProfile
from app.domain.text import parse_salary_range


def _norm(text: str) -> str:  #作用：统一技能文本格式，消除大小写、空格干扰。
    """技能/标签比较前的归一化：小写、去掉空格。

    输出 'Python 3' 和 'python3' 归一后都是 'python3'。
    """
    return text.strip().lower().replace(" ", "")


def _token_hit(wanted: str, pool: set[str], min_len: int = 3) -> bool: #技能命中判断
    """需求里的一个词（技能/标签/城市），是否出现在候选人的集合里。

    为什么不能直接写 `wanted in skill`？
        因为 `"go" in "django"` 是 True —— 参考项目正是这么写的，
        结果所有写 Django 的人都被判成"会 Go"。

    所以子串匹配加两个限制：
      1) 短的那个至少 min_len 个字符（技能默认 3，"go" 只能精确匹配；
         城市名多为 2 字，调用方传 2）
      2) 长度比不低于 0.5（"java" 不命中 "javascript"，但它俩确实不是一个东西）
    """
    if wanted in pool:
        return True

    for token in pool:
        shorter, longer = sorted((wanted, token), key=len)
        if len(shorter) < min_len:
            continue
        if shorter in longer and len(shorter) / len(longer) >= 0.5:
            return True
    return False


def score_skill(resume: ResumeMetadata, req: JobRequirement) -> float | None:
    """技能匹配度。

    必需技能占 0.8、优先技能占 0.2 —— 与参考项目的权重一致。
    但只对**需求里真的写了的那一类**计权：
        只写必需 → 它就是 100%
        只写优先 → 它也是 100%
    """
    #简历技能做归一化，存入集合`pool`；简历完全无技能 → 返回None
    #`pool`：候选人**归一化后的技能集合**
    pool = {_norm(s) for s in resume.skills if _norm(s)}
    if not pool:
        return None

    #同样做归一 + 过滤空值，转成列表。
    required = [_norm(s) for s in req.required_skills if _norm(s)]
    preferred = [_norm(s) for s in req.preferred_skills if _norm(s)]
    if not required and not preferred:
        return None  #如果岗位既没有必需、也没有优先技能要求 → 返回`None`

    def rate(items: list[str]) -> float:
        return sum(1 for s in items if _token_hit(s, pool)) / len(items)
        #内部函数`rate()`：计算**命中率**，命中数量 ÷ 总需求技能数量，值域`[0,1]`
    if required and preferred:
        return rate(required) * 0.8 + rate(preferred) * 0.2 
    
    return rate(required or preferred)


def score_education(resume: ResumeMetadata, req: JobRequirement) -> float | None:
    """学历匹配度 = 实际等级 / 要求等级，封顶 1.0。

    博士(4) 应聘本科(2) 的要求 → 2.0，封顶成 1.0，不额外加分。
    大专(1) 对本科(2) → 0.5，而不是 0：读了大专确实比没读强，但不够。
    """
    if req.min_degree_level <= 0:      # 需求不限学历
        return None

    level = resume.highest_degree_level
    if level <= 0:
        # 有教育记录但等级识别不出来（或压根没有）→ 当没数据，
        # 交给 unknown_field_policy 决定，而不是在这里判 0 分。
        return None

    return min(1.0, level / req.min_degree_level)


def score_experience(  #工作经验维度打分 
    resume: ResumeMetadata,
    req: JobRequirement,
    today_ym: str,
) -> float | None:
    """经验匹配度 = 实际月数 / 要求月数，封顶 1.0。

    today_ym 必须传进来：在職经历（"2020-07 至今"）的长度依赖今天，
    不能在这里调 datetime.now()（理由同 text.merge_experience_months）。
    """
    need = req.min_experience_months
    if not need:                       # None 或 0 都表示不限
        return None

    actual = resume.total_experience_months(today_ym)
    if actual <= 0:
        # 一段经历都解析不出来 → 没数据。不是 0 分：
        # "没写工作经历"和"工作了 0 个月"是两件事。
        return None

    return min(1.0, actual / need)


# ============================================================
# 其余四维：地点 / 行业 / 薪资 / 标签
# ============================================================

def _match_rate(wanted: list[str], pool: set[str], min_len: int = 3) -> float:
    """需求里有多少比例的词出现在候选人集合里。

    调用前必须保证 wanted 非空，否则会除零。
    """
    return sum(1 for w in wanted if _token_hit(w, pool, min_len)) / len(wanted)


def score_location(resume: ResumeMetadata, req: JobRequirement) -> float | None:
    """地点匹配度：岗位要求的地点里，有多少在候选人的期望地点中。

    城市名通常只有 2 个字（"北京"），所以 min_len 传 2 ——
    技能那套默认 3 字的规则会把所有城市名都挡在模糊匹配之外。
    """
    if not req.locations:
        return None
    pool = {_norm(loc) for loc in resume.preferred_locations if _norm(loc)}
    if not pool:
        return None

    wanted = [_norm(loc) for loc in req.locations if _norm(loc)]
    return _match_rate(wanted, pool, min_len=2)


def score_industry(resume: ResumeMetadata, req: JobRequirement) -> float | None:
    """行业匹配度。行业由 LLM 从公司名/职位推断，不要求简历明写。"""
    if not req.industries:
        return None
    pool = {_norm(name) for name in resume.industries if _norm(name)}
    if not pool:
        return None

    wanted = [_norm(name) for name in req.industries if _norm(name)]
    return _match_rate(wanted, pool)


def score_tag(resume: ResumeMetadata, req: JobRequirement) -> float | None:
    """标签匹配度。

    标签是系统生成的（"5 年以上"、"大厂背景"、"管理经验"），
    不是从简历原文里找的 —— 所以这里比较的是两套同一来源的标签体系。
    """
    if not req.tags:
        return None
    pool = {_norm(tag) for tag in resume.tags if _norm(tag)}
    if not pool:
        return None

    wanted = [_norm(tag) for tag in req.tags if _norm(tag)]
    return _match_rate(wanted, pool)


def score_salary(resume: ResumeMetadata, req: JobRequirement) -> float | None:
    """薪资匹配度：候选人期望区间有多少落在岗位预算区间内。

        岗位 20-30K，期望 25K     → 1.0   完全落在预算内
        岗位 20-30K，期望 20-40K  → 0.5   一半在预算内
        岗位 20-30K，期望 35-40K  → 0.0   完全不重叠

    预算只写了一端（"20K 以上"）时，另一端视为无界。
    """
    if req.salary_min is None and req.salary_max is None:
        return None

    expected = parse_salary_range(resume.expected_salary)
    if expected is None:
        # 没写、写"面议"、写年薪 → 都算没数据。
        # 注意这不是 0 分：不写薪资的人不该因此被扣分。
        return None

    low = req.salary_min if req.salary_min is not None else 0
    high = req.salary_max if req.salary_max is not None else 10**6
    exp_low, exp_high = expected

    if exp_high < low or exp_low > high:
        return 0.0

    span = exp_high - exp_low
    if span == 0:
        # 期望是个单点（"25K"），落在预算内就是完全匹配
        return 1.0

    overlap = min(exp_high, high) - max(exp_low, low)
    return min(1.0, overlap / span)


# ============================================================
# 汇总：六个维度 → ScoreBreakdown
#
# 这里才第一次知道权重。单维打分函数不碰权重，是为了让它们可以被
# 单独测试：改一个权重不该让某个维度的单元测试全部重写。
# ============================================================

# 固定的输出顺序。与 scoring.yaml 里的书写顺序无关 ——
# 报告要能在任何一次运行之间逐行对比。
_DIM_ORDER = ("skill", "education", "industry", "location", "salary", "tag")

_DIM_FUNCS = {
    "skill": score_skill,
    "education": score_education,
    "industry": score_industry,
    "location": score_location,
    "salary": score_salary,
    "tag": score_tag,
}

_DEGREE_NAMES = {0: "未知", 1: "大专", 2: "本科", 3: "硕士", 4: "博士"}


def _requirement_absent(name: str, req: JobRequirement) -> bool:
    """这一维的要求是不是压根没写。"""
    return {
        "skill": not (req.required_skills or req.preferred_skills),
        "education": req.min_degree_level <= 0,
        "industry": not req.industries,
        "location": not req.locations,
        "salary": req.salary_min is None and req.salary_max is None,
        "tag": not req.tags,
    }[name]


def _reason(
    name: str,
    resume: ResumeMetadata,
    req: JobRequirement,
    score: float | None,
) -> str:
    """生成一句人类可读的依据，落进 results 和 API 响应里。

    这是 ARCHITECTURE.md §6.3 规则 2 要求的：分数要能解释，
    而不是只在展示层编一个理由出来（参考项目 routes.py:71-91 的做法）。
    """
    if score is None:
        return "岗位未要求" if _requirement_absent(name, req) else "简历无此类数据"

    if name == "skill":
        pool = {_norm(s) for s in resume.skills if _norm(s)}
        hit_req = sum(1 for s in req.required_skills if _token_hit(_norm(s), pool))
        hit_pre = sum(1 for s in req.preferred_skills if _token_hit(_norm(s), pool))
        parts = []
        if req.required_skills:
            parts.append(f"必需技能命中 {hit_req}/{len(req.required_skills)}")
        if req.preferred_skills:
            parts.append(f"优先技能命中 {hit_pre}/{len(req.preferred_skills)}")
        return "，".join(parts)

    if name == "education":
        actual = resume.highest_degree_level
        need = req.min_degree_level
        return (
            f"最高学历 {_DEGREE_NAMES[actual]}（{actual}），"
            f"要求 {_DEGREE_NAMES[need]}（{need}）"
        )

    if name == "location":
        pool = {_norm(x) for x in resume.preferred_locations if _norm(x)}
        hit = sum(1 for x in req.locations if _token_hit(_norm(x), pool, min_len=2))
        return f"地点命中 {hit}/{len(req.locations)}"

    if name == "industry":
        pool = {_norm(x) for x in resume.industries if _norm(x)}
        hit = sum(1 for x in req.industries if _token_hit(_norm(x), pool))
        return f"行业命中 {hit}/{len(req.industries)}"

    if name == "tag":
        pool = {_norm(x) for x in resume.tags if _norm(x)}
        hit = sum(1 for x in req.tags if _token_hit(_norm(x), pool))
        return f"标签命中 {hit}/{len(req.tags)}"

    if name == "salary":
        expected = parse_salary_range(resume.expected_salary)
        low = str(req.salary_min) if req.salary_min is not None else "不限"
        high = str(req.salary_max) if req.salary_max is not None else "不限"
        return f"期望 {expected[0]}-{expected[1]} 元，预算 {low}-{high} 元"

    return f"得分 {score:.2f}"


def combine(
    resume: ResumeMetadata,
    req: JobRequirement,
    profile: ScoringProfile,
    *,
    profile_name: str,
    semantic_score: float | None = None,
    config_hash: str = "",
) -> ScoreBreakdown:
    """把六个维度和语义分合成一个 ScoreBreakdown。

    两条公式（ARCHITECTURE.md §6.3 规则 1 / 规则 4）：

        dims_score = Σ(score_i × w_i) / Σ(w_i)     仅累加有值的维度
        overall    = dims_score × (1 - w_semantic) + semantic × w_semantic

    语义分缺失时，overall 就只由六维决定；
    六维全缺失时，overall 就等于语义分（此时语义是唯一的信息来源）；
    两者都没有，overall 为 None。
    """
    dims: list[DimScore] = []
    for name in _DIM_ORDER:
        value = _DIM_FUNCS[name](resume, req)
        dims.append(DimScore(
            name=name,
            score=value,
            weight=profile.dims[name].weight,
            reason=_reason(name, resume, req, value),
        ))

    # ---- 剔除分母：只累加有数据的维度 ----
    active_weight = sum(d.weight for d in dims if not d.missing)
    if active_weight > 0:
        dims_score = (
            sum(d.score * d.weight for d in dims if not d.missing) / active_weight
        )
    else:
        dims_score = None

    # ---- 与语义混合 ----
    w = profile.semantic.weight
    semantic = (
        DimScore("semantic", semantic_score, w, f"向量相似度 {semantic_score:.3f}")
        if semantic_score is not None
        else None
    )

    if dims_score is None and semantic_score is None:
        overall = None
    elif dims_score is None:
        overall = semantic_score
    elif semantic_score is None:
        overall = dims_score
    else:
        overall = dims_score * (1 - w) + semantic_score * w

    return ScoreBreakdown(
        overall=overall,
        dims=tuple(dims),
        semantic=semantic,
        profile=profile_name,
        config_hash=config_hash,
    )
