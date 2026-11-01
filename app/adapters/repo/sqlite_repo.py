"""SQLite 仓储实现：领域对象 ↔ 数据库行。
本仓储职责：把领域对象拆成多张表写入；查询时从多表读出，拼装回领域对象
本文件只做翻译，不做业务判断：
    业务判断（去重要不要返回 200、失败算不算终态）在 services；
    这里只回答「怎么把 Resume 拆成几张表的行，再原样拼回来」。

一个 add() 要写 6 张表
    resumes（1 行）
    resume_metadata（1 行，整份 JSON）
    resume_skills / resume_locations / resume_education / resume_experience
    —— 全部在**一个事务**里。否则中途失败会留下"有简历没技能"的半成品。
"""

# 同 ports/repository.py：RunRepository 里有名为 `list` 的方法，
# 后面的 `list[ResultRow]` 注解必须在定义时不被求值，否则会被解析成那个方法。
from __future__ import annotations

import hashlib
import json     #序列化
import sqlite3
from dataclasses import asdict  #- dataclass 对象转字典，用于序列化

from app.adapters.repo.db import Database, now_iso  #数据库连接管理类、UTC 时间工具函数
from app.domain.models import (
    DateRange,
    DimScore,
    Education,
    Resume,
    ResumeMetadata,
    ResumeStatus,
    Run,
    RunStage,
    RunStatus,
    WorkExperience,
)
from app.domain.text import normalize_token #归一化函数，用来生成`skill_norm`、`location_norm`
from app.ports.repository import ResultRow

# ============================================================
# ResumeMetadata ↔ JSON
#
# 为什么用 dataclasses.asdict 而不是手写映射？
#    手写映射在加字段时会静默漏掉（新增的字段不会进 JSON，读回来是默认值），
#    asdict 自动跟着 dataclass 定义走。
# 反过来，反序列化必须手写：JSON 里没有类型信息，
#    "period" 是 dict 还是 DateRange，要靠人告诉它。
# ============================================================


def _period_from_dict(d: dict) -> DateRange:
    #私有函数作用：把 JSON 里面的时间段字典，还原成领域模型`DateRange`对象
    return DateRange(
        start_ym=d.get("start_ym"),
        end_ym=d.get("end_ym"),
        is_current=bool(d.get("is_current", False)),
    )


def metadata_to_json(meta: ResumeMetadata) -> str:
    """序列化函数：把 ResumeMetadata 转成 JSON 文本，存进 resume_metadata.data_json。"""
    
    #asdict(meta)：把 ResumeMetadata 这个 dataclass 完整转为 python 字典
    return json.dumps(asdict(meta), ensure_ascii=False)
    #`json.dumps()`字典序列化为 JSON 文本
    #`ensure_ascii=False`：不把中文转成 \uXXXX 编码,数据库中存储的 JSON 可以直接肉眼查看中文

def metadata_from_json(raw: str) -> ResumeMetadata:
    """反序列化函数：把 resume_metadata.data_json 读出来的 JSON 文本，转成 ResumeMetadata。"""
    d = json.loads(raw)  #把数据库读出的 JSON 字符串，加载成普通 Python 字典
    return ResumeMetadata(

        #基础字段：name /email/phone /expected_salary，直接`d.get()`读取
        name=d.get("name"),
        email=d.get("email"),
        phone=d.get("phone"),
        skills=tuple(d.get("skills") or ()),
        industries=tuple(d.get("industries") or ()),
        preferred_locations=tuple(d.get("preferred_locations") or ()),
        tags=tuple(d.get("tags") or ()),
        #educations 循环构建 `Education` 对象
        educations=tuple(
            Education(
                institution=e.get("institution"),
                major=e.get("major"),
                degree_raw=e.get("degree_raw"),
                degree_level=e.get("degree_level", 0),
                #内部调用_period_from_dict，把 period 字典转为 DateRange 时间段对象
                period=_period_from_dict(e.get("period") or {}),
            )
            for e in d.get("educations") or ()
        ),
        experiences=tuple(
            WorkExperience(
                company=e.get("company"),
                title=e.get("title"),
                period=_period_from_dict(e.get("period") or {}),
            )
            for e in d.get("experiences") or ()
        ),
        expected_salary=d.get("expected_salary"),
    )


