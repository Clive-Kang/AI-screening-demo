"""筛选编排：一句话需求 → 排好序的候选人 + Top-N 报告。

    解析需求 → 幂等键 → 召回 → 硬过滤 → 六维+语义评分 → 排序 → Top-N 分析

与参考项目的关键差别
--------------------
1. **GET 无副作用**：这里做完全部计算后把结果落进 results 表，
   查询方拿到的是现成数据，不会因为多刷几次页面就多花几倍 LLM 费用。
2. **召回之后再过滤**：向量只负责"像不像"，硬条件（学历/地点/薪资/经验）
   在 Python 里用结构化的 metadata 判断。参考项目把 top_k 写死成 10，
   语义排第 11 名的合格候选人直接被丢掉。
3. **被淘汰的人留下痕迹**：ResultRow(passed=False, reject_reason=...)
   也要落库，否则"他为什么没出现"无法回答。
4. **只对 Top-N 生成报告**：报告是最贵的一步，第 11 名之后的没人看。

失败语义
--------
    整条链路出错 → run 置 failed 并抛出（调用方决定怎么报告）
    单个候选人的报告失败 → 该行 analysis_text 留空，run 置 partial
    这两种错误不该混为一谈：一个候选人的 LLM 失败不该让整次筛选作废。
"""

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import date

from app.domain.filtering import filter_resume
from app.domain.models import Resume, Run, RunStage, RunStatus
from app.domain.policies import ScoringConfig, ScoringProfile
from app.domain.scoring import combine
from app.ports.embedding import EmbeddingPort
from app.ports.llm import LLMPort
from app.ports.repository import ResultRow, ResumeRepository, RunRepository
from app.ports.vector_store import Hit, VectorStorePort
from app.prompts import Prompt, load
from app.services.schemas import ExtractedJobRequirement


def idempotency_key(query_text: str, profile: str, corpus_version: int) -> str:
    """同 query + 同档案 + 同简历库 → 同一个 key。

    三个因子里最容易忘的是 corpus_version：简历库变了却复用旧结果，
    新入库的候选人永远不会出现，而 HR 看不出任何异常。
    """
    raw = f"{query_text.strip()}|{profile}|{corpus_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _config_fingerprint(config: ScoringConfig) -> str:
    """配置指纹：权重改了要能从结果里看出来。"""
    return hashlib.sha256(
        config.model_dump_json(exclude_none=True).encode("utf-8")
    ).hexdigest()


def create_run(
    *,
    resumes: ResumeRepository,
    runs: RunRepository,
    config: ScoringConfig,
    query_text: str,
    profile: str | None = None,
) -> tuple[Run, bool]:
    """建（或取回）一次筛选任务，返回 `(run, 是否已存在)`。

    做成模块级函数而不是纯实例方法，是为了让 API 只依赖仓储 + 配置
    （不需要构造 LLM、嵌入、向量库客户端），同时又不必把幂等逻辑抄第二份。

    命中幂等时返回既有 run 且 existed=True —— 调用方不必再入队。
    """
    profile_name = profile or config.default_profile
    config.profile(profile_name)   # 档案名拼错，在这里当场报错

    key = idempotency_key(query_text, profile_name, resumes.corpus_version())
    existing = runs.find_by_idempotency_key(key)
    if existing is not None:
        return existing, True

    run = Run(
        id=uuid.uuid4().hex,
        query_text=query_text,
        profile=profile_name,
        idempotency_key=key,
        config_snapshot_json=config.model_dump_json(),
        status=RunStatus.QUEUED,
    )
    runs.create(run)
    return run, False


@dataclass(frozen=True)
class ScreeningResult:
    run: Run
    rows: tuple[ResultRow, ...]


