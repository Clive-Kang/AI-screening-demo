"""PDF 解析器：pypdf 实现 DocumentParserPort。

选 pypdf 而不是 PyMuPDF（fitz）：
    pypdf 是纯 Python，conda 环境里已经有；
    PyMuPDF 提取速度更快、排版还原更好，但要装 C 扩展。
    简历都是文本型 PDF，pypdf 够用；将来发现扫描件识别率不够，
    换实现的成本也就是重写这个 40 行的文件。

关于扫描件
    pypdf 只能抽文字层。扫描版 PDF 抽出来是空字符串 ——
    这不是异常，调用方按 ParsedDocument.is_empty 判断，
    给用户一个"换文件"的提示（做 OCR 是另一个数量级的工程）。
"""

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.ports.document_parser import DocumentParseError, ParsedDocument

# PDF 文件头的 magic bytes。所有 PDF 都以这四个字节开头，
# 与文件后缀无关 —— 把 .docx 改名成 .pdf 骗不过这一关。
PDF_MAGIC = b"%PDF-"


class PdfParser:
    """实现 ports.document_parser.DocumentParserPort。"""

    def supports(self, filename: str, head: bytes) -> bool:
        # 只看内容，不看 filename —— 后缀是可以随便改的。
        # filename 留在签名里是给将来的 .docx 用的（ZIP 容器格式
        # 的 magic bytes 也是 PK\x03\x04，光靠 head 区分不了 docx/xlsx）。
        return head.startswith(PDF_MAGIC)

    def parse(self, path: str) -> ParsedDocument:
        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                # 很多简历设了"禁止编辑"的打开密码但密码为空，
                # 用空串试一次就能解开；解不开再报错。
                if not reader.decrypt(""):
                    raise DocumentParseError("PDF 已加密，无法读取")
            pages = [page.extract_text() or "" for page in reader.pages]
        except DocumentParseError:
            raise
        except (PdfReadError, OSError, ValueError) as exc:
            # 统一翻译成端口层的异常，调用方不需要认识 pypdf
            raise DocumentParseError(f"PDF 解析失败：{exc}") from exc

        return ParsedDocument(
            text="\n".join(pages).strip(),
            page_count=len(pages),
            source_suffix=Path(path).suffix.lower(),
        )
