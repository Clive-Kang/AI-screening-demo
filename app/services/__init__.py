"""服务层：编排。

只依赖 domain 与 ports，不 import adapters / fastapi。
"谁调用它"（HTTP 路由 / worker）由组合根决定，服务自己不关心。

schemas.py    LLM 输出的 wire 格式（pydantic）→ 领域对象
ingestion.py  上传链路：解析 → 抽取 → 落库 → 建索引
screening.py  筛选链路：召回 → 过滤 → 评分 → 排序 → 分析
"""