class ScreeningService:
    def __init__(
        self,
        *,
        resumes: ResumeRepository,
        runs: RunRepository,
        vectors: VectorStorePort,
        embedder: EmbeddingPort,
        llm: LLMPort,
        config: ScoringConfig,
        requirement_prompt: Prompt | None = None,
        report_prompt: Prompt | None = None,
        today_ym: str | None = None,
    ) -> None:
        self._resumes = resumes
        self._runs = runs
        self._vectors = vectors
        self._embedder = embedder
        self._llm = llm
        self._config = config
        self._config_hash = _config_fingerprint(config)
        self._requirement_prompt = requirement_prompt or load("job_requirement")
        self._report_prompt = report_prompt or load("candidate_report")
        # 经验月数要按"今天"实时算，不能让调用方各自取一次时钟
        self._today_ym = today_ym or date.today().strftime("%Y-%m")

    # ------------------------------------------------------------

    def create_run(
        self, query_text: str, *, profile: str | None = None
    ) -> tuple[Run, bool]:
        """建（或取回）一次筛选任务。见模块级 `create_run`。"""
        return create_run(
            resumes=self._resumes,
            runs=self._runs,
            config=self._config,
            query_text=query_text,
            profile=profile,
        )

    def run(
        self,
        query_text: str,
        *,
        profile: str | None = None,
        run_id: str | None = None,
    ) -> ScreeningResult:
        """执行一次筛选。

        两种调用方式：
            run("招 Python")              —— 同步执行，自己建 run（测试 / 脚本）
            run(..., run_id=...)          —— worker 用 API 预建好的 run 执行
        """
        if run_id is None:
            run, existed = self.create_run(query_text, profile=profile)
            if existed:
                # 命中幂等：不重跑、不重新计费，直接把上次的结果还回去
                return ScreeningResult(
                    run, tuple(self._runs.list_results(run.id))
                )
        else:
            run = self._runs.get(run_id)
            if run is None:
                raise ValueError(f"run 不存在：{run_id}")
            if run.status in (RunStatus.SUCCEEDED, RunStatus.PARTIAL):
                # 任务被重复投递（可见性锁过期/worker 崩溃前已写完）→ 不重复计费
                return ScreeningResult(
                    run, tuple(self._runs.list_results(run.id))
                )

        # 以 run 里记录的条件为准：worker 重试时传入的 payload 可能已经过期
        query_text = run.query_text
        profile_obj = self._config.profile(run.profile)

        try:
            requirement = self._parse_requirement(query_text)

            hits = self._recall(query_text, profile_obj)
            self._runs.update_stage(
                run.id, RunStage.RECALL, {"recall": len(hits)}
            )

            rows = self._score_all(hits, requirement, profile_obj, run.profile)
            passed_n = sum(1 for r in rows if r.passed)
            self._runs.update_stage(
                run.id,
                RunStage.SCORE,
                {"recall": len(hits), "filtered": passed_n, "scored": len(rows)},
            )

            rows, analyzed_n, failed_n = self._analyze(
                rows, requirement, profile_obj.analyze_top_n
            )
            self._runs.update_stage(
                run.id,
                RunStage.DONE,
                {
                    "recall": len(hits),
                    "filtered": passed_n,
                    "scored": len(rows),
                    "analyzed": analyzed_n,
                },
            )

            self._runs.save_results(run.id, list(rows))

            status = RunStatus.PARTIAL if failed_n else RunStatus.SUCCEEDED
            self._runs.finish(
                run.id,
                status,
                {
                    "recall": len(hits),
                    "filtered": passed_n,
                    "scored": len(rows),
                    "analyzed": analyzed_n,
                },
            )
        except Exception as exc:
            self._runs.finish(run.id, RunStatus.FAILED, {}, error=str(exc))
            raise

        finished = self._runs.get(run.id)
        return ScreeningResult(finished, tuple(rows))

    # ------------------------------------------------------------
    # 阶段
    # ------------------------------------------------------------

    def _parse_requirement(self, query_text: str):
        """LLM 把 HR 的一句话翻译成结构化条件。"""
        dto = self._llm.extract(
            self._requirement_prompt.render(query_text=query_text),
            ExtractedJobRequirement,
        )
        return dto.to_domain()

    def _recall(self, query_text: str, profile_obj: ScoringProfile) -> list[Hit]:
        """向量召回。查询词就是 HR 原话 —— 它已经是"岗位描述"了。"""
        vector = self._embedder.embed([query_text])[0]
        return self._vectors.search(
            vector,
            top_k=profile_obj.recall.top_k,
            min_similarity=profile_obj.recall.min_similarity,
        )

    def _score_all(
        self,
        hits: list[Hit],
        requirement,
        profile_obj: ScoringProfile,
        profile_name: str,
    ) -> list[ResultRow]:
        rows: list[ResultRow] = []

        for hit in hits:
            resume = self._resumes.get(hit.resume_id)
            # 索引里有、库里没有 = 派生数据没跟上（删简历时漏删向量）。
            # 跳过而不是报错：一个人不该让整次筛选失败。
            if resume is None or resume.metadata is None:
                continue

            verdict = filter_resume(
                resume.metadata,
                requirement,
                policy=profile_obj.unknown_field_policy,
                today_ym=self._today_ym,
            )

            breakdown = combine(
                resume.metadata,
                requirement,
                profile_obj,
                profile_name=profile_name,
                semantic_score=hit.similarity,
                config_hash=self._config_hash,
            )

            rows.append(
                ResultRow(
                    resume_id=hit.resume_id,
                    rank=0,  # 排完序再填
                    overall=breakdown.overall,
                    semantic=hit.similarity,
                    dims=breakdown.dims,
                    passed=verdict.passed,
                    reject_reason="；".join(verdict.reasons) or None,
                )
            )

        return _rank(rows)

    def _analyze(
        self, rows: list[ResultRow], requirement, top_n: int
    ) -> tuple[list[ResultRow], int, int]:
        """只对排名靠前**且通过过滤**的候选人生成报告。

        报告是最贵的一步：每份都是一次完整的 LLM 调用。
        参考项目对全部候选人串行调用，而第 11 名之后的报告没人看。
        """
        requirement_json = json.dumps(
            asdict(requirement), ensure_ascii=False, default=str
        )
        analyzed = failed = 0
        out: list[ResultRow] = []

        for row in rows:
            if not row.passed or analyzed + failed >= top_n:
                out.append(row)
                continue

            resume = self._resumes.get(row.resume_id)
            try:
                text = self._llm.generate(
                    self._report_prompt.render(
                        requirement_json=requirement_json,
                        candidate_json=_candidate_json(resume),
                        score_json=_score_json(row),
                    )
                )
            except Exception:
                # 单个候选人失败不该让整次筛选作废 → run 置 partial
                failed += 1
                out.append(row)
                continue

            analyzed += 1
            out.append(
                ResultRow(
                    resume_id=row.resume_id,
                    rank=row.rank,
                    overall=row.overall,
                    semantic=row.semantic,
                    dims=row.dims,
                    passed=row.passed,
                    reject_reason=row.reject_reason,
                    analysis_text=text,
                    analysis_prompt_version=self._report_prompt.version,
                )
            )

        return out, analyzed, failed


