"""LLM 端口：两种调用形态，业务层永远不碰 JSON 字符串。

参考项目只有 generate_text()，拿回裸字符串后手工 json.loads，
失败再用"找第一个 { 和最后一个 }"兜底——这段容错代码在两个文件里各写了一遍
（ARCHITECTURE.md §8.1）。这里把两种用途分开：

    extract()    要结构化结果 → 返回 Pydantic 对象，解析失败由 adapter 负责重试
    generate()   要一段自然语言 → 返回字符串（筛选报告用这个）

日期锚点统一注入
----------------
§4 第 5 条 bug 的根因是：三个 prompt 模板都不含"今天是几号"，
模型只能用训练数据里的"现在"去推断，于是把上个月结束的工作经历判成"未来"。

修法不是往每个模板里各写一遍日期（改一个漏两个），而是让**实现方**
在 system message 里统一注入。这个约束写在端口上，所有 LLM 实现都必须遵守。
"""

from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    #  静态检查：值为`True`，会导入`BaseModel`，做完整类型校验。
    # 程序运行时：值永远是`False`，这段导入代码直接跳过。
    # 这样连"没装 pydantic 也能 import 端口"这个性质都保住了。
    from pydantic import BaseModel

T = TypeVar("T", bound="BaseModel")


class LLMError(Exception):
    """LLM 调用失败，或重试后仍拿不到合法的结构化结果。

    和 DocumentParseError 一样放在端口层：services 要能 `except LLMError`，
    但它不该认识 openai / httpx 的异常类型。
    """
#限定`T`必须是`BaseModel`的子类，保证`extract`输出一定是合法 Pydantic 模型。


@runtime_checkable
class LLMPort(Protocol):  #Protocol 是鸭子接口：实现类不需要显式继承，
    #只要方法签名一致就算合规，避免 domain 层反向依赖适配器。
    """LLM 的能力清单。"""

    def extract(self, prompt: str, schema: type[T]) -> T:
        """让模型按 schema 输出结构化数据，返回**已经校验过**的对象。

        prompt 里不用（也不该）自己写 JSON 示例、字段说明 ——
        schema 本身就是契约，adapter 负责把它翻译成 provider 的要求
        （DeepSeek 的 JSON mode / tool calling）。

        实现方必须：
          1. 在 system message 注入当前日期（YYYY-MM-DD）
          2. 校验失败时重试，重试仍失败则抛异常，绝不返回半成品对象
        """
        ...

    def generate(self, prompt: str) -> str:
        """要一段自然语言（筛选报告的正文）。

        与 extract 的区别：这里没有 schema，输出质量靠 prompt 约束。
        """
        ...
