"""所有 HTTP 路由：简历、筛选、运维。

三组接口放在一个文件里，因为它们共享同一套依赖注入（`AppContext`），
总行数也不到 300。拆成 `routes/` 目录只会多一层 import，不增加清晰度。

原则不变：**这里不写业务逻辑**。上传只登记、筛选只入队、GET 只读，
真正的判断都在 services/ 里。
"""

import hashlib
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.api.deps import AppContext, get_context
from app.api.schemas import (
    CandidateOut,
    ClearResumesResponse,
    DimScoreOut,
    ResumeDetail,
    ResumeList,
    ResumeSummary,
    RunResults,
    RunSummary,
    SubmitRunRequest,
    SubmitRunResponse,
    UploadResponse,
)
from app.domain.models import Resume, ResumeStatus
from app.ports.repository import ResultRow
from app.ports.task_queue import Task, TaskKind
from app.services.screening import create_run

resumes_router = APIRouter(prefix="/resumes", tags=["resumes"])
runs_router = APIRouter(prefix="/runs", tags=["runs"])
system_router = APIRouter(tags=["system"])


# ============================================================
# 简历
# ============================================================

@resumes_router.post("", status_code=202, response_model=UploadResponse)
async def upload_resume(
    file: UploadFile = File(...),
    ctx: AppContext = Depends(get_context),
) -> UploadResponse:
    """上传一份简历 → 202 + resume_id。

    只做毫秒级的事：读字节 → 算 sha256 → 查重 → 落盘 + 落库（queued）→ 入队。
    解析 + LLM 抽取 + 嵌入交给 worker（ARCHITECTURE.md §5.5）。

    去重发生在**入队之前**：同一份文件第二次上传只返回既有 id，
    不产生新行，也不产生新任务。
    """
    filename = file.filename or "unnamed"
    data = await file.read()

    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > ctx.settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {ctx.settings.max_upload_mb} MB 上限",
        )
    if not ctx.parser.supports(filename, data[:8]):
        raise HTTPException(status_code=415, detail="只支持 PDF 文件")

    content_sha256 = hashlib.sha256(data).hexdigest()

    existing = ctx.resumes.find_by_content_hash(content_sha256)
    if existing is not None:
        return UploadResponse(resume_id=existing.id, deduplicated=True)

    # 落盘用内容哈希命名：同内容天然同一路径，重试不会产生第二份文件
    suffix = Path(filename).suffix.lower() or ".pdf"
    ctx.settings.blob_dir.mkdir(parents=True, exist_ok=True)
    blob_path = ctx.settings.blob_dir / f"{content_sha256}{suffix}"
    if not blob_path.is_file():
        blob_path.write_bytes(data)

    resume = Resume(
        id=uuid.uuid4().hex,
        filename=filename,
        content_sha256=content_sha256,
        blob_path=str(blob_path),
        status=ResumeStatus.QUEUED,
    )
    try:
        ctx.resumes.add(resume)
    except sqlite3.IntegrityError:
        # 并发上传同一份文件：另一个请求抢先插入了。
        # content_sha256 上的 UNIQUE 约束替我们挡住了重复，这里只需返回既有 id。
        existing = ctx.resumes.find_by_content_hash(content_sha256)
        if existing is None:
            raise
        return UploadResponse(resume_id=existing.id, deduplicated=True)

    ctx.queue.enqueue(
        Task(
            id=uuid.uuid4().hex,
            kind=TaskKind.PARSE_RESUME,
            payload=(
                ("resume_id", resume.id),
                ("filename", filename),
                ("blob_path", str(blob_path)),
                ("content_sha256", content_sha256),
            ),
        )
    )

    return UploadResponse(resume_id=resume.id, deduplicated=False)


@resumes_router.delete("", response_model=ClearResumesResponse)
def clear_resumes(ctx: AppContext = Depends(get_context)) -> ClearResumesResponse:
    """清空简历库（演示 / 重置用）。

    四处一起清，少一处都会留下"幽灵数据"：
        1. 真源行（拆表由 ON DELETE CASCADE 带走）
        2. data/blobs/ 下的原始 PDF
        3. 向量索引（整表重建比逐条删干净）
        4. 队列里的任务 —— 否则待处理的 parse_resume 会拿着已删除的
           resume_id 重新建行，清空后过几秒简历又"长"回来
    """
    resumes = ctx.resumes.list(limit=10_000, offset=0)

    for resume in resumes:
        ctx.resumes.delete(resume.id)
        if resume.blob_path:
            Path(resume.blob_path).unlink(missing_ok=True)

    ctx.vectors.drop_and_rebuild()
    ctx.db.execute("DELETE FROM tasks")
    ctx.resumes.bump_corpus_version()

    return ClearResumesResponse(deleted=len(resumes))


@resumes_router.get("", response_model=ResumeList)
def list_resumes(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: ResumeStatus | None = None,
    ctx: AppContext = Depends(get_context),
) -> ResumeList:
    """分页列表，可按状态过滤（前端轮询进度用）。"""
    items = ctx.resumes.list(limit=limit, offset=offset, status=status)
    return ResumeList(
        items=[ResumeSummary.from_resume(r) for r in items],
        limit=limit,
        offset=offset,
    )


