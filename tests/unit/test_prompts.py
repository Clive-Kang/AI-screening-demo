"""prompt 模板加载器的单测。

不测 prompt 的"质量"（那是人工验收），只测：
版本能定位、占位符能替换、漏传变量会当场报错。
"""

import pytest

from app.prompts import DEFAULT_VERSION, Prompt, load

EXPECTED = {
    "resume_extract": ("raw_text",),
    "job_requirement": ("query_text",),
    "candidate_report": ("requirement_json", "candidate_json", "score_json"),
}


@pytest.mark.parametrize("name, keys", EXPECTED.items())
def test_all_templates_loadable(name, keys):
    prompt = load(name)

    assert isinstance(prompt, Prompt)
    assert prompt.version == DEFAULT_VERSION
    assert prompt.text.strip()
    assert all(f"${{{k}}}" in prompt.text for k in keys)


@pytest.mark.parametrize("name, keys", EXPECTED.items())
def test_render_replaces_all_placeholders(name, keys):
    rendered = load(name).render(**{k: f"<{k}>" for k in keys})

    assert all(f"${{{k}}}" not in rendered for k in keys)
    assert all(f"<{k}>" in rendered for k in keys)


def test_missing_variable_raises():
    """漏传变量必须炸 —— 否则模型会收到字面量 "${raw_text}"。"""
    with pytest.raises(KeyError):
        load("resume_extract").render()


def test_unknown_template_lists_available():
    with pytest.raises(FileNotFoundError, match="resume_extract"):
        load("not_exist")


def test_dollar_in_user_text_is_not_re_processed():
    """简历原文里的 $ 不能被当成占位符（这就是不用 str.format 的原因）。"""
    out = load("resume_extract").render(raw_text="期望月薪 $15k，{花括号}也保留，50% 也一样")
    assert "$15k" in out
    assert "{花括号}" in out
    assert "50%" in out


def test_extract_prompt_anchors_current_job_rule():
    """v1 的关键约束不能被人改丢：至今 → is_current，不许换算成月份。"""
    text = load("resume_extract").text
    assert "is_current" in text
    assert "不要推测结束月份" in text
