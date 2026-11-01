"""IngestionService 的单测。

全部依赖都是假实现：不读真 PDF、不调 LLM、不碰 Ollama、不写磁盘。
这正是"services 只依赖端口"带来的回报 —— 编排逻辑可以被快照式验证。
"""

import pytest

from app.domain.models import Resume, ResumeStatus
from app.ports.document_parser import DocumentParseError, ParsedDocument
from app.ports.embedding import EmbeddingError
from app.ports.llm import LLMError
from app.ports.vector_store import ResumeDoc
from app.prompts import Prompt
from app.services.ingestion import IngestionService, build_embedding_text
from app.services.schemas import ExtractedResume

PROMPT = Prompt(name="resume_extract", version="v1", text="抽取：${raw_text}")


# ============================================================
# 假实现
# ============================================================

class FakeRepo:
    def __init__(self):
        self.rows: dict[str, Resume] = {}
        self.updates: list[ResumeStatus] = []
        self.version = 0

    def add(self, resume):
        self.rows[resume.id] = resume
        return resume.id

    def get(self, resume_id):
        return self.rows.get(resume_id)

    def find_by_content_hash(self, sha256):
        return next((r for r in self.rows.values() if r.content_sha256 == sha256), None)

    def list(self, *, limit, offset, status=None):
        return list(self.rows.values())

    def update(self, resume):
        self.rows[resume.id] = resume
        self.updates.append(resume.status)

    def delete(self, resume_id):
        self.rows.pop(resume_id, None)

    def corpus_version(self):
        return self.version

    def bump_corpus_version(self):
        self.version += 1
        return self.version


class FakeParser:
    def __init__(self, text="张三，三年 Python 经验，本科"):
        self.text = text
        self.calls = 0

    def supports(self, filename, head):
        return True

    def parse(self, path):
        self.calls += 1
        return ParsedDocument(text=self.text, page_count=1, source_suffix=".pdf")


class FakeLLM:
    def __init__(self, payload=None):
        self.payload = payload or ExtractedResume(
            name="张三",
            skills=["Python", "FastAPI"],
            experiences=[{"title": "后端工程师", "company": "某公司"}],
            educations=[{"institution": "某大学", "degree_level": 2}],
        )
        self.calls = 0

    def extract(self, prompt, schema):
        self.calls += 1
        return self.payload

    def generate(self, prompt):
        return "报告"


class FakeEmbedder:
    def __init__(self):
        self.texts: list[str] = []

    def embed(self, texts):
        self.texts.extend(texts)
        return [tuple([0.1] * 8) for _ in texts]


class FakeVectors:
    def __init__(self):
        self.docs: list[ResumeDoc] = []

    def upsert(self, docs):
        self.docs.extend(docs)

    def search(self, vector, *, top_k, min_similarity):
        return []

    def delete(self, resume_ids):
        pass

    def drop_and_rebuild(self):
        pass


def _service(parser=None, llm=None, embedder=None, vectors=None, repo=None):
    return IngestionService(
        repo=repo or FakeRepo(),
        parser=parser or FakeParser(),
        llm=llm or FakeLLM(),
        embedder=embedder or FakeEmbedder(),
        vectors=vectors or FakeVectors(),
        prompt=PROMPT,
    )


def _ingest(svc, sha="sha-1"):
    return svc.ingest(filename="简历.pdf", blob_path="blob/简历.pdf", content_sha256=sha)


# ============================================================
# 正常路径
# ============================================================

def test_happy_path_reaches_indexed():
    repo, vectors = FakeRepo(), FakeVectors()
    result = _ingest(_service(repo=repo, vectors=vectors))

    assert result.deduplicated is False
    assert result.resume.status == ResumeStatus.INDEXED
    assert result.resume.metadata.name == "张三"
    # 每一步都落过库，崩在哪一步都查得到
    assert repo.updates == [
        ResumeStatus.PARSED,
        ResumeStatus.EXTRACTED,
        ResumeStatus.INDEXED,
    ]
    assert len(vectors.docs) == 1
    assert vectors.docs[0].resume_id == result.resume.id