# ============================================================
# 辅助
# ============================================================

def _rank(rows: list[ResultRow]) -> list[ResultRow]:
    """通过的按总分降序在前，淘汰的排后面（仍保留名次，便于展示）。

    总分为 None（六维全缺失且无语义分）排最后，而不是当 0 分 ——
    "没数据"和"得 0 分"是两件事。
    """
    ordered = sorted(
        rows,
        key=lambda r: (not r.passed, r.overall is None, -(r.overall or 0.0)),
    )
    return [
        ResultRow(
            resume_id=r.resume_id,
            rank=i,
            overall=r.overall,
            semantic=r.semantic,
            dims=r.dims,
            passed=r.passed,
            reject_reason=r.reject_reason,
            analysis_text=r.analysis_text,
            analysis_prompt_version=r.analysis_prompt_version,
        )
        for i, r in enumerate(ordered, start=1)
    ]


def _candidate_json(resume: Resume | None) -> str:
    if resume is None or resume.metadata is None:
        return "{}"
    return json.dumps(asdict(resume.metadata), ensure_ascii=False, default=str)


def _score_json(row: ResultRow) -> str:
    return json.dumps(
        {
            "overall": row.overall,
            "semantic": row.semantic,
            "dims": [asdict(d) for d in row.dims],
        },
        ensure_ascii=False,
    )


__all__ = ["ScreeningService", "ScreeningResult", "idempotency_key"]
