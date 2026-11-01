"""向量库适配器：sqlite-vec 扩展，与业务表同一个 SQLite 文件。

实现 ports.vector_store.VectorStorePort。

与参考项目的三个差别
--------------------
1. **只暴露 similarity，不暴露 distance**
   参考项目 Milvus 侧做了 `1.0 - distance`、Chroma 侧没做，两边语义不一致，
   下游还根本没人读这个字段（ARCHITECTURE.md §7.2）。这里在 adapter 边界
   就把 distance 换算掉，端口层看不到它。

2. **同库同文件**
   向量表建在业务库的同一个 .db 里。库文件被删，向量跟着没了；
   重建索引也只是重跑一次嵌入，不会出现"业务数据还在、向量库对不上"。

3. **vec0 不支持 INSERT OR REPLACE**
   实测会报 `UNIQUE constraint failed`，所以 upsert 写成
   「先 DELETE 再 INSERT」，两条语句放在同一个事务里。

本文件自行开连接（不复用 Database）
----------------------------------
    因为 vec0 虚拟表要求连接上先加载扩展。若把加载逻辑塞进 Database，
    仓储层就多了一个"必须装 sqlite-vec"的隐含前提；
    而向量本来就是可丢弃的派生数据，让它用独立连接更干净。
    WAL 是数据库文件级的设置（db.py 已开），两个连接读写不互斥；
    busy_timeout 是连接级的，所以这里要重新设一遍。
"""

import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path

import sqlite_vec

from app.ports.vector_store import Hit, ResumeDoc

_TABLE = "resume_vectors"


class SqliteVecStore:
    """实现 ports.vector_store.VectorStorePort。"""

    def __init__(self, db_path: str | Path, dimensions: int) -> None:
        if dimensions <= 0:
            raise ValueError(f"dimensions 必须为正，实际是 {dimensions}")
        self._dimensions = dimensions
        self._lock = threading.RLock()

        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,  # 查询在 worker 线程里跑
            isolation_level=None,     # 关掉隐式事务，手动 BEGIN（同 db.py）
        )
        # 扩展加载是有代码执行风险的开关，用完立刻关掉
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)

        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._ensure_table()

    # ------------------------------------------------------------
    # 端口方法
    # ------------------------------------------------------------

    def upsert(self, docs: Sequence[ResumeDoc]) -> None:
        if not docs:
            return
        for doc in docs:
            self._check_vector(doc.vector)

        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for doc in docs:
                    # vec0 不支持 INSERT OR REPLACE，只能先删后插
                    self._conn.execute(
                        f"DELETE FROM {_TABLE} WHERE resume_id = ?", (doc.resume_id,)
                    )
                    self._conn.execute(
                        f"INSERT INTO {_TABLE}(resume_id, embedding) VALUES (?, ?)",
                        (doc.resume_id, sqlite_vec.serialize_float32(doc.vector)),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def search(
        self, vector: Sequence[float], *, top_k: int, min_similarity: float
    ) -> list[Hit]:
        self._check_vector(vector)
        if top_k <= 0:
            return []

        # distance 阈值直接下推到 KNN 查询里（实测支持），
        # 不在 Python 里过滤 —— 否则 top_k 个名额会被低分结果占掉。
        # cosine 距离 = 1 - 余弦相似度，所以阈值要反过来算。
        #
        # 边界：余弦距离最大是 2.0（两个向量完全反向，相似度 -1）。
        # 若阈值取 1.0，这些条目会被 SQL 直接滤掉；但端口约定相似度夹在 [0,1]，
        # 它们应该是 similarity=0 而不是"不存在"。所以 min_similarity<=0 时放开全量。
        max_distance = 2.0 if min_similarity <= 0 else 1.0 - min_similarity

        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT resume_id, distance
                FROM {_TABLE}
                WHERE embedding MATCH ? AND k = ? AND distance <= ?
                ORDER BY distance
                """,
                (sqlite_vec.serialize_float32(vector), top_k, max_distance),
            ).fetchall()

        hits = []
        for resume_id, distance in rows:
            # distance 理论上落在 [0,2]，浮点误差可能让它略微越界，夹一下
            similarity = 1.0 - float(distance)
            similarity = max(0.0, min(1.0, similarity))
            hits.append(Hit(resume_id=resume_id, similarity=similarity))
        return hits

    def delete(self, resume_ids: Sequence[str]) -> None:
        if not resume_ids:
            return
        with self._lock:
            self._conn.executemany(
                f"DELETE FROM {_TABLE} WHERE resume_id = ?",
                [(rid,) for rid in resume_ids],
            )

    def drop_and_rebuild(self) -> None:
        """清空并重建索引（运维命令 reindex 用）。

        只删向量表，不碰业务表 —— 索引是可丢弃的派生数据。
        """
        with self._lock:
            self._conn.execute(f"DROP TABLE IF EXISTS {_TABLE}")
            self._ensure_table()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------

    def _ensure_table(self) -> None:
        # 维度必须是字面量（vec0 的 DDL 要求），所以用 f-string。
        # 这里插进来的只有构造时校验过的 int，不存在注入面。
        self._conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {_TABLE} USING vec0(
                resume_id TEXT PRIMARY KEY,
                embedding FLOAT[{self._dimensions}] DISTANCE_METRIC=cosine
            )
            """
        )

    def _check_vector(self, vector: Sequence[float]) -> None:
        if len(vector) != self._dimensions:
            # 维度不对是调用方的编程错误，不是运行时故障，用 ValueError
            raise ValueError(
                f"向量维度不符：期望 {self._dimensions}，实际 {len(vector)}"
            )
