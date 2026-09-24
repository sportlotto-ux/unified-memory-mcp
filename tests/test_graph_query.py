import json

import pytest

from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store


def _rig(tmp_path):
    cfg = Config(db_path=tmp_path / "graph.db",
                 archive_path=tmp_path / "graph.archive.db",
                 context_tokens=10**9)
    store = Store(cfg)
    ingest = Ingest(store, None, None, cfg)
    return cfg, store, ingest


def _edge_id(store, fact_id):
    return store.select("SELECT id FROM um_edges WHERE fact_id=?", (fact_id,))[0][0]


def test_graph_query_filters_edges_links_and_weight(tmp_path):
    cfg, store, ingest = _rig(tmp_path)
    try:
        first = ingest.remember_fact(
            "graph", "first", "первый факт", subject="Alpha",
            predicate="knows", obj="Beta", owner="tenant-a")
        second = ingest.remember_fact(
            "graph", "second", "второй факт", subject="Beta",
            predicate="supports", obj="Gamma", owner="tenant-a")
        ingest.remember_fact(
            "graph", "foreign", "чужой факт", subject="Alpha",
            predicate="knows", obj="Beta", owner="tenant-b")
        link = store.link("um_facts", first, "um_facts", second,
                          "supports", weight=2.0, owner="tenant-a")

        out = store.graph_query(
            subject="alpha", predicate="KNOWS", object="beta",
            rel="supports", min_weight=1.5, owner="tenant-a", max_hops=1)
        assert [e["subject"] for e in out["edges"]] == ["alpha"]
        assert [e["predicate"] for e in out["edges"]] == ["knows"]
        assert [item["id"] for item in out["links"]] == [link["id"]]
        assert out["links"][0]["rel"] == "supports"
        assert out["links"][0]["weight"] == pytest.approx(2.0)
        assert out["truncated"] is False

        foreign = store.graph_query(
            subject="alpha", object="beta", owner="tenant-b", max_hops=1)
        assert foreign["edges"] and foreign["edges"][0]["id"] != _edge_id(store, first)
        assert store.graph_query(
            subject="alpha", rel="contradicts", owner="tenant-a")["links"] == []
        assert store.graph_query(
            subject="alpha", min_weight=3.0, owner="tenant-a")["links"] == []
    finally:
        store.close()


def test_graph_query_is_deterministic_and_respects_session_liveness(tmp_path):
    cfg, store, ingest = _rig(tmp_path)
    try:
        first = ingest.remember_fact(
            "graph", "first", "первый", subject="A", predicate="p",
            obj="B", session_id="s1", owner="tenant")
        second = ingest.remember_fact(
            "graph", "second", "второй", subject="A", predicate="p",
            obj="C", session_id="s1", owner="tenant")
        store.link("um_facts", first, "um_facts", second, "supports",
                    session_id="s1", owner="tenant")
        eid = _edge_id(store, first)
        now = store.conn.execute("SELECT created_at FROM um_edges WHERE id=?",
                                 (eid,)).fetchone()[0]
        store.conn.execute(
            "UPDATE um_edges SET created_at=?, valid_until=? WHERE id=?",
            (now - 20, now - 10, eid))
        store.conn.commit()

        assert store.graph_query(subject="a", object="b", session_id="s2")["edges"] == []
        assert store.graph_query(subject="a", object="b", session_id="s1")["edges"] == []
        live = store.graph_query(subject="a", object="b", session_id="s1",
                                 include_expired=True)
        assert [e["id"] for e in live["edges"]] == [eid]
        assert store.graph_query(subject="a", object="b", session_id="s1",
                                 as_of=now - 15, include_expired=True)["edges"]
        assert store.graph_query(subject="a", object="b", session_id="s1",
                                 as_of=now - 5, include_expired=True)["edges"] == []
        assert live == store.graph_query(subject="a", object="b", session_id="s1",
                                          include_expired=True)
    finally:
        store.close()


def test_graph_query_expands_typed_links_within_hop_cap(tmp_path):
    cfg, store, ingest = _rig(tmp_path)
    try:
        first = ingest.remember_fact("graph", "first", "first",
                                     subject="A", predicate="p", obj="B")
        second = ingest.remember_fact("graph", "second", "second",
                                      subject="C", predicate="q", obj="D")
        third = ingest.remember_fact("graph", "third", "third",
                                     subject="E", predicate="r", obj="F")
        store.link("um_facts", first, "um_facts", second, "supports")
        store.link("um_facts", second, "um_facts", third, "supports")

        one = store.graph_query(subject="a", rel="supports", max_hops=1)
        two = store.graph_query(subject="a", rel="supports", max_hops=2)
        assert [item["dst"]["id"] for item in one["links"]] == [second]
        assert [item["depth"] for item in one["links"]] == [1]
        assert [item["dst"]["id"] for item in two["links"]] == [second, third]
        assert [item["depth"] for item in two["links"]] == [1, 2]
        assert [item["id"] for item in two["links"]] == sorted(
            item["id"] for item in two["links"])
    finally:
        store.close()


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "srv.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as server
    monkeypatch.setattr(server, "_backend", lambda cfg: None)
    server._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield server
    if server._STATE.get("store") is not None:
        server._STATE["store"].close()
    server._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)


def test_mem_graph_query_wiring_and_validation(srv):
    first = json.loads(srv.mem_fact(
        "graph", "first", "first", subject="A", predicate="p", object="B"))["id"]
    second = json.loads(srv.mem_fact(
        "graph", "second", "second", subject="C", predicate="q", object="D"))["id"]
    srv.mem_link(f"fact:{first}", f"fact:{second}", "supports")
    out = json.loads(srv.mem_graph_query(subject="a", rel="supports"))
    assert out["edges"][0]["subject"] == "a"
    assert out["links"][0]["dst"]["id"] == second
    with pytest.raises(ValueError, match="max_hops must be"):
        srv.mem_graph_query(subject="a", max_hops=0)
    with pytest.raises(ValueError, match="UM_RECALL_MAX_HOPS"):
        srv.mem_graph_query(subject="a", max_hops=99)
    with pytest.raises(ValueError, match="min_weight must be"):
        srv.mem_graph_query(subject="a", min_weight=-1)
