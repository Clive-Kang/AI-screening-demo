"""端到端验收脚本：只调 HTTP，不碰数据库。

前提（两个终端）：
    终端 1  D:/conda_envs/ai_resume_screening/python.exe -m uvicorn app.main:app --port 8000
    终端 2  D:/conda_envs/ai_resume_screening/python.exe -m app.worker

然后：
    D:/conda_envs/ai_resume_screening/python.exe scripts/smoke_test.py

脚本会真实调用 DeepSeek 与 Ollama（3 份简历 ≈ 7 次 LLM 调用）。
"""

import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
# 默认 8000；被占用时用 API_BASE=http://127.0.0.1:8010/api/v1 覆盖
BASE = os.getenv("API_BASE", "http://127.0.0.1:8000/api/v1")
SAMPLES = sorted((ROOT / "data" / "samples").glob("*.pdf"))
# 故意不写地点：样本简历是英文的（PDF 标准字体无中文），
# "Beijing" 与岗位里的"北京"做字符串交集会 0 命中，属于样本数据的局限。
# 地点过滤已由 tests/ 的单测覆盖，这里要验收的是"评分 + Top-N 报告"整条链路。
QUERY = "招 Python 后端工程师，本科及以上，3 年以上工作经验"

sys.stdout.reconfigure(encoding="utf-8")

client = httpx.Client(base_url=BASE, timeout=60.0)


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def wait_until(desc: str, fn, timeout_s: float = 300.0):
    """轮询直到 fn() 为真；每 2 秒一次，避免把服务打满。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(2)
    raise SystemExit(f"超时：等待 {desc} 超过 {timeout_s:.0f} 秒")


def main() -> None:
    step("0. 健康检查")
    health = client.get("/health").json()
    print(health)
    if health.get("status") != "ok":
        raise SystemExit("服务未就绪，先启动 uvicorn")

    step("1. 上传简历")
    resume_ids = []
    for pdf in SAMPLES:
        resp = client.post(
            "/resumes",
            files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
        )
        if resp.status_code != 202:
            raise SystemExit(f"{pdf.name} 上传失败：{resp.status_code} {resp.text}")
        data = resp.json()
        resume_ids.append(data["resume_id"])
        print(f"{pdf.name} -> {data['resume_id']}（deduplicated={data['deduplicated']}）")

    step("2. 等待 worker 入库（解析 + 抽取 + 嵌入）")
    def all_indexed():
        rows = client.get("/resumes", params={"limit": 100}).json()["items"]
        chosen = [r for r in rows if r["id"] in resume_ids]
        print("  状态:", ", ".join(f"{r['filename']}={r['status']}" for r in chosen))
        if any(r["status"] == "failed" for r in chosen):
            raise SystemExit("有简历入库失败，看 worker 终端的日志")
        return len(chosen) == len(resume_ids) and all(
            r["status"] == "indexed" for r in chosen
        )

    wait_until("全部 indexed", all_indexed)
    print("三份简历均已入库")

    step("3. 提交筛选任务")
    resp = client.post("/runs", json={"query_text": QUERY})
    if resp.status_code != 202:
        raise SystemExit(f"提交失败：{resp.status_code} {resp.text}")
    submitted = resp.json()
    run_id = submitted["run_id"]
    print(f"run_id={run_id} status={submitted['status']} "
          f"deduplicated={submitted['deduplicated']}")

    step("4. 等待筛选完成")
    def finished():
        run = client.get(f"/runs/{run_id}").json()
        print(f"  status={run['status']} stage={run.get('stage')} "
              f"progress={run.get('progress')}")
        if run["status"] in ("failed",):
            raise SystemExit("筛选失败，看 worker 终端的日志")
        return run if run["status"] in ("succeeded", "partial") else None

    run = wait_until("run 完成", finished)

    step("5. 读取结果")
    results = client.get(
        f"/runs/{run_id}/results",
        params={"include_rejected": True, "limit": 100},
    ).json()
    print(f"候选总数 {results['total']}（含被硬性过滤淘汰的）\n")
    for item in results["items"]:
        flag = "通过" if item["passed"] else f"淘汰：{item['reject_reason']}"
        print(f"#{item['rank'] or '-'} {item['name'] or item['resume_id']}  "
              f"overall={item['overall_score']}  semantic={item['semantic_score']}  [{flag}]")
        for d in item["dims"]:
            # score=None 表示这一维不适用（岗位没提），已从分母剔除
            score = "   -  " if d["score"] is None else f"{d['score']:.3f}"
            print(f"    {d['name']:<16} {score} × {d['weight']:.2f}  {d['reason']}")
        if item["analysis"]:
            print(f"    报告: {item['analysis'][:80]}...")

    print("\n验收完成。")


if __name__ == "__main__":
    main()