# ============================================================
# DimScore[] ↔ JSON（results.dim_scores_json）
#
# 为什么要存明细而不是只存总分：
#   参考项目在 API 层伪造单项分（routes.py:71-91），根因就是只存了总分。
#   存下 DimScore(score, weight, reason) 三元组后，报告和前端都能拿到依据，
#   而且 weight 是**本次实际使用**的那份，事后能复算出同样的总分。
# ============================================================


def dims_to_json(dims: tuple[DimScore, ...]) -> str:
    return json.dumps([asdict(d) for d in dims], ensure_ascii=False)


def dims_from_json(raw: str | None) -> tuple[DimScore, ...]:
    if not raw:
        return ()
    return tuple(
        DimScore(
            name=d["name"],
            score=d.get("score"),
            weight=d.get("weight", 0.0),
            reason=d.get("reason", ""),
        )
        for d in json.loads(raw)
    )


# ============================================================
# 行 → 领域对象
# ============================================================

# 所有查询共用这一份 SELECT：元数据用 LEFT JOIN 一次带出来，
# 避免"先查 100 行简历、再循环查 100 次元数据"的 N+1 问题。

# SQL 语句。注意三引号内部只能是 SQL —— SQLite 的注释是 `--` 或 `/* */`，
# 把 Python 的 `#` 注释写进字符串会直接语法错误。
_SELECT_RESUME = """
SELECT r.id, r.filename, r.content_sha256, r.raw_text, r.blob_path,
       r.status, r.error, r.created_at,
       m.data_json,
       COALESCE(m.extractor_prompt_version, 'unknown') AS extractor_prompt_version
  FROM resumes r
  LEFT JOIN resume_metadata m ON m.resume_id = r.id
"""

#转换函数
def _row_to_resume(row: sqlite3.Row) -> Resume:
    """接收 sqlite 查询返回的sqlite3.Row一行数据,转换成:领域对象 Resume"""
    raw_meta = row["data_json"] #取出 JOIN 拿到的元数据 JSON 字符串
    return Resume(
        id=row["id"],
        filename=row["filename"],
        content_sha256=row["content_sha256"],
        raw_text=row["raw_text"],
        metadata=metadata_from_json(raw_meta) if raw_meta else None,
        #如果`raw_meta`不为 NULL，调用上一节写好的`metadata_from_json`，把 JSON 字符串反序列化为`ResumeMetadata`对象；
        
        blob_path=row["blob_path"],
        status=ResumeStatus(row["status"]),
        error=row["error"],
        created_at=row["created_at"],
        extractor_prompt_version=row["extractor_prompt_version"],
    )


# ============================================================
# 简历仓储
# ============================================================


