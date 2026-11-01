"""入库编排：文件已落盘 → 可被召回的简历。

    解析 PDF → LLM 抽取 → 落库（拆表）→ 嵌入 → 写向量索引

职责边界
--------
    **不碰 HTTP、不碰文件上传**。调用方（路由或 worker）负责把字节写进
    blob_path、算出 content_sha256，然后调 `ingest()`。
    这样同一段编排既能同步跑，也能挂到 worker 里异步跑，一行不用改。

状态机（ResumeStatus）
---------------------
    queued → parsed → extracted → indexed
    任一步抛异常 → failed + error

    每一步都**先落库再进入下一步**：进程在第三步崩了，库里能看到
    status=extracted，知道卡在嵌入。若只在最后写一次，崩了就是零信息。

为什么失败要 re-raise
--------------------
    这里把 status=failed 落库后**继续抛出**。因为"失败了"和"失败原因
    要不要展示给用户"是两个决定：worker 需要知道任务没成功，
    而 HTTP 层可能只想返回 202。服务层不替它们做决定。
"""

import uuid
from dataclasses import dataclass, replace

from app.domain.models import DEGREE_NAMES, Resume, ResumeMetadata, ResumeStatus
from app.ports.document_parser import DocumentParseError, DocumentParserPort
from app.ports.embedding import EmbeddingError, EmbeddingPort
from app.ports.llm import LLMError, LLMPort
from app.ports.repository import ResumeRepository
from app.ports.vector_store import ResumeDoc, VectorStorePort
from app.prompts import Prompt, load
from app.services.schemas import ExtractedResume


@dataclass(frozen=True)
class IngestResult:
    """入库结果。deduplicated=True 表示命中了同内容的简历，没有重跑流水线。"""

    resume: Resume
    deduplicated: bool


def build_embedding_text(meta: ResumeMetadata, raw_text: str) -> str:
    """拼出用于嵌入的文本。

    为什么不直接嵌入整份 raw_text：
        简历里的自我评价、项目罗列、获奖经历对"岗位匹配"几乎无信息量，
        却会稀释向量方向；而且长文本嵌一次贵、慢、还可能超上下文。
        结构化字段是 LLM 已经浓缩过的信号，更干净。

    为什么还要兜底 raw_text：
        抽取质量差（模型把字段全抽空）时，metadata 拼出来是空串，
        嵌入一个空串毫无意义。此时退回到正文前 1000 字，
        至少还能靠语义召回兜住这类简历。
    """
    parts: list[str] = []

    # 岗位 + 公司：最能定义"这个人是干什么的"
    for exp in meta.experiences[:5]:
        line = " ".join(x for x in (exp.title, exp.company) if x)
        if line:
            parts.append(line)

    if meta.skills:
        parts.append("技能：" + "、".join(meta.skills[:50]))
    if meta.industries:
        parts.append("行业：" + "、".join(meta.industries))
    if meta.preferred_locations:
        parts.append("期望地点：" + "、".join(meta.preferred_locations))

    # 学历：专业比学校名对匹配更有用。
    # degree_raw 可能为空（模型只给了等级），这时用 DEGREE_NAMES 还原成中文，
    # 否则"本科/硕士"这个信号在嵌入文本里就凭空消失了。
    for edu in meta.educations[:2]:
        # level=0 是"未知"，别把"未知"两个字塞进嵌入文本占位置
        degree = edu.degree_raw or (
            DEGREE_NAMES.get(edu.degree_level, "") if edu.degree_level else ""
        )
        line = " ".join(x for x in (degree, edu.major, edu.institution) if x)
        if line:
            parts.append(line)

    text = "\n".join(parts)
    if len(text) < 50:
        # 抽取几乎没拿到东西 → 退回正文
        text = f"{text}\n{raw_text[:1000]}".strip()
    return text


def _describe(exc: Exception) -> str:
    """把异常翻译成给人看的一句话，落进 resumes.error。"""
    if isinstance(exc, DocumentParseError):
        return f"解析失败：{exc}"
    if isinstance(exc, LLMError):
        return f"抽取失败：{exc}"
    if isinstance(exc, EmbeddingError):
        return f"嵌入失败：{exc}"
    return f"{type(exc).__name__}: {exc}"


