"""仓储端口:真源(SQLite)的读写接口。

> 这是**数据层抽象端口(接口定义),属于domain/port(领域层的端口),不写任何 SQL,只定义「能力契约」。
> 后面会单独写 SQLite 的适配器(adapter)实现这个接口；未来想换成 PostgreSQL,只换 adapter,业务 service 代码完全不动。

两个仓储对应两张核心表:
    ResumeRepository → resumes(简历本体,含原文与元数据)
    RunRepository    → runs(一次筛选任务的状态)

「仓储(Repository)」这个词的意思是:**把数据库伪装成一个集合**。
services 层看到的是 `repo.get(id)`、`repo.list(...)`,
看不到 SQL、连接、事务、行工厂(row factory)。换 SQLite → PostgreSQL
只需要重写 adapter,业务流程一行不改。

与 VectorStorePort 的分工(ARCHITECTURE.md §7.4)
------------------------------------------------
    要「像不像」        → VectorStorePort(模糊、浮点)
    要「是不是」        → 本模块(精确、可索引,走 SQL)
地点/学历/经验这类结构化条件必须走 SQL 下推。放进向量库做元数据过滤,
正是参考项目踩过的坑:list 被序列化成 JSON 字符串,`WHERE skills LIKE '%go%'`
还会命中 Django。
"""

# 让所有注解延迟求值（当作字符串）。本模块的 RunRepository 里有一个叫 `list`
# 的方法，若注解在定义时求值，后面的 `list[ResultRow]` 会被解析成那个方法。
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.domain.models import (
    DimScore,
    Resume,
    ResumeStatus,
    Run,
    RunStage,
    RunStatus,
)


@dataclass(frozen=True)
class ResultRow:
    """一条筛选结果（一行 results 表）。

    被淘汰的人也要有一行（passed=False + reject_reason）——
    否则"这个人为什么没出现在列表里"永远回答不了（ARCHITECTURE.md §6.4）。
    这也正是参考项目做不到的：它只存通过过滤的人。
    """

    resume_id: str
    rank: int
    overall: float | None = None
    semantic: float | None = None
    dims: tuple[DimScore, ...] = ()
    passed: bool = True
    reject_reason: str | None = None
    analysis_text: str | None = None
    analysis_prompt_version: str | None = None



@runtime_checkable
class ResumeRepository(Protocol):
    """简历仓储。所有写操作都必须幂等 —— 这是重跑与重试的前提。"""

    def add(self, resume: Resume) -> str:
        """新增,返回 resume_id。

        调用方负责先 `find_by_content_hash` 去重。这里不替它做,
        是因为"重复上传该返回既有 id 还是报错"是**业务决定**,
        属于 services；仓储只负责存。
        """
        ...         #`...` 在 Protocol 里代表：只声明接口，不写实现。

    def get(self, resume_id: str) -> Resume | None:
        """按 id 查；不存在返回 None(不抛异常)。

        「查不到」是正常业务分支(比如用户删了一个还没跑完的 run 的简历),
        让调用方每次 try/except 是没必要的噪音。
        """
        ...

    def find_by_content_hash(self, sha256: str) -> Resume | None:
        """按文件内容哈希查重。同一份 PDF 上传两次只该有一行。"""
        ...

    def list(
        self,
        *,
        limit: int,
        offset: int,
        status: ResumeStatus | None = None,
    ) -> list[Resume]:
        """分页列出,可按状态过滤。

        强制分页(limit 没有默认值):参考项目一次性把全部简历读进内存字典,
        几千份时进程直接吃掉几百 MB。
        """
        ...

    def update(self, resume: Resume) -> None:
        """按 id 整体覆盖。

        为什么是"整体覆盖"而不是 update_status / save_metadata 等多个方法？
        流水线每一步都是「拿到 Resume → 改一个字段 → 存回去」,
        一个方法就够；而多个窄方法会让 adapter 里出现 N 个几乎相同的 UPDATE。
        """
        ...

    def delete(self, resume_id: str) -> None:
        """删除；不存在时静默返回(幂等)。"""
        ...

    def corpus_version(self) -> int:
        """当前语料版本号；库是空的返回 0。

        幂等键的第三个因子：同样的 query + profile，但简历库变了 → 必须重算。
        否则新入库的简历永远不会出现在既有 run 的结果里。
        """
        ...

    def bump_corpus_version(self) -> int:
        """语料变化时 +1，返回新版本号。

        由**写入方**（入库/删除/重新抽取）调用，而不是在查询时按
        "当前简历数量"临时算 —— 删一份再加一份，数量没变但内容变了。
        """
        ...


@runtime_checkable
class RunRepository(Protocol):
    """筛选任务仓储。

    与 ResumeRepository 不同,这里的写操作是**分段**的:
    create → 多次 update_stage → finish。分成三个方法而不是一个 update,
    是因为它们在不同进程里调用(API 建任务、worker 推进度),
    每个方法对应一条固定 SQL,不用先读整行再写回去(省一次往返,也避免
    「worker 读到旧 status 又写回」的丢失更新)。
    """

    def create(self, run: Run) -> str:
        """新建任务,返回 run_id。"""
        ...

    def get(self, run_id: str) -> Run | None:
        """查任务状态。GET /runs/{id} 走这里,必须无副作用。"""
        ...

    def update_stage( #高频调用:worker 每跑完一个阶段就调用：更新当前流水线阶段、各阶段计数。
        self,
        run_id: str,
        stage: RunStage,
        progress: dict[str, int],
    ) -> None:
        """推进阶段并写进度。例如 progress={"recall": 120, "filtered": 34}。"""
        ...

    def finish(
        self,
        run_id: str,
        status: RunStatus,
        counts: dict[str, int],
        *,
        error: str | None = None,
    ) -> None:
        """落终态。counts 对应 recall_n / filtered_n / scored_n / analyzed_n。"""
        ...

    def find_by_idempotency_key(self, key: str) -> Run | None:
        """幂等查找。同参数重复提交时直接返回既有 run,不重跑也不重计费。"""
        ...

    def list(self, *, limit: int, offset: int) -> list[Run]:
        """最近的任务,按 created_at 倒序。"""
        ...

    # ---- results（run 的子表）----
    # 结果挂在 run 下面，所以放在同一个仓储里；单独开一个仓储反而要
    # 传 run_id 做外键校验，收益为零。

    def save_results(self, run_id: str, rows: list[ResultRow]) -> None:
        """整批写入本次筛选结果（先删后插，可重复执行）。

        为什么整批而不是逐条: 一次筛选的全部结果是一个整体,
        中途半批写入既没有业务含义,也会让 rank 出现空洞。
        """
        ...

    def list_results(self, run_id: str) -> list[ResultRow]:
        """按 rank 升序取出结果。GET /runs/{id} 走这里,必须无副作用。"""
        ...


__all__ = [
    "Resume",
    "ResumeStatus",
    "Run",
    "RunStage",
    "RunStatus",
    "ResultRow",
    "ResumeRepository",
    "RunRepository",
]
