"""HTTP 响应模型。

领域模型是 dataclass（不带 pydantic），所以这里是**另一层**显式转换：
DB 里的什么字段、以什么形状暴露给客户端，由这一层决定。

为什么不直接把领域 dataclass 丢给 FastAPI：
    1. 领域模型里有 raw_text（可能几十 KB），列表接口不该带上
    2. 将来改字段名（name → candidate_name）时，只改这里，不影响 domain
"""

from dataclasses import asdict

from pydantic import BaseModel, Field

from app.domain.models import Resume, Run


class ResumeSummary(BaseModel):
    """列表项：只看状态，不返回原文和完整元数据。"""

    id: str
    filename: str
    status: str
    name: str | None = None
    error: str | None = None
    created_at: str

    @classmethod
    def from_resume(cls, resume: Resume) -> "ResumeSummary":
        return cls(
            id=resume.id,
            filename=resume.filename,
            status=resume.status.value,
            name=resume.metadata.name if resume.metadata else None,
            error=resume.error,
            created_at=resume.created_at,
        )


class ResumeDetail(ResumeSummary):
    """详情：加元数据，原文按需返回（可能很大）。"""

    metadata: dict | None = None
    raw_text: str | None = None

    @classmethod
    def from_resume(cls, resume: Resume, *, include_text: bool = False) -> "ResumeDetail":
        return cls(
            **ResumeSummary.from_resume(resume).model_dump(),
            metadata=asdict(resume.metadata) if resume.metadata else None,
            raw_text=resume.raw_text if include_text else None,
        )


class ResumeList(BaseModel):
    items: list[ResumeSummary]
    limit: int
    offset: int


class UploadResponse(BaseModel):
    """上传只做登记：立刻返回 id，处理交给 worker（ARCHITECTURE.md §5.5）。"""

    resume_id: str
    deduplicated: bool


class ClearResumesResponse(BaseModel):
    """清空简历库的结果：只报数量，不回显被删的内容。"""

    deleted: int


class RunSummary(BaseModel):
    """筛选任务的状态快照（不含候选人结果）。"""

    run_id: str
    status: str
    stage: str | None = None
    profile: str
    query_text: str
    progress: dict[str, int] = Field(
        default_factory=dict,
        description="阶段计数：recall=召回数，filtered=硬性过滤后**剩余**数，"
        "scored=评分数，analyzed=已生成报告数",
    )
    recall_n: int = 0
    filtered_n: int = Field(
        0, description="硬性过滤后**通过**的人数（沿用 SQL 列名，不是淘汰数）"
    )
    scored_n: int = 0
    analyzed_n: int = 0
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None

    @classmethod
    def from_run(cls, run: Run) -> "RunSummary":
        return cls(
            run_id=run.id,
            status=run.status.value,
            stage=run.stage.value if run.stage else None,
            profile=run.profile,
            query_text=run.query_text,
            progress=dict(run.progress),
            recall_n=run.recall_n,
            filtered_n=run.filtered_n,
            scored_n=run.scored_n,
            analyzed_n=run.analyzed_n,
            error=run.error,
            created_at=run.created_at,
            finished_at=run.finished_at,
        )


class SubmitRunRequest(BaseModel):
    query_text: str
    profile: str | None = None


class SubmitRunResponse(BaseModel):
    run_id: str
    status: str
    deduplicated: bool = False


class DimScoreOut(BaseModel):
    """一个评分维度。score=None 表示"没数据"，不是 0 分。"""

    name: str
    score: float | None = None
    weight: float
    reason: str


class CandidateOut(BaseModel):
    resume_id: str
    rank: int
    name: str | None = None
    overall_score: float | None = None
    semantic_score: float | None = None
    dims: list[DimScoreOut] = Field(default_factory=list)
    passed: bool = True
    reject_reason: str | None = None
    analysis: str | None = None


class RunResults(BaseModel):
    run: RunSummary
    total: int
    items: list[CandidateOut]
