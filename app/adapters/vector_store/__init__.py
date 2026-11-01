"""向量库实现。

sqlite_vec_adapter.py   v1 默认实现：sqlite-vec 扩展 + 与业务表同一个 .db 文件

为什么不用 Chroma / Milvus：
    见 ARCHITECTURE.md §7.3。核心理由是"真源唯一" ——
    向量是可重建的派生数据，放进第二个文件（更别说第二个服务）就意味着
    又要维护一份不一致的风险。sqlite-vec 让向量和业务数据同库同事务。
"""
