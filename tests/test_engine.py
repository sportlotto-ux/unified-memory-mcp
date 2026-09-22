import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "w.db", context_tokens=200,
                 compact_threshold=0.5, fresh_tail=2, dag_fanin=2,
                 assembly_budget=100)
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


def test_auto_compact_on_pressure(ing):
    for i in range(6):
        r = ing.remember_message("s1", "user", f"сообщение {i} про бюджет и планы" * 2)
    assert r["compaction"]["status"] == "compacted"
    st = ing.store.stats()
    assert st["um_summaries"] >= 1
    # lossless
    assert len(ing.store.session_messages("s1", limit=100)) == 6


def test_no_compact_below_threshold(tmp_path):
    cfg = Config(db_path=tmp_path / "w2.db", context_tokens=10**9)
    store = Store(cfg)
    ing = Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    r = ing.remember_message("s1", "user", "коротко")
    assert r["compaction"]["status"] == "ok"
    store.close()


def test_dag_condense_levels(ing):
    for i in range(8):
        ing.remember_message("s1", "user", f"факт {i} про запуск и отчёт" * 3)
    depths = {r[0] for r in ing.store.conn.execute(
        "SELECT DISTINCT depth FROM um_summaries WHERE session_id='s1'")}
    assert 1 in depths  # fanin=2 схлопнул depth 0 в depth 1


def test_assemble_bounded(ing):
    for i in range(10):
        ing.remember_message("s1", "user", f"пункт {i} повестки совещания" * 3)
    asm = ing.window.assemble("s1")
    # bounded с гарантией «новейшее сообщение всегда внутри» (как LCM fresh tail):
    # перелёт не больше одного сообщения
    from unified_memory.store import estimate_tokens
    newest = ing.store.session_messages("s1", limit=1000000)[-1]["content"]
    assert asm["tokens"] <= asm["budget"] + estimate_tokens(newest)
    assert asm["truncated_tail"] is True
    # хвост — самые свежие
    assert "пункт 9" in asm["tail"][-1]["content"]
