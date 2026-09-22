"""Пункт 6 v0.4: sqlite-vec путь. Паритет с brute force пробами, не словами."""

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec", reason="pip install -e .[local-vec]")

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store, vec_extension_available
from unified_memory.summarize import ExtractiveSummarizer

TEXTS = [f"запись номер {i} про город и реку" for i in range(12)] + \
        ["совсем другое: квантовая физика и коты"]


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", context_tokens=10**9)
    assert vec_extension_available()
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


def _fill(ing, owner=""):
    for t in TEXTS:
        ing.remember_message("s", "user", t, owner=owner)


def test_build_and_status(ing):
    _fill(ing)
    assert ing.store.vec_index_status() == {"mode": "off"}
    n = ing.store.build_vec_index(16)
    assert n == len(TEXTS)
    assert ing.store.vec_index_status() == {"mode": "ready", "dim": 16}


def test_knn_parity_with_brute_force(ing):
    from unified_memory.ingest import Ingest as I
    _fill(ing)
    ing.store.build_vec_index(16)
    ing_off = I(ing.store, ing.backend, ExtractiveSummarizer(),
                Config(context_tokens=10**9, vec_index="off"))
    for q in ("город река", "квантовая физика", "запись номер"):
        assert ing.store.knn(ing.backend.embed_query(q), None, k=5)  # индекс отвечает
        r_on, r_off = ing.router(), ing_off.router()
        brute = r_on.recall(q, limit=5)  # через индекс — тот же порядок?
        plain = r_off.recall(q, limit=5)
        assert [(h.owner_table, h.owner_id) for h in brute] == \
               [(h.owner_table, h.owner_id) for h in plain]
        assert r_on.last_stats["vec_index"] == "knn"
        assert r_off.last_stats["vec_index"] == "brute"


def test_knn_owner_and_table_filters(ing):
    _fill(ing, owner="alice")
    _fill(ing, owner="bob")
    ing.store.build_vec_index(16)
    assert ing.store.knn(ing.backend.embed_query("город"), None, owner="alice", k=50)
    rows = ing.store.knn(ing.backend.embed_query("город"), None, owner="alice", k=50)
    owners = ing.store.owners_for([(ot, oid) for ot, oid, _ in rows])
    assert all(o == "alice" for o in owners.values())
    facts_only = ing.store.knn(ing.backend.embed_query("город"), ["um_facts"], k=5)
    assert facts_only == []  # фактов нет — пусто, а не чужое


def test_writes_and_deletes_sync(ing):
    _fill(ing)
    ing.store.build_vec_index(16)
    mid = ing.remember_message("s", "user", "свежая запись про город")["id"]
    rows = ing.store.knn(ing.backend.embed_query("город"), ["um_messages"], k=50)
    assert ("um_messages", mid) in [(ot, oid) for ot, oid, _ in rows]
    ing.store.delete_fact(999)  # чужого нет — False, индекс не тронут
    fid = ing.remember_fact("k", "n", "факт про город")
    assert ing.store.knn(ing.backend.embed_query("город"), ["um_facts"], k=50)
    assert ing.store.delete_fact(fid) is True
    assert ing.store.knn(ing.backend.embed_query("город"), ["um_facts"], k=50) == []


def test_stale_dim_falls_back(ing):
    _fill(ing)
    ing.store.build_vec_index(16)
    assert ing.store.knn([0.1] * 8, None, k=5) is None  # чужой dim → None
    ing.store.meta_set("vec_index_dim", "8")  # мета врёт: индекс stale
    r = ing.router()
    hits = r.recall("город")  # recall живёт через brute force
    assert hits and r.last_stats["vec_index"] == "brute"


def test_reindex_builds_index(ing):
    _fill(ing)
    rep = ing.reindex()
    assert rep["vec_index"] == len(TEXTS)
    assert ing.store.vec_index_status() == {"mode": "ready", "dim": 16}


def test_vec_index_config(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_VEC_INDEX", "sometimes")
    with pytest.raises(ValueError, match="UM_VEC_INDEX"):
        Config(db_path=tmp_path / "d.db")


LIVE = pytest.mark.skipif(not __import__("os").environ.get("UM_LIVE_OPENAI"),
                          reason="UM_LIVE_OPENAI=1 for potion parity")


@LIVE
def test_live_potion_parity(tmp_path):
    """Паритет на настоящих potion-векторах (нормализованы ли — покажет проба)."""
    import os
    from unified_memory.embeddings import make_backend
    url = os.environ.get("UM_EMBEDDING_BASE_URL", "http://127.0.0.1:8127")
    cfg = Config(db_path=tmp_path / "live.db", context_tokens=10**9,
                 embedding_backend="openai", embedding_base_url=url,
                 embedding_model="potion-multilingual-128M-pruned-int8-ruen")
    backend = make_backend(cfg)
    backend.warm()
    store = Store(cfg, embedding_dim=backend.dim, embedding_model=cfg.embedding_model)
    try:
        ing = Ingest(store, backend, ExtractiveSummarizer(), cfg)
        for t in TEXTS:
            ing.remember_message("s", "user", t)
        n = store.build_vec_index(backend.dim)
        assert n == len(TEXTS)
        for q in ("город река", "квантовая физика"):
            knn_ids = [(ot, oid) for ot, oid, _ in
                       store.knn(backend.embed_query(q), None, k=5)]
            cfg_off = Config(context_tokens=10**9, vec_index="off")
            from unified_memory.ingest import Ingest as I
            plain = I(store, backend, ExtractiveSummarizer(), cfg_off).router().recall(q, limit=5)
            fused = ing.router().recall(q, limit=5)
            assert [(h.owner_table, h.owner_id) for h in fused] == \
                   [(h.owner_table, h.owner_id) for h in plain], f"паритет упал на {q!r}"
    finally:
        store.close()
