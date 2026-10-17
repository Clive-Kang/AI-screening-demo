"""领域模型：业务里"一份简历长什么样"的正式定义。

这些是纯数据载体 —— 不做 IO、不依赖框架、不 import adapter。
它们同时被三处使用：

    写入路径  services/ingestion   把 LLM 抽取结果装成对象再落库
    过滤路径  domain/filtering     读字段做硬性判断
    评分路径  domain/scoring       读字段打分

所以字段必须和 ARCHITECTURE.md §4.2 的表结构一一对应。
"""

from dataclasses import dataclass, field

from app.domain.text import Period, merge_experience_months, parse_year_month


@dataclass(frozen=True) #frozen=True：实例不可变，防止评分时被改权重
class DateRange:
    """一段时间的起止，粒度到月。

    三个字段合起来表达四种情况：

        start_ym='2020-07'  end_ym='2023-12'  is_current=False   已结束
        start_ym='2020-07'  end_ym=None       is_current=True    至今
        start_ym='2020-07'  end_ym=None       is_current=False   结束时间未知
        start_ym=None       ...                                  开始时间未知

    「至今」为什么不在这里换算成具体月份？
        ARCHITECTURE.md §4.4：换算会把"写入时刻的时钟"固化进数据。
        一年后再看这份简历，他会凭空多出 12 个月。长度只在查询时实时算。
    """

    start_ym: str | None = None
    end_ym: str | None = None
    is_current: bool = False

    def __post_init__(self) -> None:
        # ① 形状校验：能进来的日期必须是干净的 YYYY-MM。
        #    不是的话说明上游漏了归一化 —— 那是 bug，要在最早的地方炸掉，
        #    而不是让它带着 '2020.07' 流到评分里。
        for label, value in (("start_ym", self.start_ym), ("end_ym", self.end_ym)):
            if value is not None and parse_year_month(value) != value:
                raise ValueError(
                    f"{label}={value!r} 不是规范化的 'YYYY-MM'，"
                    "写入前必须先经过 text.parse_year_month"
                )

        # ② 语义约束：既然标了"至今"，就不该同时存在结束月份。
        #    两者同时出现时没有正确答案 —— 到底听哪个？干脆不允许存在。
        if self.is_current and self.end_ym is not None:
            raise ValueError(
                f"is_current=True 时 end_ym 必须为空，实际是 {self.end_ym!r}"
            )

    def as_period(self) -> Period:
        """转成 text.merge_experience_months 需要的三元组。"""
        return (self.start_ym, self.end_ym, self.is_current)


@dataclass(frozen=True)
class WorkExperience:
    """一段工作经历。字段对应表 resume_experience。"""

    company: str | None = None
    title: str | None = None
    period: DateRange = field(default_factory=DateRange)

#=================5b===============================
@dataclass(frozen=True)
class Education:
    """一段教育经历。字段对应表 resume_education。

    degree_raw 保留原文（"本科"/"Bachelor"/"学士"），degree_level 是归一化后的等级。
    两者都存：过滤和评分用 level 做比较，报告给 HR 看要还原成 raw。
    找不到对应等级时 level 是 0（未知）—— 这里不猜，交给 filtering 按
    unknown_field_policy 决定是放行还是淘汰（ARCHITECTURE.md §6.4）。
    """

    institution: str | None = None  # 学校名称
    major: str | None = None        # 专业名称
    degree_raw: str | None = None   # 原始学历描述（"本科"/"Bachelor"/"学士"）
    degree_level: int = 0          # 0=未知 1=大专 2=本科 3=硕士 4=博士
    period: DateRange = field(default_factory=DateRange)

    def __post_init__(self) -> None:
        if not (0 <= self.degree_level <= 4):
            raise ValueError(
                f"degree_level 必须在 0~4，实际是 {self.degree_level}"
            )


@dataclass(frozen=True)
class ResumeMetadata:
    """一份简历抽取出来的全部结构化信息。

    对应评分六维：skill / industry / location / salary / education / tag。
    原始抽取结果里没有 industry 和 tags —— 这两项是本项目新增的，
    参考项目的六维里虽然有权重，却没有对应的字段可打分。
    """

    name: str | None = None
    email: str | None = None
    phone: str | None = None

    skills: tuple[str, ...] = ()  #技能列表
    industries: tuple[str, ...] = ()  #行业列表
    preferred_locations: tuple[str, ...] = ()  #期望工作地点列表
    tags: tuple[str, ...] = ()  #标签列表

    educations: tuple[Education, ...] = ()  #教育经历列表
    experiences: tuple[WorkExperience, ...] = ()  #工作经历列表

    expected_salary: str | None = None   #期望薪资原文（"15k-20k"/"年薪 30 万"）

    @property
    def highest_degree_level(self) -> int:
        """最高学历等级；没有任何教育记录时为 0。"""
        return max((e.degree_level for e in self.educations), default=0)

    def total_experience_months(self, today_ym: str) -> int:
        """累计工作月数，多段经历重叠部分只算一次（R3 的归并逻辑）。"""
        return merge_experience_months(
            [e.period.as_period() for e in self.experiences], today_ym
        )


