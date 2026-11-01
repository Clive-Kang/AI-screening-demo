"""生成三份最小可解析的测试简历 PDF（纯 ASCII，Helvetica）。

为什么不用 reportlab：项目没这个依赖，而验收只需要"能被 pypdf 读出文字"，
手写 PDF 结构足够。内容是英文的 —— PDF 标准字体不含中文字形，
写中文需要嵌入字体，与验收目标无关。

用法：
    D:/conda_envs/ai_resume_screening/python.exe scripts/make_samples.py
"""

import sys
from pathlib import Path

# 直接运行脚本时 sys.path[0] 是 scripts/，把项目根补进来才能 import app.*
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，中文会乱码

OUT_DIR = ROOT / "data" / "samples"

RESUMES: dict[str, list[str]] = {
    "zhangsan.pdf": [
        "Zhang San",
        "Backend Engineer | Beijing | zhangsan@example.com",
        "",
        "SUMMARY",
        "5 years of experience building Python backend services.",
        "",
        "SKILLS",
        "Python, FastAPI, Django, PostgreSQL, Redis, Docker, Kubernetes",
        "",
        "EXPERIENCE",
        "2021-03 - present  Senior Backend Engineer, ByteWave, Beijing",
        "  Built high-traffic APIs with FastAPI and PostgreSQL.",
        "2019-07 - 2021-02  Backend Engineer, CloudStep, Beijing",
        "  Developed microservices in Python and Redis.",
        "",
        "EDUCATION",
        "2015-09 - 2019-06  BSc Computer Science, Beijing University",
        "",
        "EXPECTED SALARY",
        "30000-40000 CNY per month",
        "PREFERRED LOCATION: Beijing",
    ],
    "lisi.pdf": [
        "Li Si",
        "Backend Engineer | Shanghai | lisi@example.com",
        "",
        "SUMMARY",
        "2 years of experience writing Go services.",
        "",
        "SKILLS",
        "Go, Gin, MySQL, Redis, Linux",
        "",
        "EXPERIENCE",
        "2024-01 - present  Backend Engineer, ShopNow, Shanghai",
        "  Built order services in Go.",
        "2023-05 - 2023-12  Junior Developer, SmallApp, Shanghai",
        "  Maintained internal tools.",
        "",
        "EDUCATION",
        "2020-09 - 2023-06  Associate Degree in Software Technology,",
        "Shanghai Vocational College",
        "",
        "EXPECTED SALARY",
        "18000-22000 CNY per month",
        "PREFERRED LOCATION: Shanghai",
    ],
    "wangwu.pdf": [
        "Wang Wu",
        "Machine Learning Engineer | Beijing | wangwu@example.com",
        "",
        "SUMMARY",
        "6 years of experience in Python and deep learning.",
        "",
        "SKILLS",
        "Python, PyTorch, TensorFlow, NLP, FastAPI, SQL",
        "",
        "EXPERIENCE",
        "2020-04 - present  ML Engineer, AI Labs, Beijing",
        "  Trained NLP models and served them with FastAPI.",
        "2018-07 - 2020-03  Data Scientist, DataWorks, Beijing",
        "  Built recommendation models in Python.",
        "",
        "EDUCATION",
        "2015-09 - 2018-06  MSc Computer Science, Tsinghua University",
        "2011-09 - 2015-06  BSc Software Engineering, Tsinghua University",
        "",
        "EXPECTED SALARY",
        "45000-60000 CNY per month",
        "PREFERRED LOCATION: Beijing",
    ],
}


def _escape(text: str) -> str:
    """PDF 字符串里的三个特殊字符必须转义。"""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(lines: list[str]) -> bytes:
    ops = ["BT", "/F1 11 Tf", "50 800 Td", "14 TL"]
    ops += [f"({_escape(line)}) Tj T*" for line in lines]
    ops.append("ET")
    stream = "\n".join(ops).encode("ascii")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"

    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, lines in RESUMES.items():
        path = OUT_DIR / name
        path.write_bytes(build_pdf(lines))
        print(f"写入 {path}")

    # 立刻用项目自己的解析器回读一遍，确保生成的文件真能被解析
    from app.adapters.parser.pdf_parser import PdfParser

    parser = PdfParser()
    for name in RESUMES:
        doc = parser.parse(str(OUT_DIR / name))
        if doc.is_empty:
            raise SystemExit(f"{name} 解析为空，生成失败")
        print(f"校验 {name}: {len(doc.text)} 字符, {doc.page_count} 页")


if __name__ == "__main__":
    main()
