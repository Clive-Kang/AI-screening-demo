"""LLM 输出的 wire 格式：pydantic 模型 → 领域对象。

为什么需要这一层
----------------
    domain/models.py 里全是 `@dataclass(frozen=True)`，校验写在 __post_init__；
    而 LLMPort.extract 要的是 pydantic BaseModel（用它生成 JSON Schema、
    用它校验模型回复）。两者接不上。

为什么不是"把领域模型改成 pydantic"
------------------------------------
    那样领域层就永久绑定 pydantic，而且 dataclass 的不可变语义、报错文案、
    所有既有单测都得跟着改。更重要的是概念上不对：
    **LLM 的输出格式是外部契约，不是领域真相**。
    模型完全可能返回 "2020/07"、"至今"、空字符串 —— 这些是"脏输入"，
    该在边界上被清洗和拒绝，而不是让领域对象去兼容模型的坏习惯。

这一层做的三件事
----------------
1. 宽松接收：空字符串 → None，null 列表 → ()，"至今" → is_current=True
2. 严格校验：日期形状、枚举范围、必填关系；不合法就 ValueError → 触发 extract 重试
3. to_domain()：转成干净的 dataclass，之后领域层再也见不到 pydantic
"""

from pydantic import BaseModel, Field, model_validator

from app.domain.models import (
    DateRange,
    Education,
    JobRequirement,
    ResumeMetadata,
    WorkExperience,
)
from app.domain.text import is_current_marker, parse_year_month


# ============================================================
# 通用清洗
# ============================================================

def _clean_str(value):
    """空字符串 / 纯空白 → None。

    模型经常用 "" 表示"没有"，直接落库会让下游的 `if x` 判断变成
    "字段存在但为空"，比 None 更难处理。
    """
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _clean_tuple(value):
    if value is None:
        return ()
    if isinstance(value, str):
        # 模型偶尔把数组写成逗号分隔的字符串
        value = value.split(",")
    seen, out = set(), []
    for item in value:
        item = item.strip() if isinstance(item, str) else item
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _normalize_ym(value, label: str):
    """把各种日期写法归一成 YYYY-MM；认不出来就报错（触发重试）。

    注意和领域层的分工：这里**归一化**，DateRange 只**接受**已归一的字符串。
    参考项目是让脏数据一路流到评分里再出怪结果，正好反过来。
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    normalized = parse_year_month(value)
    if normalized is None:
        raise ValueError(f"{label}={value!r} 无法解析成 YYYY-MM")
    return normalized


# ============================================================
# 简历
# ============================================================

class WirePeriod(BaseModel):
    start_ym: str | None = None
    end_ym: str | None = None
    is_current: bool = False

    @model_validator(mode="after")
    def _normalize(self):
        self.start_ym = _normalize_ym(self.start_ym, "start_ym")

        # 模型常常把 "至今" 塞进 end_ym 而不是置 is_current —— 顺着它改写，
        # 而不是报错重试：这是最常见的输出形态，值得宽容。
        if is_current_marker(self.end_ym):
            self.end_ym = None
            self.is_current = True

        self.end_ym = _normalize_ym(self.end_ym, "end_ym")
        if self.is_current and self.end_ym is not None:
            # 两者同时存在时没有正确答案，交给领域层之前就拒绝
            raise ValueError("is_current=true 时 end_ym 必须为空")
        return self

    def to_domain(self) -> DateRange:
        return DateRange(
            start_ym=self.start_ym, end_ym=self.end_ym, is_current=self.is_current
        )


class WireExperience(BaseModel):
    company: str | None = None
    title: str | None = None
    period: WirePeriod = Field(default_factory=WirePeriod)

    @model_validator(mode="before")
    @classmethod
    def _clean(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            data["company"] = _clean_str(data.get("company"))
            data["title"] = _clean_str(data.get("title"))
        return data

    def to_domain(self) -> WorkExperience:
        return WorkExperience(
            company=self.company, title=self.title, period=self.period.to_domain()
        )


class WireEducation(BaseModel):
    institution: str | None = None
    major: str | None = None
    degree_raw: str | None = None
    degree_level: int = Field(default=0, ge=0, le=4)
    period: WirePeriod = Field(default_factory=WirePeriod)

    @model_validator(mode="before")
    @classmethod
    def _clean(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            for key in ("institution", "major", "degree_raw"):
                data[key] = _clean_str(data.get(key))
        return data

    def to_domain(self) -> Education:
        return Education(
            institution=self.institution,
            major=self.major,
            degree_raw=self.degree_raw,
            degree_level=self.degree_level,
            period=self.period.to_domain(),
        )


class ExtractedResume(BaseModel):
    """对应 domain.ResumeMetadata。字段名与 prompt 里描述的一致。"""

    name: str | None = None
    email: str | None = None
    phone: str | None = None

    skills: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    preferred_locations: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    educations: tuple[WireEducation, ...] = ()
    experiences: tuple[WireExperience, ...] = ()

    expected_salary: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _clean(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for key in ("name", "email", "phone", "expected_salary"):
            data[key] = _clean_str(data.get(key))
        for key in ("skills", "industries", "preferred_locations", "tags"):
            data[key] = _clean_tuple(data.get(key))
        for key in ("educations", "experiences"):
            # 模型可能返回 null，pydantic 对 tuple 字段不接受 None
            data[key] = data.get(key) or ()
        return data

    def to_domain(self) -> ResumeMetadata:
        return ResumeMetadata(
            name=self.name,
            email=self.email,
            phone=self.phone,
            skills=self.skills,
            industries=self.industries,
            preferred_locations=self.preferred_locations,
            tags=self.tags,
            educations=tuple(e.to_domain() for e in self.educations),
            experiences=tuple(e.to_domain() for e in self.experiences),
            expected_salary=self.expected_salary,
        )


# ============================================================
# 岗位需求
# ============================================================

class ExtractedJobRequirement(BaseModel):
    """对应 domain.JobRequirement。"""

    required_skills: tuple[str, ...] = ()
    preferred_skills: tuple[str, ...] = ()
    min_degree_level: int = Field(default=0, ge=0, le=4)
    industries: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    tags: tuple[str, ...] = ()
    min_experience_months: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _clean(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for key in ("required_skills", "preferred_skills", "industries", "locations", "tags"):
            data[key] = _clean_tuple(data.get(key))
        return data

    @model_validator(mode="after")
    def _check_salary(self):
        # 模型偶尔会把上下限填反（"15k-20k" 解析错方向）。
        # 交换比重试划算：这个错误的信息量不在顺序上。
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            self.salary_min, self.salary_max = self.salary_max, self.salary_min
        return self

    def to_domain(self) -> JobRequirement:
        return JobRequirement(
            required_skills=self.required_skills,
            preferred_skills=self.preferred_skills,
            min_degree_level=self.min_degree_level,
            industries=self.industries,
            locations=self.locations,
            salary_min=self.salary_min,
            salary_max=self.salary_max,
            tags=self.tags,
            min_experience_months=self.min_experience_months,
        )


__all__ = [
    "WirePeriod",
    "WireExperience",
    "WireEducation",
    "ExtractedResume",
    "ExtractedJobRequirement",
]