class SqliteResumeRepository:
    """实现 ports.repository.ResumeRepository。

    没有显式继承那个 Protocol —— 结构化子类型，方法齐了就算实现。
    """

    #构造函数注入`Database`实例（依赖注入），仓储本身不创建数据库连接,方便单元测试时替换成内存数据库
    def __init__(self, db: Database) -> None:
        self._db = db  #`self._db`保存数据库管理器。

    # ---- 写 ----

    def add(self, resume: Resume) -> str:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO resumes
                    (id, filename, content_sha256, blob_path, raw_text,
                     char_count, status, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resume.id,
                    resume.filename,
                    resume.content_sha256,
                    resume.blob_path,
                    resume.raw_text,
                    len(resume.raw_text) if resume.raw_text else None,
                    resume.status.value,
                    resume.error,
                    resume.created_at or now_iso(),
                ),
            )
            self._write_metadata(conn, resume)
        return resume.id

    def update(self, resume: Resume) -> None:
        """整体覆盖。content_sha256 不在 SET 里 —— 文件内容不会变，
        能变的是它走到哪一步了。"""
        with self._db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE resumes
                   SET filename = ?, blob_path = ?, raw_text = ?, char_count = ?,
                       status = ?, error = ?
                 WHERE id = ?
                """,
                (
                    resume.filename,
                    resume.blob_path,
                    resume.raw_text,
                    len(resume.raw_text) if resume.raw_text else None,
                    resume.status.value,
                    resume.error,
                    resume.id,
                ),
            )
            if cur.rowcount == 0: #获取 SQL 影响行数。如果更新行数 = 0，说明该简历 id 不存在，抛`KeyError`
                raise KeyError(f"resume {resume.id} 不存在，无法更新")
            self._write_metadata(conn, resume)

    def delete(self, resume_id: str) -> None:
        # 拆分表由 schema 里的 ON DELETE CASCADE 负责，这里只删主行
        self._db.execute("DELETE FROM resumes WHERE id = ?", (resume_id,))

    # ---- 语料版本 ----

    def corpus_version(self) -> int:
        row = self._db.query_one("SELECT MAX(version) AS v FROM corpus_versions")
        return int(row["v"]) if row is not None and row["v"] is not None else 0

    def bump_corpus_version(self) -> int:
        """简历库变了就 +1，同时记下当时的规模与内容指纹。

        指纹用"全部 content_sha256 排序后再哈希"：
        顺序固定（否则同样的库每次算出来都不一样），
        且能区分"删一份又加一份"这种数量不变但内容变了的情况。
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(GROUP_CONCAT(h, ','), '') AS hashes "
                "FROM (SELECT content_sha256 AS h FROM resumes ORDER BY content_sha256)"
            ).fetchone()
            fingerprint = hashlib.sha256(row["hashes"].encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO corpus_versions (resume_count, content_hash, created_at) "
                "VALUES (?, ?, ?)",
                (row["n"], fingerprint, now_iso()),
            )
            new_version = conn.execute(
                "SELECT MAX(version) AS v FROM corpus_versions"
            ).fetchone()["v"]
        return int(new_version)

    # ---- 读 ----

    def get(self, resume_id: str) -> Resume | None:
        """根据简历 id 查询，拼接前面定义好的`_SELECT_RESUME`左连接 SQL;
        查到行就调用`_row_to_resume`转为领域对象，无数据返回 None。"""
        row = self._db.query_one(_SELECT_RESUME + " WHERE r.id = ?", (resume_id,))
        return _row_to_resume(row) if row is not None else None

    def find_by_content_hash(self, sha256: str) -> Resume | None:
        """简历去重。上传简历文件，计算文件 sha256,调用这个方法查库,判断相同文件是否已经入库"""
        row = self._db.query_one(
            _SELECT_RESUME + " WHERE r.content_sha256 = ?", (sha256,)
        )
        return _row_to_resume(row) if row is not None else None

    def list(
        self,
        *,
        limit: int,  #`limit`一页多少条
        offset: int, #`offset`偏移量
        status: ResumeStatus | None = None,
    ) -> list[Resume]:
        if status is None:
            rows = self._db.query(
                _SELECT_RESUME
                + " ORDER BY r.created_at DESC, r.id LIMIT ? OFFSET ?",
                (limit, offset),
            )
        else:
            rows = self._db.query(
                _SELECT_RESUME
                + " WHERE r.status = ? ORDER BY r.created_at DESC, r.id LIMIT ? OFFSET ?",
                (status.value, limit, offset),
            )
        return [_row_to_resume(r) for r in rows]

    # ---- 内部：写元数据与拆表 ----
    
    def _write_metadata(self, conn: sqlite3.Connection, resume: Resume) -> None:
        """写入 resume_metadata 和 4 张拆表。先删后插，全量覆盖

        先删后插，而不是想办法 UPDATE:
            update() 和 add() 共用这一条路径，
            而且"这个人换了工作经历"在拆表层面没有稳定的行标识，
            整组替换是最不容易出错的做法。
        """
        meta = resume.metadata
        if meta is None:    #如果简历没有抽取出来的元数据，直接返回，不写入子表
            return

        conn.execute( #删除旧的 metadata 记录
            "DELETE FROM resume_metadata WHERE resume_id = ?", (resume.id,)
        )
        conn.execute( #插入新的 JSON 元数据，更新`updated_at`为当前 UTC 时间。
            "INSERT INTO resume_metadata"
            " (resume_id, data_json, extractor_prompt_version, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (
                resume.id,
                metadata_to_json(meta),
                resume.extractor_prompt_version,
                now_iso(),
            ),
        )

        for table in (  #循环 4 张子表，删除当前简历所有旧记录
            "resume_skills",
            "resume_locations",
            "resume_education",
            "resume_experience",
        ):
            conn.execute(f"DELETE FROM {table} WHERE resume_id = ?", (resume.id,))

        # INSERT OR IGNORE：如果违反唯一约束，直接忽略这条，不抛出异常.同一个人的重复技能（"Python" 和 "python"）
        # 归一化后撞上复合主键，忽略即可，不该让整个上传失败。
        conn.executemany(
            "INSERT OR IGNORE INTO resume_skills (resume_id, skill_raw, skill_norm)"
            " VALUES (?, ?, ?)",
            [
                (resume.id, s, normalize_token(s))
                for s in meta.skills
                if normalize_token(s)
            ],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO resume_locations (resume_id, location_norm)"
            " VALUES (?, ?)",
            [
                (resume.id, normalize_token(loc))
                for loc in meta.preferred_locations
                if normalize_token(loc)
            ],
        )
        conn.executemany(
            "INSERT INTO resume_education"
            " (resume_id, institution, major, degree_raw, degree_level,"
            "  start_ym, end_ym, is_current)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    resume.id,
                    e.institution,
                    e.major,
                    e.degree_raw or "",          # NOT NULL，缺失存空串
                    e.degree_level,
                    e.period.start_ym,
                    e.period.end_ym,
                    int(e.period.is_current),
                )
                for e in meta.educations
            ],
        )
        # months 列故意不填：一段"至今"的经历，长度取决于查询当天的日期。
        # 落库就等于把写入时刻的时钟固化进去，一年后它会凭空多 12 个月。
        # 需要长度时由 domain.text.merge_experience_months 实时算。
        conn.executemany(
            "INSERT INTO resume_experience"
            " (resume_id, company, title, start_ym, end_ym, is_current)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    resume.id,
                    e.company,
                    e.title,
                    e.period.start_ym,
                    e.period.end_ym,
                    int(e.period.is_current),
                )
                for e in meta.experiences
            ],
        )


