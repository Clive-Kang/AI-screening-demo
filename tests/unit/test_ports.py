"""app/ports/ 的单测：值对象的不变量 + 协议的结构化子类型判定。

端口里没有算法，所以这里测的不是"算得对不对"，而是两件事：
    1. 值对象造不出自相矛盾的数据（Hit(0.9, similarity=1.7) 必须报错）
    2. 一个没继承 VectorStorePort 的类，只要方法齐了就算实现
"""

import pytest

from app.domain.models import Resume, ResumeMetadata, ResumeStatus
from app.ports.document_parser import DocumentParserPort, ParsedDocument
from app.ports.embedding import EmbeddingPort
from app.ports.llm import LLMPort
from app.ports.repository import ResumeRepository, RunRepository
from app.ports.task_queue import Task, TaskKind, TaskQueuePort
from app.ports.vector_store import Hit, ResumeDoc, VectorStorePort

# ============================================================
# Hit / ResumeDoc
# ============================================================


def test_hit_accepts_similarity_in_range():
    assert Hit("r1", 0.87).similarity == 0.87


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_hit_rejects_similarity_out_of_range(bad):
    with pytest.raises(ValueError, match="similarity"):
        Hit("r1", bad)


def test_resume_doc_rejects_empty_vector():
    with pytest.raises(ValueError, match="vector"):
        ResumeDoc("r1", "文本", ())


def test_resume_doc_rejects_empty_id():
    with pytest.raises(ValueError, match="resume_id"):
        ResumeDoc("", "文本", (0.1, 0.2))


# ============================================================
# Resume / ResumeStatus
# ============================================================


def test_resume_defaults_to_queued():
    """刚上传的简历：有文件、有哈希，但还没解析。"""
    resume = Resume(id="r1", filename="a.pdf", content_sha256="abc")
    assert resume.status is ResumeStatus.QUEUED
    assert resume.raw_text is None
    assert resume.metadata is None


def test_resume_rejects_extracted_without_metadata():
    """状态说"抽完了"，却没有抽取结果 —— 这种行在库里没法排查。"""
    with pytest.raises(ValueError, match="metadata"):
        Resume(id="r1", filename="a.pdf", content_sha256="abc",
               status=ResumeStatus.EXTRACTED)


def test_resume_rejects_indexed_without_metadata():
    with pytest.raises(ValueError, match="metadata"):
        Resume(id="r1", filename="a.pdf", content_sha256="abc",
               status=ResumeStatus.INDEXED)


def test_resume_accepts_extracted_with_metadata():
    resume = Resume(
        id="r1", filename="a.pdf", content_sha256="abc",
        metadata=ResumeMetadata(name="张三"),
        status=ResumeStatus.EXTRACTED,
    )
    assert resume.metadata.name == "张三"


def test_resume_rejects_failed_without_error():
    with pytest.raises(ValueError, match="error"):
        Resume(id="r1", filename="a.pdf", content_sha256="abc",
               status=ResumeStatus.FAILED)


def test_resume_requires_id_and_hash():
    with pytest.raises(ValueError, match="id"):
        Resume(id="", filename="a.pdf", content_sha256="abc")
    with pytest.raises(ValueError, match="content_sha256"):
        Resume(id="r1", filename="a.pdf", content_sha256="")


def test_resume_status_is_plain_string():
    """继承 str 是为了能直接落 TEXT 列、也能被 json.dumps。"""
    assert ResumeStatus.INDEXED == "indexed"


# ============================================================
# Protocol：结构化子类型
# ============================================================


class _FakeStore:
    """完全没有继承 VectorStorePort，只是方法齐了。"""

    def upsert(self, docs):
        pass

    def search(self, vector, *, top_k, min_similarity):
        return []

    def delete(self, resume_ids):
        pass

    def drop_and_rebuild(self):
        pass


class _IncompleteStore:
    def upsert(self, docs):
        pass


def test_structurally_complete_class_satisfies_protocol():
    assert isinstance(_FakeStore(), VectorStorePort)


def test_missing_method_does_not_satisfy_protocol():
    assert not isinstance(_IncompleteStore(), VectorStorePort)


def test_fake_store_is_usable_as_the_port():
    """端口的价值：调用方只按协议用，不关心背后是什么实现。"""

    def count_hits(store: VectorStorePort, vector) -> int:
        return len(store.search(vector, top_k=10, min_similarity=0.3))

    assert count_hits(_FakeStore(), [0.1, 0.2]) == 0


