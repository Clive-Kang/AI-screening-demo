"""嵌入端口：文本 → 向量。

为什么不并进 LLMPort？
    两者虽然是同一个 OpenAI 兼容协议，但走的是**不同 provider**：
    本项目的对话用 DeepSeek（云端），嵌入用 Ollama 的 bge-m3（本地）。
    更实际的理由是它们的失败模式不同——嵌入失败几乎总是"服务没起"或
    "维度配错"，而对话失败常常是"输出格式不对、需要重试"。
    分开之后，各自的 adapter 可以有不同的重试策略。
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


class EmbeddingError(Exception):
    """嵌入调用失败，或返回的向量不符合约定（数量/维度不对）。

    维度不匹配必须**当场报错**：1024 维和 768 维的向量混进同一个索引，
    余弦相似度算出来是有值的——只是全错，而且不会抛异常。
    这类错误要在写入时就炸，而不是等排名结果离谱了再回头查。
    """


@runtime_checkable
class EmbeddingPort(Protocol):
    """批量把文本转成向量。"""

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """返回的列表长度必须与输入一致，第 i 个向量对应 texts[i]。

        为什么一次收一批而不是单条？
            建索引时要处理成千上万段文本，逐条 HTTP 往返是主要瓶颈。
            provider 基本都支持一次传多条，端口不该把这个能力丢掉。

        为什么写成方法而不是 `dimensions` 属性？
            Protocol 带数据成员时 `runtime_checkable` 会退化成只检查属性存在、
            不能用于 issubclass。全部写成方法，运行时检查的行为才可预期。
        """
        ...
