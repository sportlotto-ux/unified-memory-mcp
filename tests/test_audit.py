"""Регрессия по внешнему аудиту (6 пунктов). Каждый тест падает на старом коде."""

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path):
    store = Store(Config(db_path=tmp_path / "a.db"))
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer())
    store.close()


def _wraps(fn):
    n, cur = 0, fn
    while hasattr(cur, "__wrapped__"):
        n += 1
        cur = cur.__wrapped__
    return n


def test_1_stats_has_lock_add_entity_single_wrap():
    from unified_memory.store import Store as S
    assert _wraps(S.stats) == 1, "stats без лока (count без блокировки)"
    assert _wraps(S.add_entity) == 1, "двойная обёртка add_entity"


def test_2_graph_arm_without_fts_and_vectors(tmp_path):
    store = Store(Config(db_path=tmp_path / "b.db"))  # backend None
    try:
        store.add_edge("Zebra", "likes", "grass")
        store.fts_search = lambda *a, **k: []  # FTS тоже пуст
        from unified_memory.recall import Router
        hits = Router(store, None).recall("Zebra")
        assert any(h.owner_table == "um_edges" for h in hits), \
            "граф-рука пропущена при пустых FTS+векторах"
    finally:
        store.close()


def test_3_fts_returns_edges(ing):
    ing.store.add_edge("Zebra", "likes", "grass")
    hits = ing.store.fts_search("Zebra")
    assert any(h.owner_table == "um_edges" for h in hits), "мёртвый индекс рёбер"


def test_4_forget_cleans_graph(ing):
    fid = ing.remember_fact("p", "чай", "любит", subject="Иван",
                            predicate="любит", obj="чай")
    assert ing.store.stats()["um_edges"] == 1
    assert ing.store.delete_fact(fid) is True
    st = ing.store.stats()
    assert st["um_edges"] == 0, "рёбра-сироты остались"
    assert st["um_entities"] == 0, "сущности-сироты остались"
    assert ing.router().recall("Иван") == [], "мусор после forget"


def test_4b_forget_edge_and_entity_direct(ing):
    eid = ing.store.add_edge("Zebra", "likes", "grass")  # fact_id=0, прямой вызов
    assert ing.store.delete_edge(eid) is True
    assert ing.store.delete_edge(eid) is False
    assert ing.store.stats()["um_edges"] == 0
    ing.store.add_edge("Zebra", "likes", "grass")
    assert ing.store.delete_entity("zebra") is True
    assert ing.store.delete_entity("zebra") is False
    st = ing.store.stats()
    assert st["um_edges"] == 0 and st["um_entities"] == 0


def test_1b_summary_session_isolation(ing):
    ing.store.add_summary("s2", "pizza-night-summary-body")
    ing.remember_message("s1", "user", "про серверы и деплой")
    hits = ing.router().recall("pizza", scope="session", session_id="s1")
    assert all(h.owner_table != "um_summaries" for h in hits), "утечка саммари"
    hits_all = ing.store.fts_search("pizza", scope="all")
    assert any(h.owner_table == "um_summaries" for h in hits_all)


def test_5b_skip_reembed_known_entity(ing, tmp_path):
    calls = []
    backend = FakeBackend()
    orig = backend.embed_docs
    backend.embed_docs = lambda ts: (calls.append(len(ts)), orig(ts))[1]
    from unified_memory.ingest import Ingest as I
    ing2 = I(ing.store, backend)
    ing2.remember_fact("p", "f1", "b", subject="Иван", predicate="знает", obj="x")
    n1 = len(calls)
    ing2.remember_fact("p", "f2", "b", subject="Иван", predicate="видел", obj="y")
    n2 = len(calls)
    # второй факт: факт-вектор + ребро-вектор + 1 новая сущность (Иван пропущен)
    assert n2 - n1 == 3, f"пересчёт известного вектора: {n2 - n1}"


def test_indexes_exist(ing):
    idx = {r[0] for r in ing.store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_um_edges_fact" in idx and "idx_um_edges_session" in idx


def test_5_no_entity_vector_dupes(ing):
    for i in range(3):
        ing.remember_fact("p", f"f{i}", "body", subject="Иван",
                          predicate="знает", obj=f"факт{i}")
    n = ing.store.conn.execute(
        "SELECT count(*) FROM um_vectors WHERE owner_table='um_entities'").fetchone()[0]
    assert ing.store.stats()["um_entities"] == 4  # иван + 3 объекта
    assert n == 4, f"дубли векторов сущностей: {n}"


def test_6_session_scope_no_foreign_edges(ing):
    ing.store.add_edge("Иван", "любит", "чай", session_id="s2")
    hits = ing.router().recall("Иван", scope="session", session_id="s1")
    assert all(h.owner_table != "um_edges" for h in hits), "утечка чужих рёбер"
    hits_all = ing.router().recall("Иван", scope="all")
    assert any(h.owner_table == "um_edges" for h in hits_all)