# ============================================================
# 仓储协议：同样只要求方法齐备
# ============================================================


class _FakeResumeRepo:
    def add(self, resume): return resume.id
    def get(self, resume_id): return None
    def find_by_content_hash(self, sha256): return None
    def list(self, *, limit, offset, status=None): return []
    def update(self, resume): pass
    def delete(self, resume_id): pass
    def corpus_version(self): return 0
    def bump_corpus_version(self): return 1


class _FakeRunRepo:
    def create(self, run): return run.id
    def get(self, run_id): return None
    def update_stage(self, run_id, stage, progress): pass
    def finish(self, run_id, status, counts, *, error=None): pass
    def find_by_idempotency_key(self, key): return None
    def list(self, *, limit, offset): return []
    def save_results(self, run_id, rows): pass
    def list_results(self, run_id): return []


def test_fake_resume_repo_satisfies_port():
    assert isinstance(_FakeResumeRepo(), ResumeRepository)


def test_fake_run_repo_satisfies_port():
    assert isinstance(_FakeRunRepo(), RunRepository)


def test_repos_are_not_interchangeable():
    """两个仓储的职责不能混用 —— 简历仓储不认 run 的方法。"""
    assert not isinstance(_FakeResumeRepo(), RunRepository)
    assert not isinstance(_FakeRunRepo(), ResumeRepository)


# ============================================================
# LLM / Parser / Queue 协议
# ============================================================


class _FakeLLM:
    def extract(self, prompt, schema):
        return schema()

    def generate(self, prompt):
        return "报告"


class _FakeParser:
    def supports(self, filename, head):
        return head.startswith(b"%PDF-")

    def parse(self, path):
        return ParsedDocument(text="张三", page_count=2)


class _FakeQueue:
    def enqueue(self, task): pass
    def dequeue(self, timeout): return None
    def ack(self, task_id): pass
    def nack(self, task_id, retry, *, error=None): pass


def test_fake_llm_satisfies_port():
    assert isinstance(_FakeLLM(), LLMPort)


def test_fake_parser_satisfies_port():
    assert isinstance(_FakeParser(), DocumentParserPort)


def test_fake_queue_satisfies_port():
    assert isinstance(_FakeQueue(), TaskQueuePort)


class _FakeEmbedding:
    def embed(self, texts):
        return [(0.1, 0.2)] * len(texts)


def test_fake_embedding_satisfies_port():
    assert isinstance(_FakeEmbedding(), EmbeddingPort)


def test_embedding_output_length_matches_input():
    """长度对齐是调用方唯一能依赖的约定，这里把它钉住。"""
    vectors = _FakeEmbedding().embed(["a", "b", "c"])
    assert len(vectors) == 3


def test_llm_without_generate_is_not_llm_port():
    """少一个方法就不算实现 —— 这正是契约测试要防的那类漂移。"""
    class _OnlyExtract:
        def extract(self, prompt, schema):
            return schema()

    assert not isinstance(_OnlyExtract(), LLMPort)


# ============================================================
# Task / ParsedDocument
# ============================================================

def test_task_params_roundtrip():
    task = Task(id="t1", kind=TaskKind.PARSE_RESUME,
                payload=(("resume_id", "r1"),))
    assert task.params == {"resume_id": "r1"}


def test_task_params_returns_a_copy():
    """改 params 的结果不能影响任务本体。"""
    task = Task(id="t1", kind=TaskKind.PARSE_RESUME,
                payload=(("resume_id", "r1"),))
    task.params["resume_id"] = "改掉"
    assert task.params["resume_id"] == "r1"


def test_task_rejects_empty_id_and_negative_attempts():
    with pytest.raises(ValueError, match="id"):
        Task(id="", kind=TaskKind.SCREEN_RUN)
    with pytest.raises(ValueError, match="attempts"):
        Task(id="t1", kind=TaskKind.SCREEN_RUN, attempts=-1)


def test_task_kind_is_plain_string():
    assert TaskKind.SCREEN_RUN == "screen_run"


def test_parsed_document_detects_empty_text():
    """扫描版 PDF：解析成功但没有文字层，这是合法结果而不是异常。"""
    assert ParsedDocument(text="   \n ").is_empty
    assert not ParsedDocument(text="张三").is_empty


def test_parsed_document_rejects_negative_pages():
    with pytest.raises(ValueError, match="page_count"):
        ParsedDocument(text="x", page_count=-1)