# ============================================================
# 筛选任务仓储
# ============================================================

_SELECT_RUN = """
SELECT id, query_text, profile, status, stage, idempotency_key,
       config_snapshot_json, progress_json,
       recall_n, filtered_n, scored_n, analyzed_n,
       error, created_at, finished_at
  FROM runs
"""


def _row_to_run(row: sqlite3.Row) -> Run:
    raw_progress = row["progress_json"]
    return Run(
        id=row["id"],
        query_text=row["query_text"],
        profile=row["profile"],
        status=RunStatus(row["status"]),
        stage=RunStage(row["stage"]) if row["stage"] else None,
        idempotency_key=row["idempotency_key"] or "",
        config_snapshot_json=row["config_snapshot_json"],
        # JSON 对象在 Python 3.7+ 保持插入顺序，转回 tuple 仍然稳定
        progress=tuple(json.loads(raw_progress).items()) if raw_progress else (),
        recall_n=row["recall_n"],
        filtered_n=row["filtered_n"],
        scored_n=row["scored_n"],
        analyzed_n=row["analyzed_n"],
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


class SqliteRunRepository:
    """实现 ports.repository.RunRepository。

    三个写方法（create / update_stage / finish）各自对应一条固定 SQL，
    而不是「读整行 → 改 → 写回」。原因见 ports/repository.py 的说明：
    API 进程建任务、worker 进程推进度，整体覆盖会把对方刚写的值冲掉。
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, run: Run) -> str:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runs
                    (id, query_text, profile, config_snapshot_json,
                     idempotency_key, status, stage, progress_json,
                     error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.query_text,
                    run.profile,
                    run.config_snapshot_json,
                    # 列上有 UNIQUE：空字符串只能有一条，
                    # 用 None 表示"没有幂等键"，SQLite 允许多个 NULL
                    run.idempotency_key or None,
                    run.status.value,
                    run.stage.value if run.stage else None,
                    json.dumps(dict(run.progress), ensure_ascii=False)
                    if run.progress else None,
                    run.error,
                    run.created_at or now_iso(),
                ),
            )
        return run.id

    def get(self, run_id: str) -> Run | None:
        row = self._db.query_one(_SELECT_RUN + " WHERE id = ?", (run_id,))
        return _row_to_run(row) if row is not None else None

    def update_stage(
        self,
        run_id: str,
        stage: RunStage,
        progress: dict[str, int],
    ) -> None:
        """推进阶段。同时把 status 置为 running（能推进说明在跑）。"""
        with self._db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                   SET status = ?, stage = ?, progress_json = ?
                 WHERE id = ? AND status IN (?, ?)
                """,
                (
                    RunStatus.RUNNING.value,
                    stage.value,
                    json.dumps(progress, ensure_ascii=False),
                    run_id,
                    RunStatus.QUEUED.value,
                    RunStatus.RUNNING.value,
                ),
            )
            if cur.rowcount == 0:
                # 两种可能：run 不存在，或已被取消/已结束。
                # 抛异常让 worker 停下——已经取消的任务不该继续烧 token。
                raise KeyError(f"run {run_id} 不存在或已不在运行中")

    def finish(
        self,
        run_id: str,
        status: RunStatus,
        counts: dict[str, int],
        *,
        error: str | None = None,
    ) -> None:
        """落终态。

        counts 用 COALESCE 更新：调用方没提供的键保留库里原值，
        不会因为只传了两个数字就把另外两个清零。
        """
        if status is RunStatus.FAILED and not error:
            raise ValueError("status=failed 时必须给出 error")

        stage = RunStage.DONE.value if status in (
            RunStatus.SUCCEEDED, RunStatus.PARTIAL
        ) else None   # 失败/取消时保留失败发生的阶段，便于排查

        with self._db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                   SET status = ?, stage = COALESCE(?, stage), error = ?,
                       recall_n   = COALESCE(?, recall_n),
                       filtered_n = COALESCE(?, filtered_n),
                       scored_n   = COALESCE(?, scored_n),
                       analyzed_n = COALESCE(?, analyzed_n),
                       finished_at = ?
                 WHERE id = ?
                """,
                (
                    status.value,
                    stage,
                    error,
                    counts.get("recall"),
                    counts.get("filtered"),
                    counts.get("scored"),
                    counts.get("analyzed"),
                    now_iso(),
                    run_id,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"run {run_id} 不存在")

    def find_by_idempotency_key(self, key: str) -> Run | None:
        if not key:
            return None
        row = self._db.query_one(
            _SELECT_RUN + " WHERE idempotency_key = ?", (key,)
        )
        return _row_to_run(row) if row is not None else None

    def list(self, *, limit: int, offset: int) -> list[Run]:
        rows = self._db.query(
            _SELECT_RUN + " ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [_row_to_run(r) for r in rows]

    # ---- results ----

    def save_results(self, run_id: str, rows: list[ResultRow]) -> None:
        """整批覆盖：先删后插，和 _write_metadata 是同一个思路。

        重跑同一个 run（比如评分配置改完重算）时，旧结果必须消失；
        逐条 upsert 还得处理"这次比上次少了几个人"的残留。
        """
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
            conn.executemany(
                """
                INSERT INTO results
                    (run_id, resume_id, rank, overall_score, semantic_score,
                     dim_scores_json, hard_filter_passed, reject_reason,
                     analysis_text, analysis_prompt_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        row.resume_id,
                        row.rank,
                        row.overall,
                        row.semantic,
                        dims_to_json(row.dims),
                        1 if row.passed else 0,
                        row.reject_reason,
                        row.analysis_text,
                        row.analysis_prompt_version,
                    )
                    for row in rows
                ],
            )

    def list_results(self, run_id: str) -> list[ResultRow]:
        rows = self._db.query(
            """
            SELECT resume_id, rank, overall_score, semantic_score,
                   dim_scores_json, hard_filter_passed, reject_reason,
                   analysis_text, analysis_prompt_version
            FROM results
            WHERE run_id = ?
            ORDER BY rank
            """,
            (run_id,),
        )
        return [
            ResultRow(
                resume_id=r["resume_id"],
                rank=r["rank"],
                overall=r["overall_score"],
                semantic=r["semantic_score"],
                dims=dims_from_json(r["dim_scores_json"]),
                passed=bool(r["hard_filter_passed"]),
                reject_reason=r["reject_reason"],
                analysis_text=r["analysis_text"],
                analysis_prompt_version=r["analysis_prompt_version"],
            )
            for r in rows
        ]
