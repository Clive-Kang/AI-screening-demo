"""评分档案：把 config/scoring.yaml 读成一个校验过的对象。

为什么权重必须校验而不是"顺手归一化"？
    配置写成六维和 = 0.90 时，归一化会把它悄悄拉成 1.00。
    调参的人看到 skill 写的是 0.40，实际生效的是 0.36 —— 文档和行为从此对不上。
    参考项目的 `_filter_by_education` 就因为 `required_level=0` 静默失效，
    别人的相似毛病是：配置错误无声通过。

本模块是 domain 层，只依赖 pydantic / PyYAML，不 import 任何框架或 adapter。
"""

import math
from enum import Enum    #定义枚举类 `UnknownFieldPolicy`
from pathlib import Path  #`Path`：路径工具，用来读取 yaml 文件
from typing import Any   

import yaml   # yaml：PyYAML 库,读取 yaml 配置文件
from pydantic import BaseModel, ConfigDict, Field, model_validator
#`model_validator`：模型级别的自定义校验器

# 六个评分维度。为什么要写死这份名单？
#   维度名拼错（如把 skill 写成 skll）时，那条权重的 key 就成了一个"新维度"，
#   它照样参与求和，总和甚至能凑成 1.0 —— 错误无声通过。
KNOWN_DIMS = frozenset({"skill", "industry", "location", "salary", "education", "tag"})

# 浮点数的容差。0.1 + 0.2 != 0.3，权重求和不能用 == 直接比。
WEIGHT_TOLERANCE = 1e-6  #浮点数容差：`0.000001`。


class UnknownFieldPolicy(str, Enum):  #**无法识别**时的业务策略
    """字段缺失或无法识别时，硬性过滤怎么处理。"""

    KEEP = "keep"              # 不淘汰，交给评分去区分（默认，追求召回）
    REJECT = "reject"          # 淘汰，并记录 reject_reason（严招，追求精确）
    SCORE_ONLY = "score_only"  # 不参与硬性过滤，只参与打分（软性条件）


