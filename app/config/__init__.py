"""运行配置（环境变量 / .env）。

注意与仓库根目录的 `config/` 区分：
    config/scoring.yaml   业务策略：权重、阈值（可热改、带版本号）
    app/config/settings.py 部署配置：密钥、地址、路径（属于环境，不属于业务）
两者混在一起是参考项目的问题之一 —— 调权重和泄漏 API key 被放在同一处。
"""
