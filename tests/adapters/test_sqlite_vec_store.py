"""SqliteVecStore 的单测。

用 3 维向量而不是 1024 维：测试关心的是逻辑，维度只要够小就看得清。
每个用例一个 tmp_path 下的独立 .db 文件，互不干扰。
"""

import pytest

from app.adapters.vector_store.sqlite_vec_adapter import SqliteVecStore
from app.ports.vector_store import Hit, ResumeDoc, VectorStorePort

DIM = 3


@pytest.fixture
def store(tmp_path):
    s = SqliteVecStore(tmp_path / "vec.db", DIM)
    yield s
    s.close()


def _doc(resume_id, vector, text="") -> ResumeDoc:
    return ResumeDoc(resume_id=resume_id, text=text, vector=tuple(vector))


def _seed(store):
    # r1 与查询向量同向；r2 正交；r3 完全反向
    store.upsert([_doc("r1", [1, 0, 0]), _doc("r2", [0, 1, 0]), _doc("r3", [-1, 0, 0])])


# ============================================================
# 结构契约
# ============================================================

def test_satisfies_port(store):
    assert isinstance(store, VectorStorePort)


# ============================================================
# 写入与检索
# ============================================================

def test_empty_upsert_is_noop(store):
    store.upsert([])
    assert store.search([1, 0, 0], top_k=10, min_similarity=0.0) == []


def test_search_returns_hits_ordered_by_similarity(store):
    _seed(store)
    hits = store.search([1, 0, 0], top_k=10, min_similarity=0.0)

    assert all(isinstance(h, Hit) for h in hits)
    assert [h.resume_id for h in hits] == ["r1", "r2", "r3"]
    assert hits[0].similarity == pytest.approx(1.0, abs=1e-5)
    assert hits[1].similarity == pytest.approx(0.0, abs=1e-5)
    # 反向向量的余弦相似度是 -1，但端口约定只在 [0,1]，必须夹到 0
    assert hits[2].similarity == 0.0


def test_similarity_always_in_unit_interval(store):
    _seed(store)
    hits = store.search([1, 0, 0], top_k=10, min_similarity=0.0)
    assert all(0.0 <= h.similarity <= 1.0 for h in hits)


def test_upsert_same_id_overwrites(store):
    store.upsert([_doc("r1", [1, 0, 0])])
    store.upsert([_doc("r1", [0, 1, 0])])  # 同一份简历重新嵌入

    hits = store.search([1, 0, 0], top_k=10, min_similarity=0.0)
    assert len(hits) == 1
    assert hits[0].similarity == pytest.approx(0.0, abs=1e-5)


def test_top_k_limits_results(store):
    _seed(store)
    assert len(store.search([1, 0, 0], top_k=2, min_similarity=0.0)) == 2


def test_min_similarity_filters(store):
    _seed(store)
    hits = store.search([1, 0, 0], top_k=10, min_similarity=0.9)

    assert [h.resume_id for h in hits] == ["r1"]


def test_top_k_zero_returns_nothing(store):
    _seed(store)
    assert store.search([1, 0, 0], top_k=0, min_similarity=0.0) == []


# ============================================================
# 删除与重建
# ============================================================

def test_delete_removes_only_given_ids(store):
    _seed(store)
    store.delete(["r1"])

    ids = [h.resume_id for h in store.search([1, 0, 0], top_k=10, min_similarity=0.0)]
    assert ids == ["r2", "r3"]


def test_delete_unknown_id_is_noop(store):
    _seed(store)
    store.delete(["不存在"])
    assert len(store.search([1, 0, 0], top_k=10, min_similarity=0.0)) == 3


def test_drop_and_rebuild_empties_index(store):
    _seed(store)
    store.drop_and_rebuild()

    assert store.search([1, 0, 0], top_k=10, min_similarity=0.0) == []
    # 重建后仍可正常写入
    store.upsert([_doc("r9", [1, 0, 0])])
    assert store.search([1, 0, 0], top_k=10, min_similarity=0.0)[0].resume_id == "r9"


# ============================================================
# 校验与持久化
# ============================================================

def test_dimension_mismatch_raises(store):
    with pytest.raises(ValueError, match="维度不符"):
        store.upsert([_doc("r1", [1, 0])])
    with pytest.raises(ValueError, match="维度不符"):
        store.search([1, 0], top_k=1, min_similarity=0.0)


def test_data_survives_reconnect(tmp_path):
    """索引落在文件里，进程重启后仍在 —— reindex 才有意义。"""
    path = tmp_path / "vec.db"
    first = SqliteVecStore(path, DIM)
    first.upsert([_doc("r1", [1, 0, 0])])
    first.close()

    second = SqliteVecStore(path, DIM)
    try:
        hits = second.search([1, 0, 0], top_k=1, min_similarity=0.0)
        assert hits[0].resume_id == "r1"
    finally:
        second.close()