class _Strict(BaseModel):
    """所有配置模型的共同基类：拒绝未知字段，创建后不可修改。

    `extra="forbid"` 是本文件最重要的一行代码：
     含义：yaml 里出现**模型没有定义的字段 → 直接抛异常**。
      例:把 `weight` 拼成 `weigth` 时，pydantic 会直接报错。
      否则它会被当成"一个不认识的多余字段"默默忽略，
      那条维度的权重悄悄变成默认值 —— 又是一次配置错误无声通过。

    `frozen=True` 让实例不可变：R6 的评分代码拿到档案后不可能改到权重，
    同一份配置在一次筛选里对所有候选人用的是完全相同的权重。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class DimWeight(_Strict):
    """单个维度的权重。取值范围 [0, 1]，负权重没有意义。"""

    weight: float = Field(ge=0.0, le=1.0)


class RecallConfig(_Strict):
    """向量召回的参数。"""

    top_k: int = Field(default=100, ge=1, le=1000)
    min_similarity: float = Field(default=0.0, ge=0.0, le=1.0)


class ScoringProfile(_Strict):
    """一个完整的评分档案。"""

    dims: dict[str, DimWeight]
    semantic: DimWeight  #语义分权重
    recall: RecallConfig = RecallConfig() #向量召回配置
    unknown_field_policy: UnknownFieldPolicy = UnknownFieldPolicy.KEEP  #默认策略 KEEP
    analyze_top_n: int = Field(default=10, ge=1, le=100)#用于日志 / 诊断，取 topN 候选人做打分分析

    @property  #像读取属性一样调用，不用加括号。
    def dim_weight_total(self) -> float:  #计算`dims`内部 6 个维度权重总和
        return sum(d.weight for d in self.dims.values())    

    @model_validator(mode="after") #型后置校验器
    def _check_weights(self) -> "ScoringProfile":
        """三条不变量，任何一条不满足就拒绝构造。"""
        # ① 维度名必须在已知的六个之内
        #检查 dims 里所有 key，必须属于 KNOWN_DIMS；
        # 如果出现未知维度，抛错；同时 dims 字典不能为空。
        unknown = sorted(set(self.dims) - KNOWN_DIMS) 
        if unknown:
            raise ValueError(
                f"未知维度 {unknown}，允许的是 {sorted(KNOWN_DIMS)}"
            )
        if not self.dims:
            raise ValueError("dims 不能为空")

        # ② 六维权重和必须恰好是 1.0 
        #校验 6 个维度权重总和≈1.0，
        # 用`math.isclose`+ 容差判断，浮点数安全对比。不满足直接抛异常。
        total = self.dim_weight_total
        if not math.isclose(total, 1.0, abs_tol=WEIGHT_TOLERANCE):
            raise ValueError(
                f"六维权重和为 {total:.6f}，必须等于 1.0"
                f"（相差 {total - 1.0:+.6f}）"
            )

        # ③ 语义权重是"占最终分的比例"，0 表示不用语义分，1 表示完全由语义决定
        #    （DimWeight 已把范围限制在 [0, 1]，这里只补一句语义说明）
        return self


class ScoringConfig(_Strict): #`ScoringConfig`：顶层总配置，包含多套评分档案
    """整份评分配置：若干档案 + 一个默认档案。"""

    version: int = Field(ge=1)
    default_profile: str  #默认评分档案名称，字符串 key。
    profiles: dict[str, ScoringProfile]  
    #多套评分档案，key = 档案名称，value = 一套`ScoringProfile`。

    @model_validator(mode="after")
    def _check_default(self) -> "ScoringConfig": #后置校验器 `_check_default`
        if not self.profiles:
            raise ValueError("profiles 不能为空")
        if self.default_profile not in self.profiles:
            raise ValueError(
                f"default_profile={self.default_profile!r} 不在 profiles 中，"
                f"可用：{sorted(self.profiles)}"
            )
        return self

    def profile(self, name: str | None = None) -> ScoringProfile:  #实例方法
        """按名字取档案；传 None 取 default_profile。"""
        key = name or self.default_profile
        if key not in self.profiles:
            raise KeyError(f"未知档案 {key!r}，可用：{sorted(self.profiles)}")
        return self.profiles[key]


# ============================================================
# 加载：YAML → 展开 inherits → 校验
# ============================================================

#递归深度合并字典
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典：override 覆盖 base，嵌套字典逐层合并而不是整块替换。

    这样 `strict` 只写 `recall: { top_k: 200 }` 时，
    `min_similarity` 仍然从父档案继承，而不是被整块丢掉。
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve(name: str, raw: dict[str, Any], seen: tuple[str, ...] = ()) -> dict[str, Any]:
    """展开 inherits 链，返回最终生效的配置字典。"""
    #`name`：当前要解析的档案名称  `raw`：原始所有 profiles 字典（未展开继承）
    #`seen`：元组，记录当前递归路径，**用来检测循环继承**，元组不可变，适合递归传参
    if name in seen:
        chain = " → ".join((*seen, name))
        raise ValueError(f"档案继承出现循环：{chain}")
    if name not in raw:
        raise ValueError(f"inherits 指向不存在的档案 {name!r}，可用：{sorted(raw)}")

    spec = raw[name]
    if not isinstance(spec, dict):
        raise ValueError(f"档案 {name!r} 必须是映射，实际是 {type(spec).__name__}")

    spec = dict(spec)
    parent = spec.pop("inherits", None)
    if parent is None:
        return spec
    return _deep_merge(_resolve(parent, raw, (*seen, name)), spec)


def load_scoring_config(path: str | Path) -> ScoringConfig:
    """读取 YAML 配置。任何问题都抛 ValueError，绝不返回"半可用"的对象。"""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    if not isinstance(document, dict):
        raise ValueError(
            f"{path}: 顶层必须是映射（dict），实际是 {type(document).__name__}"
        )
    if "default_profile" not in document:
        raise ValueError(f"{path}: 缺少 default_profile")

    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError(f"{path}: profiles 缺失或为空")

    resolved = {name: _resolve(name, raw_profiles) for name in raw_profiles}

    return ScoringConfig(
        version=document.get("version", 1),
        default_profile=document["default_profile"],
        profiles=resolved,
    )
