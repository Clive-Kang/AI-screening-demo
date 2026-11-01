"""简历接口的测试。

用 TestClient + 进程内 SQLite，不启 uvicorn、不碰真实文件系统之外的路径
（blob 目录用 pytest 的 tmp_path）。上传只登记，所以这里**不会**调 LLM。
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_context
from app.main import create_app

PDF = b"%PDF-1.4\n% test pdf\n"


def _upload(client, data=PDF, name="张三.pdf"):
    return client.post(
        "/api/v1/resumes",
        files={"file": (name, data, "application/pdf")},
    )


# ============================================================
# 上传
# ============================================================

def test_upload_returns_202_and_resume_id(client):
    resp = _upload(client)

    assert resp.status_code == 202
    body = resp.json()
    assert body["resume_id"]
    assert body["deduplicated"] is False


def test_upload_persists_queued_row_and_enqueues_task(client, ctx):
    resume_id = _upload(client).json()["resume_id"]

    resume = ctx.resumes.get(resume_id)
    assert resume.status.value == "queued"
    assert resume.blob_path and Path(resume.blob_path).is_file()

    rows = ctx.db.query("SELECT kind, payload_json FROM tasks")
    assert len(rows) == 1
    assert rows[0]["kind"] == "parse_resume"
    assert resume_id in rows[0]["payload_json"]


def test_duplicate_upload_returns_same_id_without_new_task(client, ctx):
    first = _upload(client).json()
    second = _upload(client).json()

    assert second["deduplicated"] is True
    assert second["resume_id"] == first["resume_id"]

    assert len(ctx.db.query("SELECT 1 FROM tasks")) == 1
    assert len(ctx.resumes.list(limit=10, offset=0)) == 1


def test_non_pdf_is_rejected(client):
    resp = _upload(client, data=b"PK\x03\x04 not a pdf", name="简历.docx")
    assert resp.status_code == 415


def test_empty_file_is_rejected(client):
    assert _upload(client, data=b"").status_code == 400


def test_oversized_file_is_rejected(make_context):
    context = make_context(max_upload_mb=1)
    app = create_app()
    app.dependency_overrides[get_context] = lambda: context

    with TestClient(app) as c:
        resp = _upload(c, data=PDF + b"x" * (1024 * 1024))
    assert resp.status_code == 413


# ============================================================
# 列表与详情
# ============================================================

def test_list_returns_uploaded_resumes(client):
    resume_id = _upload(client).json()["resume_id"]

    resp = client.get("/api/v1/resumes")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["id"] for i in body["items"]] == [resume_id]
    assert body["limit"] == 20


def test_list_filters_by_status(client):
    _upload(client)

    assert len(client.get("/api/v1/resumes?status=queued").json()["items"]) == 1
    assert client.get("/api/v1/resumes?status=indexed").json()["items"] == []


def test_list_rejects_unknown_status(client):
    assert client.get("/api/v1/resumes?status=bogus").status_code == 422


def test_detail_hides_raw_text_by_default(client):
    resume_id = _upload(client).json()["resume_id"]

    body = client.get(f"/api/v1/resumes/{resume_id}").json()
    assert body["id"] == resume_id
    assert body["raw_text"] is None      # 刚上传还没有原文
    assert body["metadata"] is None


def test_detail_404_for_unknown_id(client):
    assert client.get("/api/v1/resumes/不存在").status_code == 404


# ============================================================
# 运维接口
# ============================================================

def test_health(client):
    body = client.get("/api/v1/health").json()
    assert body == {"status": "ok", "db": True}


def test_clear_resumes_removes_rows_blobs_vectors_and_tasks(client, ctx):
    """清空必须四处一起清：真源、原始文件、向量、队列任务。"""
    _upload(client)
    _upload(client, PDF + b"2", "李四.pdf")
    assert len(client.get("/api/v1/resumes").json()["items"]) == 2
    assert ctx.db.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 2
    assert list(ctx.settings.blob_dir.glob("*")), "上传后应当有原始文件"

    resp = client.delete("/api/v1/resumes")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}
    assert client.get("/api/v1/resumes").json()["items"] == []
    assert ctx.db.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0
    assert list(ctx.settings.blob_dir.glob("*")) == []


def test_clear_resumes_on_empty_library_is_noop(client):
    resp = client.delete("/api/v1/resumes")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 0}


def test_root_serves_demo_page(client):
    """根路径是单文件演示页，不依赖任何静态资源服务器。"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "智能简历筛选" in resp.text


def test_profiles_lists_configured_names(client):
    body = client.get("/api/v1/profiles").json()

    names = {p["name"] for p in body["profiles"]}
    assert body["default"] in names
    assert "tech" in names
    # 权重一起返回，HR 调参前不用去翻 YAML
    assert "skill" in body["profiles"][0]["dims"]