# ============================================================
# 评分输出
#
# 每维度返回 (score, weight, reason) 三元组，而不是一个 float。
# 参考项目在 API 层伪造成绩单（routes.py:71-91），根因就是 domain 层
# 只吐了总分，展示层拿不到依据，只能自己编明细。
# ============================================================

@dataclass(frozen=True)
class DimScore:
    """一个维度的得分。

    score 为 None 表示**该维度没有数据**，而不是"得了 0 分"。
    两者在报告里必须区分开："简历里没写期望薪资"和"期望薪资完全不匹配"
    是两件事，混在一起会让 HR 误判。
    """

    name: str
    score: float | None            # [0,1]；None = 无数据
    weight: float                  # 本次实际使用的权重
    reason: str                    # 人类可读依据

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("DimScore.name 不能为空")
        if self.score is not None and not (0.0 <= self.score <= 1.0):
            raise ValueError(
                f"{self.name}.score 必须在 [0,1] 或 None，实际是 {self.score}"
            )
        if not (0.0 <= self.weight <= 1.0):
            raise ValueError(
                f"{self.name}.weight 必须在 [0,1]，实际是 {self.weight}"
            )

    @property
    def missing(self) -> bool:
        """该维度是否因为没数据而不参与计算。"""
        return self.score is None


@dataclass(frozen=True)
class ScoreBreakdown:
    """一次评分的完整结果，也是落进 results.dim_scores_json 的内容。

    overall 为 None 表示**完全没有可评分的信息**（六维全缺失且没有语义分）。
    和 DimScore.score 保持同一套语义：None 是"没有"，0.0 是"很差"。
    """

    overall: float | None       # 最终综合总分 [0,1]；None = 无任何可用信息
    dims: tuple[DimScore, ...]  # 六维规则分的DimScore数组
    semantic: DimScore | None   # 向量语义分单独放这里（不属于六维规则维度）
    profile: str                # 使用的评分档案名称，例如 "strict" / "base"
    config_hash: str            # 评分配置文件的哈希值（关键！）

    def __post_init__(self) -> None:
        if self.overall is not None and not (0.0 <= self.overall <= 1.0):
            raise ValueError(
                f"overall 必须在 [0,1] 或 None，实际是 {self.overall}"
            )
        names = [d.name for d in self.dims]
        if len(names) != len(set(names)):
            raise ValueError(f"dims 中维度名重复：{names}")

    @property
    def coverage(self) -> float:   #信息覆盖率
        """有数据的维度占了多少权重。1.0 表示六维全都有数据。

        缺失维度被剔除出分母后，这个数必须一起暴露：
        「0.85 分，但只基于 85% 的信息」和「0.85 分，六维齐全」
        对 HR 是两回事。
        """
        return sum(d.weight for d in self.dims if not d.missing)


# ============================================================
# 岗位需求
#
# HR 的一句话需求经 LLM 解析后变成这个结构。
# 评分的每个维度都要拿它做对照物 —— 只有简历那一半是算不出匹配度的。
# ============================================================

@dataclass(frozen=True)
class JobRequirement:
    """结构化的岗位需求。字段与评分六维一一对应。

    技能为什么分「必需 / 优先」而其他维度不分？
        因为「3 年 Python，会 Go 加分」是招聘里最常见的表述，
        两者的错配代价不同：缺必需技能 = 不合格，缺优先技能 = 扣几分。
        至于行业、地点、标签，需求方很少写"必须…否则…加分"的层级，
        硬套两级反而逼着 LLM 去猜。
    """

    # ---- skill ----
    required_skills: tuple[str, ...] = ()
    preferred_skills: tuple[str, ...] = ()

    # ---- education ----
    min_degree_level: int = 0          # 0=不限 1=大专 2=本科 3=硕士 4=博士

    # ---- industry ----
    industries: tuple[str, ...] = ()

    # ---- location ----
    locations: tuple[str, ...] = ()

    # ---- salary ----
    salary_min: int | None = None      # 月薪，单位：元
    salary_max: int | None = None

    # ---- tag ----
    tags: tuple[str, ...] = ()

    # ---- 经验（同时参与过滤与打分）----
    # 用「月」而不是「年」：R3 已经做到月粒度，这里再退回年就白算了。
    # 参考项目按年比较，把 3 年 11 个月和 3 年 0 个月当成同一档。
    min_experience_months: int | None = None

    #`@dataclass`的`__post_init__`：做数据合法性校验，保证构造出来的实例一定满足业务约束
    def __post_init__(self) -> None: 
        if not (0 <= self.min_degree_level <= 4):
            raise ValueError(
                f"min_degree_level 必须在 0~4，实际是 {self.min_degree_level}"
            )
        if self.min_experience_months is not None and self.min_experience_months < 0:
            raise ValueError(
                f"min_experience_months 不能为负，实际是 {self.min_experience_months}"
            )
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            raise ValueError(
                f"salary_min={self.salary_min} 大于 salary_max={self.salary_max}"
            )
