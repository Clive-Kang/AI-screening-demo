"""OpenAICompatibleLLM 的单测。

全部用假 client：不发真实请求、不烧 API 额度、不需要 .env。
真实调用质量（抽取准不准）属于人工验收，不放进单测。
"""

from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.adapters.llm.openai_compat import OpenAICompatibleLLM, _strip_code_fence
from app.config.settings import Settings
from app.ports.llm import LLMError, LLMPort


class Person(BaseModel):
    name: str
    age: int


class FakeClient:
    """只模拟 `client.chat.completions.create` 这一条调用链。

    为什么不用 mock 库：这里要断言的正是"发了什么参数"，
    SimpleNamespace + 列表比 MagicMock 更直白，也不依赖实现细节的自动属性。
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        completions = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=completions)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies.pop(0)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, llm_api_key="sk-test", **overrides)


def _llm(*replies, max_retries=2, today=date(2026, 10, 18)):
    client = FakeClient(*replies)
    llm = OpenAICompatibleLLM(
        _settings(llm_max_retries=max_retries),
        client=client,
        today=lambda: today,
    )
    return llm, client


# ============================================================
# 结构契约
# ============================================================

def test_satisfies_port():
    """不需要继承，方法齐了就算 LLMPort（结构化子类型）。"""
    llm, _ = _llm("x")
    assert isinstance(llm, LLMPort)


# ============================================================
# 日期锚点
# ============================================================

def test_system_message_injects_today():
    llm, client = _llm('{"name": "张三", "age": 30}')
    llm.extract("抽取这个人的信息", Person)

    system = client.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "2026-10-18" in system["content"]


def test_date_anchor_is_not_in_user_prompt():
    """业务 prompt 里不该重复写日期 —— 那正是参考项目的翻车点。"""
    llm, client = _llm('{"name": "张三", "age": 30}')
    llm.extract("抽取这个人的信息", Person)

    user = client.calls[0]["messages"][1]
    assert user["role"] == "user"
    assert "2026-10-18" not in user["content"]


# ============================================================
# extract
# ============================================================

def test_extract_returns_validated_model():
    llm, _ = _llm('{"name": "张三", "age": 30}')
    person = llm.extract("抽取", Person)

    assert isinstance(person, Person)
    assert person.name == "张三"


def test_extract_passes_schema_and_json_mode():
    llm, client = _llm('{"name": "张三", "age": 30}')
    llm.extract("抽取", Person)

    call = client.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["temperature"] == 0
    # schema 被翻译成 JSON Schema 塞进 prompt
    assert '"name"' in call["messages"][1]["content"]


def test_extract_strips_code_fence():
    llm, _ = _llm('```json\n{"name": "张三", "age": 30}\n```')
    assert llm.extract("抽取", Person).age == 30


def test_extract_retries_on_invalid_output():
    """第一次缺字段 → 重试一次拿到合法结果。"""
    llm, client = _llm(
        '{"name": "张三"}',              # 缺 age
        '{"name": "张三", "age": 30}',
        max_retries=2,
    )
    assert llm.extract("抽取", Person).age == 30
    assert len(client.calls) == 2


def test_extract_gives_up_after_configured_attempts():
    llm, client = _llm("不是 JSON", "还不是 JSON", "仍然不是", max_retries=2)

    with pytest.raises(LLMError, match="Person"):
        llm.extract("抽取", Person)
    # 1 次首发 + 2 次重试
    assert len(client.calls) == 3


def test_extract_does_not_retry_validation_inside_single_chat():
    """确认重试是真发请求，而不是在本地反复解析同一份文本。"""
    llm, client = _llm("坏数据", '{"name": "李四", "age": 20}', max_retries=1)
    llm.extract("抽取", Person)
    assert client.calls[1]["messages"][1]["content"] == client.calls[0]["messages"][1]["content"]


# ============================================================
# generate
# ============================================================

def test_generate_returns_text_without_json_mode():
    llm, client = _llm("这位候选人匹配度较高。")
    assert llm.generate("写一段评价") == "这位候选人匹配度较高。"
    assert "response_format" not in client.calls[0]


def test_generate_returns_empty_string_on_empty_choices():
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(choices=[]))
        )
    )
    llm = OpenAICompatibleLLM(_settings(), client=client)
    assert llm.generate("x") == ""


# ============================================================
# 文本清理
# ============================================================

@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('以下是结果：\n{"a": 1}\n希望有帮助', '{"a": 1}'),
        ("没有 json", "没有 json"),
    ],
)
def test_strip_code_fence(raw, expected):
    assert _strip_code_fence(raw) == expected
