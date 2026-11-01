"""嵌入适配器：OpenAI 兼容协议（本项目指向本地 Ollama 的 bge-m3）。

和 LLM 适配器分成两个文件，是因为它们的 provider、失败模式、
重试语义都不一样（见 ports/embedding.py 的说明）。写法却几乎一样 ——
这就是"同一个协议"的好处，但也带来一个必须留心的差异：

    嵌入请求传 **字符串列表**，不传 token 数组。
    参考项目用 langchain 的 OpenAIEmbeddings，它默认
    check_embedding_ctx_length=True，会在本地把文本切成 tiktoken token
    再发给服务端；Ollama 的 OpenAI 兼容层只接受字符串，于是报
    `400 invalid input type`（CLAUDE.md §4 第 4 条）。
    这里直接用官方 SDK 传 list[str]，天然不会踩。

维度校验
--------
BGE-M3 输出 1024 维，与 settings.embedding_dimensions 对上。写错模型名或
配错维度时，向量照样能生成，只是和库里已有的向量算不出有意义的相似度 ——
所以这里强制比对，不一致立刻抛 EmbeddingError。
"""

from collections.abc import Sequence

from openai import OpenAI, OpenAIError

from app.config.settings import Settings
from app.ports.embedding import EmbeddingError


class OpenAICompatibleEmbedding:
    """实现 ports.embedding.EmbeddingPort。"""

    def __init__(self, settings: Settings, *, client=None) -> None:
        self._settings = settings
        self._dimensions = settings.embedding_dimensions
        self._client = client or OpenAI(
            api_key=settings.embedding_api_key.get_secret_value(),
            base_url=settings.embedding_base_url,
            timeout=settings.llm_timeout_s,
            # 嵌入失败几乎都是"服务没起"或"配错"，重试一次够；多了只是等超时
            max_retries=1,
        )

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        if not texts:
            # 空列表直接返回，不浪费一次网络往返
            return []

        try:
            resp = self._client.embeddings.create(
                model=self._settings.embedding_model,
                input=list(texts),  # 必须是 list[str]，不能是 token 数组
            )
        except OpenAIError as exc:
            raise EmbeddingError(f"嵌入调用失败：{exc}") from exc

        # 协议**不保证**返回顺序与输入一致，每条带 index 字段，必须按它排回来。
        # 参考项目直接把 data 当有序列表用，并发/批处理时就会串位。
        vectors = [item.embedding for item in sorted(resp.data, key=lambda d: d.index)]

        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"嵌入数量不符：请求 {len(texts)} 条，返回 {len(vectors)} 条"
            )
        for vec in vectors:
            if len(vec) != self._dimensions:
                raise EmbeddingError(
                    f"向量维度不符：期望 {self._dimensions}，实际 {len(vec)}。"
                    f"检查 EMBEDDING_MODEL 与 EMBEDDING_DIMENSIONS 是否一致"
                )

        # 转成 tuple：端口约定不可变，且能哈希、能放进 frozen dataclass
        return [tuple(vec) for vec in vectors]
