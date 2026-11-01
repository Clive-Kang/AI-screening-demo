"""FastAPI 应用入口（组合根之一，另一个是 worker.py）。

启动：

    D:/conda_envs/ai_resume_screening/python.exe -m uvicorn app.main:app --reload --port 8000

必须先有 `.env`（至少 LLM_API_KEY）—— 但**不是在 import 时检查**：
本模块 import 阶段不读配置、不开数据库，缺 key 时 `app.main:app` 依然能被
导入，真正报错发生在第一个请求。这样测试可以只 import 不跑服务。
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api import routes

API_PREFIX = "/api/v1"
WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def create_app() -> FastAPI:
    app = FastAPI(
        title="智能简历筛选系统",
        version="0.1.0",
        description="上传简历 → 向量召回 → 硬性过滤 → 六维评分 → Top-N 报告",
    )

    # 只读这一个独立环境变量：Settings 里的 LLM_API_KEY 是必填项，
    # 在 import 阶段构造 Settings 会让"缺 key"直接炸掉 import（参考项目的老毛病）。
    origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(routes.resumes_router, prefix=API_PREFIX)
    app.include_router(routes.runs_router, prefix=API_PREFIX)
    app.include_router(routes.system_router, prefix=API_PREFIX)

    # 演示页：单文件、零依赖，浏览器直接调 /api/v1。
    # 不挂 StaticFiles 是因为只有这一个文件，多一层 mount 没有收益。
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app


# uvicorn 找的就是这个变量。构造函数本身不读配置，所以 import 是安全的。
app = create_app()
