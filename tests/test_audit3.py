"""Регрессия по четвёртому аудиту (добивочный v0.3.4)."""

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store, estimate_tokens
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", context_tokens=10**9)
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


def test_scope_validated(ing):
    with pytest.raises(ValueError):
        ing.router().recall("x", scope="nope")
    with pytest.raises(ValueError):
        ing.store.fts_search("x", scope="nope")


def test_limit_zero_empty(ing):
    ing.remember_message("s1", "user", "текст про бюджет")
    assert ing.router().recall("бюджет", limit=0) == []


def test_empty_content_rejected(ing):
    with pytest.raises(ValueError):
        ing.remember_message("s1", "user", "   ")


def test_config_validation(tmp_path):
    with pytest.raises(ValueError):
        Config(db_path=tmp_path / "x.db", dag_fanin=1)
    with pytest.raises(ValueError):
        Config(db_path=tmp_path / "x.db", compact_threshold=0)
    with pytest.raises(ValueError):
        Config(db_path=tmp_path / "x.db", fresh_tail=-1)


def test_display_name_kept(ing):
    ing.store.add_edge("Иван", "любит", "Чай")
    nbs = ing.store.neighbors("иван")
    assert nbs[0]["subject"] == "Иван"
    assert nbs[0]["object"] == "Чай"
    body, _ = ing.store._body_of("um_edges", nbs[0]["edge_id"])
    assert "Иван" in body and "Чай" in body


def test_like_escape_no_overmatch(ing):
    ing.store.add_entity("100%_гарантия")
    ing.store.add_entity("стопроцентная гарантия качества")
    found = ing.store.match_entities(["100%_гарантия"])
    assert found == ["100%_гарантия"]


def test_neighbors_limit(ing):
    for i in range(10):
        ing.store.add_edge("хаб", "связан", f"узел{i}")
    assert len(ing.store.neighbors("хаб", limit=3)) == 3
    assert len(ing.store.neighbors("хаб")) == 10


def test_bodies_for_batch(ing):
    m1 = ing.store.add_message("s1", "user", "раз")
    m2 = ing.store.add_message("s1", "user", "два")
    out = ing.store.bodies_for([("um_messages", m1), ("um_messages", m2),
                                ("um_messages", 999999)])
    assert out[("um_messages", m1)][0] == "раз"
    assert ("um_messages", 999999) not in out


def test_degraded_on_summarizer_failure(tmp_path):
    class Boom:
        def summarize(self, texts, max_sentences=8):
            raise RuntimeError("endpoint down")

    cfg = Config(db_path=tmp_path / "e.db", context_tokens=10,
                 compact_threshold=0.1, fresh_tail=1)
    store = Store(cfg)
    ing = Ingest(store, FakeBackend(), Boom(), cfg)
    try:
        for i in range(3):
            mid = store.add_message("s1", "user", f"длинный текст {i} про бюджет" * 10)
        res = ing.window.maybe_compact("s1")
        assert res["status"] == "degraded"
        assert "endpoint down" in res["error"]
        assert store.get_message(mid)["content"].startswith("длинный")
    finally:
        store.close()


def test_reindex_idempotent(ing):
    ing.remember_message("s1", "user", "текст для индекса")
    ing.remember_fact("p", "n", "b", subject="A", predicate="к", obj="B")
    # снести часть векторов вручную
    ing.store.execute_write("DELETE FROM um_vectors WHERE owner_table='um_messages'")
    r1 = ing.reindex()
    assert r1["embedded"] == 1
    r2 = ing.reindex()
    assert r2["embedded"] == 0


def test_reindex_no_backend(tmp_path):
    store = Store(Config(db_path=tmp_path / "f.db"))
    try:
        with pytest.raises(ValueError):
            Ingest(store, None).reindex()
    finally:
        store.close()


def test_counter_subtracts_superseded(ing):
    for i in range(8):
        ing.remember_message("s1", "user", f"факт {i} про запуск и отчёт" * 3)
    live = ing.store.select(
        "SELECT body FROM um_summaries WHERE session_id='s1' AND superseded_by=0")
    msgs = ing.store.session_messages("s1", limit=1000000)
    expect = sum(estimate_tokens(m["content"]) for m in msgs)
    expect += sum(estimate_tokens(r[0]) for r in live)
    assert int(ing.store.meta_get("tokens:s1")) == expect


def test_recall_truncation_marked(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "g.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("UM_EMBEDDING_MODEL",
                       "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    import unified_memory.server as srv
    srv._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    try:
        srv.mem_remember(session_id="s", content="длинный текст " * 500)
        import json
        hits = json.loads(srv.mem_recall(query="длинный"))
        assert hits and hits[0]["body"].endswith("…[truncated, use mem_expand for full text]")
    finally:
        srv._STATE["store"].close()
        srv._STATE.update(ingest=None, store=None, cfg=None)


def test_strict_env_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DAG_FANIN", "мусор")
    from unified_memory.config import load
    with pytest.raises(ValueError):
        load()


def test_config_fresh_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_FRESH_TAIL_COUNT", "7")
    from unified_memory.config import Config as C
    assert C(db_path=tmp_path / "h.db").fresh_tail == 7