class IngestionService:
    """实现"上传 → 可被召回"这一段。所有依赖都是端口，可注入假实现。"""

    def __init__(
        self,
        *,
        repo: ResumeRepository,
        parser: DocumentParserPort,
        llm: LLMPort,
        embedder: EmbeddingPort,
        vectors: VectorStorePort,
        prompt: Prompt | None = None,
    ) -> None:
        self._repo = repo
        self._parser = parser
        self._llm = llm
        self._embedder = embedder
        self._vectors = vectors
        # prompt 可注入：测试里传一个固定模板，避免依赖文件内容
        self._prompt = prompt or load("resume_extract")

    # ------------------------------------------------------------

    def ingest(
        self,
        *,
        filename: str,
        blob_path: str,
        content_sha256: str,
        resume_id: str | None = None,
    ) -> IngestResult:
        """解析 → 抽取 → 索引。

        resume_id 由 API 层在上传时预先生成并落库（`status=queued`），
        这样 POST /resumes 能立刻把 id 返回给用户，而处理交给 worker。
        """
        # ① 任务重复投递（worker 超时后又被领取）：已经处理完的直接返回
        resume = self._repo.get(resume_id) if resume_id else None
        if resume is not None and resume.status is ResumeStatus.INDEXED:
            return IngestResult(resume=resume, deduplicated=True)

        # ② 内容去重：同一份 PDF 上传两次只留一行。
        #    参考项目每次上传都生成新 uuid，同一个人会出现两份（CLAUDE.md §6）。
        if resume is None:
            existing = self._repo.find_by_content_hash(content_sha256)
            if existing is not None:
                return IngestResult(resume=existing, deduplicated=True)

        # ③ 先建行再干活：崩了也能看到"这份简历正在处理"
        if resume is None:
            resume = Resume(
                id=resume_id or uuid.uuid4().hex,
                filename=filename,
                content_sha256=content_sha256,
                blob_path=blob_path,
                status=ResumeStatus.QUEUED,
            )
            self._repo.add(resume)

        try:
            resume = self._parse(resume)
            resume = self._extract(resume)
            resume = self._index(resume)
        except Exception as exc:
            # 落失败态后继续抛：worker 需要知道这次没成功
            self._repo.update(
                replace(resume, status=ResumeStatus.FAILED, error=_describe(exc))
            )
            raise

        return IngestResult(resume=resume, deduplicated=False)

    # ------------------------------------------------------------
    # 三个阶段，每个都是「读入 Resume → 落库 → 返回新 Resume」
    # ------------------------------------------------------------

    def _parse(self, resume: Resume) -> Resume:
        doc = self._parser.parse(resume.blob_path or "")

        if doc.is_empty:
            # 空文本是合法结果（扫描件没有文字层），但没法继续。
            # 用专门的文案，提示用户换文件 —— 这和他的简历内容无关。
            raise DocumentParseError(
                "未提取到文字，可能是扫描版 PDF 或图片，请上传文本型文件"
            )

        updated = replace(resume, raw_text=doc.text, status=ResumeStatus.PARSED)
        self._repo.update(updated)
        return updated

    def _extract(self, resume: Resume) -> Resume:
        prompt_text = self._prompt.render(raw_text=resume.raw_text or "")

        # extract 内部已经做了"结构不合法就重试"，失败才抛 LLMError
        dto = self._llm.extract(prompt_text, ExtractedResume)

        updated = replace(
            resume,
            metadata=dto.to_domain(),
            status=ResumeStatus.EXTRACTED,
            # 记下用的是哪版提示词：将来发现某批简历抽得差，能定位到版本
            extractor_prompt_version=self._prompt.version,
        )
        self._repo.update(updated)
        return updated

    def _index(self, resume: Resume) -> Resume:
        text = build_embedding_text(resume.metadata, resume.raw_text or "")
        vector = self._embedder.embed([text])[0]

        # 同 id 覆盖，重复入库不会产生孤儿向量
        self._vectors.upsert(
            [ResumeDoc(resume_id=resume.id, text=text, vector=vector)]
        )

        updated = replace(resume, status=ResumeStatus.INDEXED)
        self._repo.update(updated)

        # 简历库变了 → 语料版本 +1，旧 run 的幂等键自动失效。
        # 否则新入库的简历永远不会出现在"同 query 同 profile"的历史结果里。
        self._repo.bump_corpus_version()
        return updated


__all__ = ["IngestionService", "IngestResult", "build_embedding_text"]
