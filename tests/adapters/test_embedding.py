"""OpenAICompatibleEmbedding 的单测。

假 client，不连 Ollama。真实调用（bge-m3 是否返回 1024 维）
由用户在本机跑一次确认，不放进单测。
"""

from types import SimpleNamespace

import pytest
from openai import OpenAIError

from app.adapters.embedding.openai_compat import OpenAICompatibleEmbedding
from app.config.settings import Settings
from app.ports.embedding import EmbeddingError, EmbeddingPort


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, llm_api_key="sk-test", **overrides)


class FakeClient:
    """模拟 client.embeddings.create。

    data 故意可以乱序，用来验证适配器按 index 重排。
    """

    def __init__(self, data=None, *, raise_error=False):
        self.data = data or []
        self.raise_error = raise_error
        self.calls: list[dict] = []
        self.embeddings = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_error:
            raise OpenAIError("连接被拒绝")
        return SimpleNamespace(data=self.data)


def _vec(index, dim=1024, fill=0.0):
    return SimpleNamespace(index=index, embedding=[fill] * dim)


def _embedding(data=None, *, dim=1024, raise_error=False):
    client = FakeClient(data, raise_error=raise_error)
    return OpenAICompatibleEmbedding(_settings(embedding_dimensions=dim), client=client), client


# ============================================================
# 结构契约
# ============================================================

def test_satisfies_port():
    emb, _ = _embedding()
    assert isinstance(emb, EmbeddingPort)


# ============================================================
# 正常路径
# ============================================================

def test_empty_input_makes_no_request():
    emb, client = _embedding()
    assert emb.embed([]) == []
    assert client.calls == []


def test_returns_tuples_of_configured_dimension():
    emb, _ = _embedding([_vec(0), _vec(1)])
    out = emb.embed(["简历一", "简历二"])

    assert len(out) == 2
    assert all(isinstance(v, tuple) for v in out)
    assert all(len(v) == 1024 for v in out)


def test_sends_strings_not_tokens():
    """必须是 list[str] —— 传 token 数组会被 Ollama 判 400。"""
    emb, client = _embedding([_vec(0)])
    emb.embed(["张三 三年 Python 经验"])

    sent = client.calls[0]["input"]
    assert sent == ["张三 三年 Python 经验"]
    assert all(isinstance(x, str) for x in sent)


def test_reorders_by_index():
    """服务端返回乱序时，必须按 index 归位，否则向量与文本错配。"""
    emb, _ = _embedding([_vec(1, fill=2.0), _vec(0, fill=1.0)])
    first, second = emb.embed(["第一段", "第二段"])

    assert first[0] == 1.0
    assert second[0] == 2.0


# ============================================================
# 失败路径：必须当场炸，不能静默写进索引
# ============================================================

def test_count_mismatch_raises():
    emb, _ = _embedding([_vec(0)])  # 请求 2 条，只回 1 条
    with pytest.raises(EmbeddingError, match="数量不符"):
        emb.embed(["a", "b"])


def test_dimension_mismatch_raises():
    emb, _ = _embedding([_vec(0, dim=768)], dim=1024)
    with pytest.raises(EmbeddingError, match="维度不符"):
        emb.embed(["a"])


def test_connection_error_is_translated():
    """服务没起时抛的是端口层异常，services 不需要认识 openai。"""
    emb, _ = _embedding(raise_error=True)
    with pytest.raises(EmbeddingError, match="嵌入调用失败"):
        emb.embed(["a"])
