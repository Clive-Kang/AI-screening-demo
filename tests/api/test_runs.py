"""筛选接口的测试：提交（202 + 队列）、读状态、读结果、删除简历。

结果行直接写进仓储，不跑 worker —— 测的是"读接口的形状与分页",
不是流水线本身（那条链路由 test_screening / test_worker 覆盖）。
"""

from app.domain.models import DimScore
from app.ports.repository import ResultRow

PDF = b"%PDF-1.4\n% test pdf\n"


def _upload(client, name="张三.pdf", data=PDF) -> str:
    return client.post(
        "/api/v1/resumes",
        files={"file": (name, data, "application/pdf")},
    ).json()["resume_id"]


def _two_resumes(client) -> tuple[str, str]:
    """两份内容不同的 PDF —— 相同内容会被去重成同一个 id。"""
    return _upload(client), _upload(client, "李四.pdf", PDF + b"2")


def _submit(client, query="招 Python 后端，本科"):
    return client.post("/api/v1/runs", json={"query_text": query})


def _dims():
    return (
        DimScore(name="skill", score=0.9, weight=0.4, reason="命中 3/3"),
        DimScore(name="education", score=None, weight=0.2, reason="无数据"),
    )


# ============================================================
# 提交
# ============================================================

def test_submit_returns_202_and_queues_task(client, ctx):
    resp = _submit(client)

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["deduplicated"] is False

    run = ctx.runs.get(body["run_id"])
    assert run.status.value == "queued"
    assert run.profile == "tech"          # 默认档案

    rows = ctx.db.query("SELECT kind, payload_json FROM tasks")
    assert [r["kind"] for r in rows] == ["screen_run"]
    assert body["run_id"] in rows[0]["payload_json"]


def test_repeat_submit_hits_idempotency(client, ctx):
    first = _submit(client).json()
    second = _submit(client).json()

    assert second["deduplicated"] is True
    assert second["run_id"] == first["run_id"]
    # 没有产生第二个任务
    assert len(ctx.db.query("SELECT 1 FROM tasks")) == 1


def test_submit_rejects_blank_query(client):
    assert _submit(client, query="   ").status_code == 400


def test_submit_rejects_unknown_profile(client):
    resp = client.post("/api/v1/runs", json={"query_text": "招人", "profile": "不存在"})
    assert resp.status_code == 400
    assert "未知档案" in resp.json()["detail"]


def test_submit_accepts_explicit_profile(client, ctx):
    body = client.post(
        "/api/v1/runs", json={"query_text": "招人", "profile": "strict"}
    ).json()
    assert ctx.runs.get(body["run_id"]).profile == "strict"


# ============================================================
# 状态
# ============================================================

def test_get_run_returns_progress(client):
    run_id = _submit(client).json()["run_id"]

    body = client.get(f"/api/v1/runs/{run_id}").json()
    assert body["run_id"] == run_id
    assert body["status"] == "queued"
    assert body["query_text"] == "招 Python 后端，本科"
    assert body["progress"] == {}


def test_get_run_404(client):
    assert client.get("/api/v1/runs/不存在").status_code == 404


# ============================================================
# 结果
# ============================================================

def _seed_results(ctx, run_id, resume_ids):
    ctx.runs.save_results(
        run_id,
        [
            ResultRow(resume_id=resume_ids[0], rank=1, overall=0.86, semantic=0.7,
                      dims=_dims(), analysis_text="推荐"),
            ResultRow(resume_id=resume_ids[1], rank=2, passed=False,
                      reject_reason="学历不满足：要求本科，简历是大专"),
        ],
    )


def test_results_empty_before_worker_runs(client):
    run_id = _submit(client).json()["run_id"]

    body = client.get(f"/api/v1/runs/{run_id}/results").json()
    assert body["total"] == 0
    assert body["items"] == []


def test_results_exclude_rejected_by_default(client, ctx):
    r1, r2 = _two_resumes(client)
    run_id = _submit(client).json()["run_id"]
    _seed_results(ctx, run_id, [r1, r2])

    body = client.get(f"/api/v1/runs/{run_id}/results").json()
    assert body["total"] == 1
    assert [i["resume_id"] for i in body["items"]] == [r1]
    assert body["items"][0]["overall_score"] == 0.86
    assert body["items"][0]["dims"][0]["reason"] == "命中 3/3"
    # 缺失维度以 null 暴露，不能悄悄变成 0
    assert body["items"][0]["dims"][1]["score"] is None


def test_results_include_rejected_shows_reason(client, ctx):
    r1, r2 = _two_resumes(client)
    run_id = _submit(client).json()["run_id"]
    _seed_results(ctx, run_id, [r1, r2])

    body = client.get(
        f"/api/v1/runs/{run_id}/results?include_rejected=true"
    ).json()
    assert body["total"] == 2
    rejected = [i for i in body["items"] if not i["passed"]][0]
    assert "学历" in rejected["reject_reason"]


def test_results_pagination(client, ctx):
    r1, r2 = _two_resumes(client)
    run_id = _submit(client).json()["run_id"]
    _seed_results(ctx, run_id, [r1, r2])

    body = client.get(
        f"/api/v1/runs/{run_id}/results?include_rejected=true&limit=1&offset=1"
    ).json()
    assert body["total"] == 2
    assert [i["resume_id"] for i in body["items"]] == [r2]


def test_results_404_for_unknown_run(client):
    assert client.get("/api/v1/runs/不存在/results").status_code == 404


# ============================================================
# 删除简历
# ============================================================

def test_delete_resume_removes_row_and_bumps_corpus(client, ctx):
    resume_id = _upload(client)
    before = ctx.resumes.corpus_version()

    assert client.delete(f"/api/v1/resumes/{resume_id}").status_code == 204

    assert ctx.resumes.get(resume_id) is None
    assert client.get(f"/api/v1/resumes/{resume_id}").status_code == 404
    # 语料版本 +1 → 旧 run 的幂等键立即失效
    assert ctx.resumes.corpus_version() == before + 1


def test_delete_unknown_resume_404(client):
    assert client.delete("/api/v1/resumes/不存在").status_code == 404
