"""app/config/settings.py 的单测。

所有用例都传 _env_file=None，避免读到开发者本机的真实 .env —
测试结果不能取决于跑测试的机器上配置了什么。
"""

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ============================================================
# 必填与默认值
# ============================================================

def test_llm_api_key_is_required():
    """缺 key 必须在构造时失败 —— 而且要早于"第一个请求发出时"。"""
    with pytest.raises(ValidationError, match="llm_api_key"):
        _settings()


def test_defaults():
    s = _settings(llm_api_key="sk-test")
    assert s.llm_base_url == "https://api.deepseek.com"
    assert s.embedding_base_url == "http://localhost:11434/v1"
    assert s.embedding_dimensions == 1024
    assert s.analyze_top_n == 10
    assert s.db_path.name == "app.db"


def test_reads_environment_variables(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    s = _settings()
    assert s.llm_api_key.get_secret_value() == "sk-from-env"
    assert s.embedding_model == "text-embedding-3-small"


# ============================================================
# 密钥保护
# ============================================================

def test_secret_is_masked():
    """SecretStr 的意义：日志 / 报错里不会把 key 打出来。"""
    s = _settings(llm_api_key="sk-very-secret")
    assert "sk-very-secret" not in str(s)
    assert "sk-very-secret" not in repr(s.llm_api_key)
    assert s.llm_api_key.get_secret_value() == "sk-very-secret"


# ============================================================
# 校验
# ============================================================

@pytest.mark.parametrize("bad", [0, -1])
def test_dimensions_must_be_positive(bad):
    with pytest.raises(ValidationError, match="embedding_dimensions"):
        _settings(llm_api_key="sk", embedding_dimensions=bad)


@pytest.mark.parametrize("bad", [0, 101])
def test_upload_size_is_bounded(bad):
    with pytest.raises(ValidationError, match="max_upload_mb"):
        _settings(llm_api_key="sk", max_upload_mb=bad)


# ============================================================
# 派生属性
# ============================================================

def test_max_upload_bytes():
    assert _settings(llm_api_key="sk", max_upload_mb=2).max_upload_bytes == 2 * 1024 * 1024


def test_origins_split_and_strip():
    s = _settings(llm_api_key="sk", allowed_origins="http://a.com, http://b.com ,")
    assert s.origins == ["http://a.com", "http://b.com"]