@resumes_router.get("/{resume_id}", response_model=ResumeDetail)
def get_resume(
    resume_id: str,
    include_text: bool = Query(False, description="是否返回 PDF 解析出的原文"),
    ctx: AppContext = Depends(get_context),
) -> ResumeDetail:
    """详情。原文默认不返回 —— 一份简历的原文可能几十 KB。"""
    resume = ctx.resumes.get(resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")
    return ResumeDetail.from_resume(resume, include_text=include_text)


@resumes_router.delete("/{resume_id}", status_code=204)
def delete_resume(
    resume_id: str,
    ctx: AppContext = Depends(get_context),
) -> None:
    """删除简历：真源、派生索引、语料版本三处都要清。

    只删数据库会留下"幽灵向量" —— 召回时命中一个查不到的 id，
    表现为结果里莫名少几个人。screening 里虽然有跳过逻辑兜底，
    但那是止血，不是治疗。
    """
    if ctx.resumes.get(resume_id) is None:
        raise HTTPException(status_code=404, detail="简历不存在")

    ctx.resumes.delete(resume_id)          # 拆表由 ON DELETE CASCADE 负责
    ctx.vectors.delete([resume_id])        # 派生索引
    ctx.resumes.bump_corpus_version()      # 旧 run 的幂等键立即失效


# ============================================================
# 筛选
# ============================================================

@runs_router.post("", status_code=202, response_model=SubmitRunResponse)
def submit_run(
    req: SubmitRunRequest,
    ctx: AppContext = Depends(get_context),
) -> SubmitRunResponse:
    """提交一次筛选 → 202 + run_id，真正的计算在 worker 里。

    幂等命中（同 query + 同档案 + 简历库没变）时不再入队，
    直接返回既有 run_id，deduplicated=true。
    """
    query_text = req.query_text.strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="query_text 不能为空")

    try:
        run, existed = create_run(
            resumes=ctx.resumes,
            runs=ctx.runs,
            config=ctx.scoring,
            query_text=query_text,
            profile=req.profile,
        )
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc.args[0])) from exc

    if not existed:
        ctx.queue.enqueue(
            Task(
                id=uuid.uuid4().hex,
                kind=TaskKind.SCREEN_RUN,
                payload=(
                    ("run_id", run.id),
                    ("query_text", query_text),
                    ("profile", run.profile),
                ),
            )
        )

    return SubmitRunResponse(
        run_id=run.id,
        status=run.status.value,
        deduplicated=existed,
    )


@runs_router.get("/{run_id}", response_model=RunSummary)
def get_run(run_id: str, ctx: AppContext = Depends(get_context)) -> RunSummary:
    """查进度。纯读操作，重复调用不改变任何状态。"""
    run = ctx.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return RunSummary.from_run(run)


@runs_router.get("/{run_id}/results", response_model=RunResults)
def get_results(
    run_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_rejected: bool = Query(
        False, description="是否包含被硬性过滤淘汰的候选人（带淘汰原因）"
    ),
    ctx: AppContext = Depends(get_context),
) -> RunResults:
    """读已落库的结果。结果还没写出来时返回空列表，而不是重新跑一遍。"""
    run = ctx.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    rows = ctx.runs.list_results(run_id)
    if not include_rejected:
        rows = [r for r in rows if r.passed]

    page = rows[offset : offset + limit]
    return RunResults(
        run=RunSummary.from_run(run),
        total=len(rows),
        items=[_candidate(ctx, row) for row in page],
    )


def _candidate(ctx: AppContext, row: ResultRow) -> CandidateOut:
    resume = ctx.resumes.get(row.resume_id)
    return CandidateOut(
        resume_id=row.resume_id,
        rank=row.rank,
        name=resume.metadata.name if resume and resume.metadata else None,
        overall_score=row.overall,
        semantic_score=row.semantic,
        dims=[
            DimScoreOut(
                name=d.name, score=d.score, weight=d.weight, reason=d.reason
            )
            for d in row.dims
        ],
        passed=row.passed,
        reject_reason=row.reject_reason,
        analysis=row.analysis_text,
    )


# ============================================================
# 运维与配置
# ============================================================
# /health 不主动调 LLM / Embedding：那会产生真实费用，而且一秒钟几十次
# 健康探测的代价远大于收益。依赖连通性由第一次真实任务暴露（worker 日志里）。

@system_router.get("/health")
def health(ctx: AppContext = Depends(get_context)) -> dict:
    try:
        ctx.db.query_one("SELECT 1 AS ok")
        db_ok = True
    except Exception:
        db_ok = False

    return {"status": "ok" if db_ok else "degraded", "db": db_ok}


@system_router.get("/profiles")
def list_profiles(ctx: AppContext = Depends(get_context)) -> dict:
    """列出可用评分档案及权重 —— HR 调参前先看这个。"""
    return {
        "default": ctx.scoring.default_profile,
        "profiles": [
            {"name": name, **profile.model_dump(mode="json")}
            for name, profile in ctx.scoring.profiles.items()
        ],
    }
