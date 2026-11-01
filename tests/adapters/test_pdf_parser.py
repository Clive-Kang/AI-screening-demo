"""PdfParser 的单测。

用 pypdf 自己生成 PDF（空白页），不依赖仓库外的示例文件。
真实简历的抽取质量属于人工验收，不放进单测。
"""

import pytest
from pypdf import PdfWriter

from app.adapters.parser.pdf_parser import PDF_MAGIC, PdfParser
from app.ports.document_parser import DocumentParseError


@pytest.fixture
def parser():
    return PdfParser()


def _make_pdf(path, pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as f:
        writer.write(f)
    return str(path)


# ============================================================
# supports：只看内容
# ============================================================

def test_supports_real_pdf_header(parser):
    assert parser.supports("简历.pdf", PDF_MAGIC + b"1.7 ...")


def test_supports_ignores_extension(parser):
    """改了后缀的 Word 文件不能被当成 PDF 收下。"""
    assert not parser.supports("简历.pdf", b"PK\x03\x04...")


def test_supports_rejects_image(parser):
    assert not parser.supports("简历.png", b"\x89PNG\r\n\x1a\n")


# ============================================================
# parse
# ============================================================

def test_parse_blank_pdf(parser, tmp_path):
    doc = parser.parse(_make_pdf(tmp_path / "blank.pdf", pages=2))

    assert doc.page_count == 2
    assert doc.source_suffix == ".pdf"
    # 空白页抽不出文字，这是合法结果而不是异常
    assert doc.is_empty


def test_parse_missing_file_raises_parse_error(parser):
    """文件不存在也翻译成端口层异常，调用方只认这一个。"""
    with pytest.raises(DocumentParseError):
        parser.parse("不存在的文件.pdf")


def test_parse_corrupt_file_raises_parse_error(parser, tmp_path):
    bad = tmp_path / "bad.pdf"
    # 字节字面量只能含 ASCII，所以这里用英文说明
    bad.write_bytes(PDF_MAGIC + b"\x00\x01\x02 not a valid pdf")
    with pytest.raises(DocumentParseError):
        parser.parse(str(bad))
