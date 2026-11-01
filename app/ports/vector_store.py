"""向量库端口：只回答一个问题 ——「哪几份简历和这段文本最像」。

它不做结构化过滤（地点/学历/经验那些走 SQL，ARCHITECTURE.md §7.4），
也不管向量怎么算出来的（那是 Embedding 客户端的事）。
职责越窄，换实现（sqlite-vec / Chroma / Milvus）时改动面越小。
"""

from collections.abc import Sequence
#`Sequence` = **所有支持顺序索引、可迭代的只读序列抽象类型**
#凡是可以用 `[i]` 取下标、支持 `len()` 的，都属于 `Sequence`
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ResumeDoc:
    """一条待写入向量库的记录。

    vector 用 tuple 而不是 list：frozen dataclass 只禁止"换字段"，
    塞进 list 后照样能被 append 改掉内容（假冻结）。
    """

    resume_id: str
    text: str                 # 产生该向量的原文，便于排查与重建
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.resume_id:
            raise ValueError("ResumeDoc.resume_id 不能为空")
        if not self.vector:
            raise ValueError(f"{self.resume_id}: vector 不能为空")


@dataclass(frozen=True)
class Hit:
    """一次检索命中的结果。

    只有 similarity，没有 distance —— 这是有意的：
    参考项目 Milvus 侧做了 `1.0 - distance`、Chroma 侧没做，
    同一个字段在两种后端下含义相反，而下游根本没人读 distance。
    统一成「越大越像，区间 [0,1]」后，换后端不会悄悄改变排序含义。
    """

    resume_id: str
    similarity: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.similarity <= 1.0):
            raise ValueError(
                f"similarity 必须在 [0,1]，实际是 {self.similarity}"
                "（实现方负责把不同后端的分值换算到同一区间）"
            )


@runtime_checkable
class VectorStorePort(Protocol):
    """向量索引的能力清单。实现方（adapter）不必显式继承本类。

    Python 的 Protocol 是「结构化子类型」：只要方法签名对得上，
    这个对象就算实现了本端口 —— 不需要 `class SqliteVecStore(VectorStorePort)`。
    好处是 adapter 不用 import ports（依赖方向更干净），
    坏处是编译器不会提醒你"少写了一个方法"，所以要用契约测试兜住
    （tests/contract/，ARCHITECTURE.md §10.3）。
    """

    #upsert语义：存在就覆盖，不存在就新增；同 ID 重复调用不会产生重复数据。
    def upsert(self, docs: Sequence[ResumeDoc]) -> None:
        #`docs: Sequence[ResumeDoc]`意思：只要是*可索引的序列*就行，不一定非要 `list`
        """写入或覆盖。同 resume_id 重复写入必须幂等。

        幂等是重跑的前提：重建索引、重抽一份简历都不该产生重复条目。
        """
        ...

    def search(
        self,
        vector: Sequence[float],
        *,
        top_k: int,
        min_similarity: float,
    ) -> list[Hit]:
        """按相似度降序返回至多 top_k 条，且 similarity ≥ min_similarity。

        为什么 min_similarity 是入参而不是 adapter 里的常量？
        它跟着评分配置档案走（config/scoring.yaml::recall），
        strict 档案要求 0.40，tech 只要 0.30 —— 硬编码进 adapter 就调不动。
        """
        ...

    def delete(self, resume_ids: Sequence[str]) -> None:
        """按 id 删除；不存在的 id 静默忽略（幂等）。"""
        ...

    def drop_and_rebuild(self) -> None:
        """清空索引。

        有的实现（及运维命令 reindex）需要它：向量是可丢弃的派生数据，
        任何时候都能从 SQLite 原文重新算出来（原则 P6）。
        """
        ...
