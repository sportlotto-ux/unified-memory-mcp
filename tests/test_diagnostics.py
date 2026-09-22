"""v0.7 п.6: mem_recall(diagnostics) — аддитивность + per-arm/BFS-статы."""

import json

import pytest


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda c: None)  # FTS-only
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield m
    if m._STATE.get("store") is not None:
        m._STATE["store"].close()
    m._STATE.update(ingest=None, store=None, cfg=None)


def test_diagnostics_off_is_bare_list(srv):
    srv.mem_fact("pref", "a", "яблоко красное")
    out = json.loads(srv.mem_recall(query="яблоко", scope="facts"))
    assert isinstance(out, list)  # контракт v0.6 не изменён


def test_diagnostics_on_wraps_and_counts(srv, monkeypatch):
    monkeypatch.setenv("UM_RECENCY_HALFLIFE_DAYS", "0")  # детерминизм хитов
    srv.mem_fact("pref", "a", "яблоко красное")
    off = json.loads(srv.mem_recall(query="яблоко", scope="facts"))
    on = json.loads(srv.mem_recall(query="яблоко", scope="facts", diagnostics=True))
    assert isinstance(on, dict)
    assert on["hits"] == off  # аддитивность: хиты не изменились
    d = on["diagnostics"]
    assert d["arms"]["fts"] >= 1
    assert "fts_ms" in d["timings_ms"] and "fuse_ms" in d["timings_ms"]
    assert {"vectors_enabled", "fts", "vec_index", "dim_skipped"} <= set(d["degraded"])
    assert d["degraded"]["vectors_enabled"] is False


def test_diagnostics_bfs_stats(srv):
    a = json.loads(srv.mem_fact("pref", "a", "яблоко красное"))["id"]
    b = json.loads(srv.mem_fact("pref", "b", "банан"))["id"]
    srv.mem_link(f"fact:{a}", f"fact:{b}", "derives_from")
    on = json.loads(srv.mem_recall(query="яблоко", scope="facts", hops=2,
                                   diagnostics=True))
    d = on["diagnostics"]
    assert d["hops"] == 2
    assert d["bfs"]["links"] >= 1 and d["bfs"]["emitted"] >= 1
