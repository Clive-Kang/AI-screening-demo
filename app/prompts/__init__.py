"""prompt 模板：外置成文件，文件名带版本号。

为什么外置（ARCHITECTURE.md §8.2）
    参考项目的 prompt 内联在 Python 字符串里，改一句提示词：
      - 无法追溯（git diff 里混在代码改动中）
      - 无法 A/B（新旧两版不能并存）
      - 无法知道某份简历是用哪版提示词抽出来的
    外置后，版本号随文件名走，抽取结果把版本写进
    `resume_metadata.extractor_prompt_version`，出问题能定位到具体版本。

为什么用 `${name}` 而不是 `{}` 或 `%s`
    简历原文里出现花括号和百分号的概率不低（模板语法、代码片段），
    `str.format` 会直接报 KeyError，`%` 同理。
    `string.Template` 的占位符是 `${name}`，而且**只解析模板本身**、
    不解析替换进去的值 —— 用户文本里有 `$` 也不会出问题。

版本升级约定
    内容有任何改动就复制成 `xxx.v2.txt`，不要原地改 v1：
    老数据里记着 "v1"，改了文件就再也复现不出当初的结果。
"""

from dataclasses import dataclass
from pathlib import Path
from string import Template

PROMPT_DIR = Path(__file__).resolve().parent
DEFAULT_VERSION = "v1"


@dataclass(frozen=True)
class Prompt:
    """一个已加载的模板。版本随对象走，调用方不用自己记。"""

    name: str
    version: str
    text: str

    def render(self, **values: str) -> str:
        # 用 substitute 而不是 safe_substitute：漏传变量要当场报错，
        # 而不是把 "${raw_text}" 原样发给模型（那会悄悄变成一次无效调用）。
        return Template(self.text).substitute(values)


def load(name: str, version: str = DEFAULT_VERSION) -> Prompt:
    """按名字和版本加载模板，例如 load("resume_extract") → resume_extract.v1.txt。"""
    path = PROMPT_DIR / f"{name}.{version}.txt"
    if not path.is_file():
        available = sorted(p.name for p in PROMPT_DIR.glob("*.txt"))
        raise FileNotFoundError(f"找不到模板 {path.name}，现有：{available}")
    return Prompt(name=name, version=version, text=path.read_text(encoding="utf-8"))


__all__ = ["Prompt", "load", "PROMPT_DIR", "DEFAULT_VERSION"]