def test_prompt_version_is_recorded():
    result = _ingest(_service())
    assert result.resume.extractor_prompt_version == "v1"


def test_duplicate_content_is_not_reprocessed():
    repo, parser = FakeRepo(), FakeParser()
    svc = _service(repo=repo, parser=parser)

    first = _ingest(svc, sha="same")
    second = _ingest(svc, sha="same")

    assert second.deduplicated is True
    assert second.resume.id == first.resume.id
    assert parser.calls == 1  # 第二次根本没解析


# ============================================================
# 异步模式：API 预建 queued 行，worker 用同一个 id 继续
# ============================================================

def test_ingest_reuses_prebuilt_resume_id():
    repo = FakeRepo()
    repo.add(Resume(
        id="r1", filename="简历.pdf", content_sha256="sha-1",
        blob_path="blob/简历.pdf", status=ResumeStatus.QUEUED,
    ))
    svc = _service(repo=repo)

    result = svc.ingest(
        filename="简历.pdf", blob_path="blob/简历.pdf",
        content_sha256="sha-1", resume_id="r1",
    )

    assert result.resume.id == "r1"
    assert result.resume.status == ResumeStatus.INDEXED
    assert len(repo.rows) == 1  # 没有多出一行


def test_repeated_delivery_of_finished_task_is_noop():
    """可见性锁过期导致任务被重新领取：已 index 的简历不该再花一次 LLM 钱。"""
    repo, parser, llm = FakeRepo(), FakeParser(), FakeLLM()
    svc = _service(repo=repo, parser=parser, llm=llm)

    first = svc.ingest(filename="a.pdf", blob_path="b", content_sha256="sha-1")
    again = svc.ingest(filename="a.pdf", blob_path="b",
                       content_sha256="sha-1", resume_id=first.resume.id)

    assert again.deduplicated is True
    assert parser.calls == 1
    assert llm.calls == 1


# ============================================================
# 失败路径：状态必须落 failed，且带人能看懂的原因
# ============================================================

def test_empty_text_marks_failed():
    svc = _service(parser=FakeParser(text="   "))

    with pytest.raises(DocumentParseError):
        _ingest(svc)

    resume = next(iter(svc._repo.rows.values()))
    assert resume.status == ResumeStatus.FAILED
    assert "扫描版" in resume.error


def test_llm_error_marks_failed():
    class BadLLM(FakeLLM):
        def extract(self, prompt, schema):
            raise LLMError("模型拒绝回答")

    svc = _service(llm=BadLLM())

    with pytest.raises(LLMError):
        _ingest(svc)

    resume = next(iter(svc._repo.rows.values()))
    assert resume.status == ResumeStatus.FAILED
    assert resume.error.startswith("抽取失败")


def test_embedding_error_marks_failed_and_writes_no_vector():
    class BadEmbedder(FakeEmbedder):
        def embed(self, texts):
            raise EmbeddingError("Ollama 没起")

    vectors = FakeVectors()
    svc = _service(embedder=BadEmbedder(), vectors=vectors)

    with pytest.raises(EmbeddingError):
        _ingest(svc)

    assert vectors.docs == []
    resume = next(iter(svc._repo.rows.values()))
    assert resume.status == ResumeStatus.FAILED
    assert resume.error.startswith("嵌入失败")


# ============================================================
# 嵌入文本的拼装
# ============================================================

def test_embedding_text_uses_structured_fields():
    meta = FakeLLM().payload.to_domain()
    text = build_embedding_text(meta, raw_text="很长的正文" * 100)

    assert "后端工程师" in text
    assert "技能：Python、FastAPI" in text
    assert "本科" in text


def test_embedding_text_falls_back_to_raw_text_when_metadata_is_thin():
    """抽取没拿到东西时，不能嵌入一个空串。"""
    meta = ExtractedResume(name="张三").to_domain()
    text = build_embedding_text(meta, raw_text="这是正文" * 200)

    assert "这是正文" in text
