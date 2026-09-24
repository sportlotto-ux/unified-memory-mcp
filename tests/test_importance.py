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


def _store(tmp_path, **kwargs):
    cfg = Config(
        db_path=tmp_path / "rank.db",
        archive_path=tmp_path / "rank.archive.db",
        context_tokens=10**9,
        recency_halflife_days=0.0,
        mmr_lambda=1.0,
        vec_index="off",
        **kwargs,
    )
    return cfg, Store(cfg)


def test_importance_is_bounded_and_does_not_override_relevance(tmp_path, monkeypatch):
    cfg, store = _store(tmp_path, importance_weight=0.4)
    exact = store.add_fact("rank", "exact", "target", importance=0.0)
    weak = store.add_fact("rank", "weak", "target " + "noise " * 20, importance=0.95)
    store.add_vector("um_facts", exact, [1.0, 0.0], "test/ordered-2")
    store.add_vector("um_facts", weak, [0.1, 0.995], "test/ordered-2")
    monkeypatch.setattr(store, "fts_search", lambda *args, **kwargs: [])

    try:
        router = Router(store, OrderedBackend(), cfg)
        hits = router.recall("target", scope="facts", limit=2, diagnostics=True)
        assert [h.owner_id for h in hits] == [exact, weak]
        stats = router.last_stats["diagnostics"]["importance"]
        assert stats["weight"] == pytest.approx(0.4)
        assert stats["delta"][f"um_facts:{weak}"] > 0
        assert stats["delta"][f"um_facts:{exact}"] < 0
    finally:
        store.close()


def test_importance_default_preserves_existing_scores(tmp_path, monkeypatch):
    cfg, store = _store(tmp_path)
    exact = store.add_fact("rank", "exact", "target", importance=0.0)
    weak = store.add_fact("rank", "weak", "target " + "noise " * 20, importance=0.95)
    store.add_vector("um_facts", exact, [1.0, 0.0], "test/ordered-2")
    store.add_vector("um_facts", weak, [0.1, 0.995], "test/ordered-2")
    monkeypatch.setattr(store, "fts_search", lambda *args, **kwargs: [])

    try:
        router = Router(store, OrderedBackend(), cfg)
        hits = router.recall("target", scope="facts", limit=2, diagnostics=True)
        assert [h.owner_id for h in hits] == [exact, weak]
        assert hits[0].score == pytest.approx(1.0)
        assert hits[1].score == pytest.approx(0.1, abs=2e-6)
        assert router.last_stats["diagnostics"]["importance"]["weight"] == 0.0
    finally:
        store.close()


def test_importance_for_is_batch_and_fact_only(tmp_path):
    _, store = _store(tmp_path)
    fid = store.add_fact("rank", "fact", "body", importance=0.73)
    mid = store.add_message("s", "user", "message")
    try:
        values = store.importance_for([
            ("um_facts", fid), ("um_messages", mid), ("um_facts", 999),
        ])
        assert values == {("um_facts", fid): pytest.approx(0.73)}
    finally:
        store.close()


def test_importance_weight_validation(tmp_path):
    with pytest.raises(ValueError, match="UM_IMPORTANCE_WEIGHT"):
        Config(db_path=tmp_path / "bad.db", importance_weight=-0.1)
    with pytest.raises(ValueError, match="UM_IMPORTANCE_WEIGHT"):
        Config(db_path=tmp_path / "bad.db", importance_weight=1.1)
