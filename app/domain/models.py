"""领域模型：业务里"一份简历长什么样"的正式定义。(该怎么清洗拿到的数据)

这些是纯数据载体 —— 不做 IO、不依赖框架、不 import adapter。
它们同时被三处使用：

    写入路径  services/ingestion   把 LLM 抽取结果装成对象再落库
    过滤路径  domain/filtering     读字段做硬性判断
    评分路径  domain/scoring       读字段打分

所以字段必须和 ARCHITECTURE.md §4.2 的表结构一一对应。
"""

from dataclasses import dataclass, field
from enum import Enum

from app.domain.text import Period, merge_experience_months, parse_year_month

# 学历等级的中文名。评分和过滤都要用它生成人话理由，
# 所以放在 models 而不是某个模块内部 —— 两边各写一份迟早会对不上。
DEGREE_NAMES = {0: "未知", 1: "大专", 2: "本科", 3: "硕士", 4: "博士"}


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


# ============================================================
# 硬性过滤的输出
# ============================================================

@dataclass(frozen=True)
class FilterResult:
    """一次硬性过滤的结果。

    被淘汰的候选人**也要留下来**，并且必须带上原因 ——
    参考项目直接丢弃被过滤者，导致"为什么这个人没出现"完全无法回答
    （ARCHITECTURE.md §6.4）。
    """

    passed: bool
    reasons: tuple[str, ...] = ()   # 淘汰原因；passed=True 时为空
    unknown: tuple[str, ...] = ()   # 哪些维度因为没数据而无法判断

    def __post_init__(self) -> None:
        # 不变量：淘汰必须给得出解释。
        # 没有理由的淘汰等于黑箱，而"为什么没通过"正是 HR 最常问的问题。
        if self.passed and self.reasons:
            raise ValueError("passed=True 时不应该有淘汰原因")
        if not self.passed and not self.reasons:
            raise ValueError("passed=False 时必须给出至少一条原因")


# ============================================================
# 简历本体
#
# ResumeMetadata 只是"简历里抽出来的结构化信息"，
# 一份简历还有文件、原文、处理状态这些外围属性 —— 那些在这里。
# 两者合起来才是 resumes 表的一行（ARCHITECTURE.md §4.2）。
# ============================================================

class ResumeStatus(str, Enum):
    """简历走到流水线的哪一步了。

    继承 str 是为了能直接塞进 SQLite 的 TEXT 列、也能被 JSON 序列化，
    不用每次 `.value` 一下。参考项目没有状态字段，前端只能靠"在不在
    内存字典里"猜，重启后就全丢了。
    """

    QUEUED = "queued"        # 排队:收到文件，还没解析
    PARSED = "parsed"        # 已解析:PDF → 纯文本
    EXTRACTED = "extracted"  # 已提取:文本 → ResumeMetadata
    INDEXED = "indexed"      # 已入库(可被检索):向量已入库，可被检索
    FAILED = "failed"        # 已失败:任一环节失败，原因在 error 里


@dataclass(frozen=True)
class Resume:
    """一份简历的完整状态。

    raw_text 和 metadata 为什么可以是 None？
        因为上传是异步的（ARCHITECTURE.md §5.5）：刚落到 queued 状态的
        简历还没解析、更没抽取。用一个"空的 ResumeMetadata()"冒充
        已抽取，会让下游分不清"这人什么都没写"和"我们还没看"。

    id 由是谁生成的？
        仓储层生成（sqlite_repo），domain 只管它是个字符串。
        领域层不 import uuid，保持零环境依赖。
    """

    id: str
    filename: str
    content_sha256: str                  # 文本哈希值:用于去重；同一份文件只入库一次
    raw_text: str | None = None
    metadata: ResumeMetadata | None = None
    blob_path: str | None = None         # 原始文件在磁盘上的位置
    status: ResumeStatus = ResumeStatus.QUEUED
    error: str | None = None             # status=FAILED 时的原因
    created_at: str = ""                 # ISO 格式字符串，由仓储层填
    # 抽取这份简历用的是哪一版 prompt。换模型/改 prompt 后，
    # 能区分"结果不一样是因为简历变了，还是因为 prompt 变了"。
    extractor_prompt_version: str = "unknown"

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Resume.id 不能为空")
        if not self.content_sha256:
            raise ValueError("Resume.content_sha256 不能为空")
        # 不变量：状态和内容必须自洽。
        # 否则会出现"状态是 indexed，但根本没有元数据"这种没法排查的行。
        if self.status in (ResumeStatus.EXTRACTED, ResumeStatus.INDEXED):
            if self.metadata is None:
                raise ValueError(f"status={self.status.value} 但 metadata 为空")
        if self.status is ResumeStatus.FAILED and not self.error:
            raise ValueError("status=failed 时必须给出 error")


