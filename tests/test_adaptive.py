"""P2.8: per-call rerank knobs поверх конфига (red-first).

None = конфиг, поведение бит-в-бит. Прецедент детерминированного
ранкинга — tests/test_importance.py (OrderedBackend + stub FTS).
"""

import pytest

from unified_memory.config import Config
from unified_memory.recall import Router
from unified_memory.store import Store


class OrderedBackend:
    dim = 2
    model_name = "test/ordered-2"
    spec_name = "test/ordered-2"

    def embed_query(self, text):
        return [1.0, 0.0]


def _cfg(tmp_path, **kwargs):
    return Config(
        db_path=tmp_path / "rank.db",
        archive_path=tmp_path / "rank.archive.db",
        context_tokens=10**9,
        recency_halflife_days=0.0,
        mmr_lambda=1.0,
        vec_index="off",
        **kwargs,
    )


@pytest.fixture
def ranked(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store = Store(cfg)
    exact = store.add_fact("rank", "exact", "target", importance=0.0)
    weak = store.add_fact("rank", "weak", "target " + "noise " * 20,
                          importance=0.95)
    store.add_vector("um_facts", exact, [1.0, 0.0], "test/ordered-2")
    store.add_vector("um_facts", weak, [0.9, 0.4359], "test/ordered-2")
    monkeypatch.setattr(store, "fts_search", lambda *args, **kwargs: [])
    yield cfg, store, exact, weak
    store.close()


def test_percall_importance_promotes(ranked):
    cfg, store, exact, weak = ranked
    try:
        router = Router(store, OrderedBackend(), cfg)
        default = [h.owner_id for h in router.recall(
            "target", scope="facts", limit=2)]
        boosted = [h.owner_id for h in router.recall(
            "target", scope="facts", limit=2, importance_weight=1.0)]
        assert default == [exact, weak]
        assert boosted == [weak, exact]
    finally:
        pass


def test_percall_rejects_out_of_range(ranked):
    cfg, store, _, _ = ranked
    router = Router(store, OrderedBackend(), cfg)
    with pytest.raises(ValueError):
        router.recall("target", scope="facts", importance_weight=1.5)
    with pytest.raises(ValueError):
        router.recall("target", scope="facts", mmr_lambda=-0.1)
    with pytest.raises(ValueError):
        router.recall("target", scope="facts", scope_bias=-1.0)


def test_none_is_bitwise_config(ranked):
    cfg, store, _, _ = ranked
    router = Router(store, OrderedBackend(), cfg)
    plain = [(h.owner_id, h.score) for h in router.recall(
        "target", scope="facts", limit=2)]
    explicit = [(h.owner_id, h.score) for h in router.recall(
        "target", scope="facts", limit=2, importance_weight=None,
        mmr_lambda=None, scope_bias=None)]
    assert plain == explicit


def test_effective_in_diagnostics(ranked):
    cfg, store, _, _ = ranked
    router = Router(store, OrderedBackend(), cfg)
    router.recall("target", scope="facts", limit=2, diagnostics=True,
                  mmr_lambda=0.3, scope_bias=2.0)
    eff = router.last_stats["diagnostics"]["effective"]
    assert eff == {"importance_weight": 0.0, "mmr_lambda": 0.3,
                   "scope_bias": 2.0}


def test_override_equals_config(ranked):
    cfg, store, exact, weak = ranked
    import dataclasses
    cfg_hot = dataclasses.replace(cfg, importance_weight=1.0)
    r_default = Router(store, OrderedBackend(), cfg)
    r_hot = Router(store, OrderedBackend(), cfg_hot)
    via_config = [h.owner_id for h in r_hot.recall(
        "target", scope="facts", limit=2)]
    via_call = [h.owner_id for h in r_default.recall(
        "target", scope="facts", limit=2, importance_weight=1.0)]
    assert via_config == via_call == [weak, exact]


def test_server_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "server.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import json
    import unified_memory.server as srv
    monkeypatch.setattr(srv, "_backend", lambda cfg: None)
    srv._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    try:
        fid = json.loads(srv.mem_fact("long", "t", "target body"))["id"]
        import re
        out = json.loads(srv.mem_recall("target", scope="facts",
                                        importance_weight=0.5,
                                        diagnostics=True))
        assert isinstance(out["hits"], list)
        assert out["diagnostics"]["effective"]["importance_weight"] == 0.5
        with pytest.raises(ValueError):
            srv.mem_recall("target", importance_weight=2.0)
    finally:
        if srv._STATE.get("store") is not None:
            srv._STATE["store"].close()
        srv._STATE.update(ingest=None, store=None, cfg=None,
                          backend_error=None)
