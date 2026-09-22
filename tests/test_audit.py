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
    assert ing.router().recall("Иван") == [] or all(
        h.owner_table != "um_edges" for h in ing.router().recall("Иван"))


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
