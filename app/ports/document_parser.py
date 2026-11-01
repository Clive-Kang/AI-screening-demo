"""文档解析端口：二进制文件 → 纯文本。

为什么不是简单的 `def parse(path) -> str`？
    一是要能拒绝不支持的文件（用户传了 .docx 或改了后缀的图片）；
    二是解析结果里除了正文，还有页数、是否扫描件这类信息，
    后面判断"LLM 抽取质量差是不是因为 PDF 本身就是图片"要用到。
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class DocumentParseError(Exception):
    """解析失败（文件损坏、加密、格式不支持）。

    为什么异常定义在 ports 而不是 adapter？
        services 要捕获它就得 import adapter —— 依赖方向反了。
        放在端口层，调用方 `except DocumentParseError` 即可，
        不需要知道背后是 pypdf 还是 pdfplumber。
    实现方负责把底层库的异常翻译成这一个。
    """



@dataclass(frozen=True)
class ParsedDocument:
    """一次解析的结果。

    text 为空是**合法**的：扫描版 PDF 抽不出文字层，属于常见情况。
    把它和一个异常分开，是因为处理方式不同——
    空文本会走到 status=failed + error="扫描件，无文字层"，用户可以换文件重传。
    """

    text: str
    page_count: int = 0
    source_suffix: str = "" # 原文件的后缀，用于排查（".pdf" / ".docx"）

    def __post_init__(self) -> None:
        if self.page_count < 0:
            raise ValueError(f"page_count 不能为负，实际是 {self.page_count}")

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@runtime_checkable
class DocumentParserPort(Protocol):
    """解析器插件。

    为什么拆成 supports + parse 两个方法？
        将来加 .docx / .txt 时，services 不需要知道有哪些解析器，
        只需要遍历注册表、问每一个 "你能处理这个文件吗"。
        新增格式 = 新增一个 adapter，不改调用方。
    """

    def supports(self, filename: str, head: bytes) -> bool:
        """能否处理这个文件。

        看后缀不够——改个名字就能骗过去。head 是文件前若干字节，
        用来做 magic bytes 校验（PDF 是 `%PDF-`）。
        """
        ...

    def parse(self, path: str) -> ParsedDocument:
        """解析磁盘上的文件。

        拿路径而不是 bytes：上传的文件已经落盘了（blob_path），
        再读进内存传一遍没有意义；而且大文件读两遍是纯浪费。
        """
        ...