# ============================================================
# 一次筛选任务（run）
#
# 对应 runs 表（ARCHITECTURE.md §4.2）。它记录的是「这一次筛选跑到哪了」，
# 不记录结果 —— 结果是 results 表，按 (run_id, rank) 单独存。
# 分开的理由：进度要频繁更新（每阶段一次），结果只在最后写一次，
# 混在一行里会让每次更新进度都重写一遍几十 KB 的 JSON。
# ============================================================

class RunStatus(str, Enum):
    """一次筛选任务的生命周期。

    PARTIAL 是参考项目没有的状态：analyze 阶段因为 token 预算超限被中止，
    但召回/过滤/评分的结果是有效的，不该整体判失败。
    """

    QUEUED = "queued"        # 已接受，等 worker 取
    RUNNING = "running"      # 正在跑，stage 字段说明跑到哪一步
    SUCCEEDED = "succeeded"  # 全流程完成
    PARTIAL = "partial"      # 部分成功:主流程完成，analyze 阶段未跑完
    FAILED = "failed"        # 出错，reason 在 error 里
    CANCELLED = "cancelled"  # 用户 DELETE 掉的


class RunStage(str, Enum):
    """run 内部的阶段，用于前端轮询显示进度。"""

    RECALL = "recall"      # 向量召回
    FILTER = "filter"      # 硬性过滤
    SCORE = "score"        # 评分排序
    ANALYZE = "analyze"    # LLM 生成报告
    DONE = "done"          # 全部结束


@dataclass(frozen=True)
class Run:
    """一次筛选任务的状态快照。

    这里没有 results —— 结果按候选人一行存，和 run 是一对多。
    把结果塞进 Run 意味着查一次进度要把所有候选人的报告都读出来。

    progress 为什么是 tuple 而不是 dict？
        同 ResumeDoc.vector：frozen dataclass 里的 dict 照样能被改。
        用 ((阶段名, 计数), ...) 保存，读的时候 dict(run.progress) 转回去。
    """

    id: str                               #run 唯一 ID，主键
    query_text: str                       # HR 那句原始需求
    profile: str                          # 使用哪个评分档案（tech / strict …）
    status: RunStatus = RunStatus.QUEUED  #RunStatus 枚举，任务整体状态
    stage: RunStage | None = None         #RunStage 枚举，当前执行阶段
    idempotency_key: str = ""             #幂等 key 用 query、profile、简历库版本做 sha256。防止重复提交完全相同的筛选请求
    config_snapshot_json: str = "{}"      #本次生效的完整配置，用于事后复算.以后配置文件 (scoring.yaml) 改了，依然可以拿这份快照**复现本次打分结果**，用于审计、对比。
    progress: tuple[tuple[str, int], ...] = ()  #进度元组
    recall_n: int = 0                     #各阶段数量统计：召回多少、      
    filtered_n: int = 0                   #过滤剩下多少
    scored_n: int = 0                     #打分多少
    analyzed_n: int = 0                   #LLM 生成报告多少
    error: str | None = None              #失败原因，只有 FAILED 状态才需要填
    created_at: str = ""                  #任务创建时间
    finished_at: str | None = None        #任务结束时间，只有 SUCCEEDED / PARTIAL / FAILED / CANCELLED 状态才需要填

    def __post_init__(self) -> None:  #实例创建后校验逻辑
        if not self.id:
            raise ValueError("Run.id 不能为空")
        if not self.query_text.strip():
            raise ValueError("Run.query_text 不能为空")
        if not self.profile:
            raise ValueError("Run.profile 不能为空")
        if self.status is RunStatus.FAILED and not self.error:
            raise ValueError("status=failed 时必须给出 error")
        # 已结束的状态必须有结束时间，否则前端永远显示"进行中"
        if self.status in (
            RunStatus.SUCCEEDED, RunStatus.PARTIAL,
            RunStatus.FAILED, RunStatus.CANCELLED,
        ) and not self.finished_at:
            raise ValueError(f"status={self.status.value} 时必须给出 finished_at")
