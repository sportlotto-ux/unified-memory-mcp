"""v0.8 D15: bounded assemble/compact — тот же результат, меньше прочитанных строк."""

import pytest
from fake_backend import FakeBackend

from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "b.db", context_tokens=60,
                 compact_threshold=0.5, fresh_tail=1, dag_fanin=3,
                 assembly_budget=40)
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


def test_assemble_bounded_tail_equivalent(ing, monkeypatch):
    for i in range(12):
        ing.remember_message("s", "user", f"сообщение номер {i} про бюджет и отчёт")
    expected = ing.window.assemble("s")

    def boom(*a, **k):
        raise AssertionError("assemble не должен читать всю сессию")

    monkeypatch.setattr(ing.store, "session_messages", boom)
    assert ing.window.assemble("s") == expected   # DESC/LIMIT-хвост, без полного скана


def test_compact_head_bounded_by_cap(ing, monkeypatch):
    for i in range(12):
        ing.store.add_message("s", "user", f"строка {i} " * 20)
    ing.window.pressure("s")                      # одноразовый backfill счётчика
    limits: list = []
    orig = ing.store.session_messages

    def rec(*a, **k):
        limits.append(k.get("limit"))
        return orig(*a, **k)

    monkeypatch.setattr(ing.store, "session_messages", rec)
    out = ing.window.maybe_compact("s")
    assert out["status"] == "compacted"
    assert limits and all(l <= ing.cfg.compact_max_msgs for l in limits)
    assert all(l != 1_000_000 for l in limits)
    assert ing.store.meta_get("frontier:s")            # frontier сдвинулся


def test_compact_max_msgs_validation(tmp_path):
    with pytest.raises(ValueError, match="UM_COMPACT_MAX_MSGS"):
        Config(db_path=tmp_path / "x.db", compact_max_msgs=0)
