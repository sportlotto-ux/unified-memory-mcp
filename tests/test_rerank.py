"""Пункт 3 v0.4: recency-приор + scope-bias + MMR. FTS-only, детерминировано."""

import time

import pytest

from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", context_tokens=10**9)
    store = Store(cfg)
    yield Ingest(store, None, ExtractiveSummarizer(), cfg)  # FTS-only: все score=1.0
    store.close()


def _backdate(store, table, oid, days):
    store.conn.execute(f"UPDATE {table} SET created_at=? WHERE id=?",
                       (time.time() - days * 86400, oid))
    store.conn.commit()


def test_recency_fresh_first(ing):
    ing.remember_message("s", "user", "отчёт про бюджет за январь")
    old_id = ing.store.select("SELECT id FROM um_messages")[0][0]
    _backdate(ing.store, "um_messages", old_id, 60)
    ing.remember_message("s", "user", "отчёт про бюджет за декабрь")
    hits = ing.router().recall("отчёт бюджет")
    assert len(hits) == 2
    assert "декабрь" in hits[0].body  # 60 дней при halflife 30: вес ~0.57 vs ~1.0


def test_recency_off_keeps_tie(ing):
    cfg = Config(context_tokens=10**9, recency_halflife_days=0.0)
    ing2 = Ingest(ing.store, None, ExtractiveSummarizer(), cfg)
    ing.remember_message("s", "user", "отчёт про бюджет за январь")
    old_id = ing.store.select("SELECT id FROM um_messages")[0][0]
    _backdate(ing.store, "um_messages", old_id, 60)
    ing.remember_message("s", "user", "отчёт про бюджет за декабрь")
    hits = ing2.router().recall("отчёт бюджет")
    assert hits[0].score == hits[1].score == 1.0


def test_scope_bias_current_session(ing):
    ing.remember_message("s1", "user", "деплой на проде вечером")
    ing.remember_message("s2", "user", "деплой на проде утром")
    hits = ing.router().recall("деплой проде", scope="all", session_id="s2")
    assert hits[0].session_id == "s2"  # +15% к своему при scope=all
    tied = ing.router().recall("деплой проде", scope="all", session_id="")
    assert tied[0].score == pytest.approx(tied[1].score, abs=1e-6)  # ничья с точностью до ресенси


def test_mmr_diversifies_dag_dupes(ing):
    ing.remember_message("s", "user", "кот сидит на коврике")
    ing.remember_message("s", "user", "кот сидит на коврике и мурлычет")
    ing.remember_message("s", "user", "собака лает во дворе громко")
    hits = ing.router().recall("кот коврик собака лает", limit=2)
    bodies = [h.body for h in hits]
    assert any("собака" in b for b in bodies)  # разное выжило
    assert not sum("кот сидит на коврике" in b for b in bodies) == 2  # дубли не забили топ


def test_mmr_off_pure_order(ing):
    cfg = Config(context_tokens=10**9, mmr_lambda=1.0)
    ing2 = Ingest(ing.store, None, ExtractiveSummarizer(), cfg)
    for t in ("альфа бета", "альфа гамма", "дельта эпсилон"):
        ing.remember_message("s", "user", t)
    hits = ing2.router().recall("альфа", limit=2)
    assert len(hits) == 2
    assert hits[0].score >= hits[1].score


def test_created_for_batch(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", context_tokens=10**9)
    store = Store(cfg)
    try:
        mid = store.add_message("s", "user", "x", "mcp")
        got = store.created_for([("um_messages", mid), ("um_facts", 999),
                                 ("nope", 1)])
        assert got[("um_messages", mid)] > 0
        assert ("um_facts", 999) not in got and ("nope", 1) not in got
    finally:
        store.close()


def test_rerank_config_validation(tmp_path):
    with pytest.raises(ValueError, match="UM_RECENCY_HALFLIFE_DAYS"):
        Config(db_path=tmp_path / "d.db", recency_halflife_days=-1)
    with pytest.raises(ValueError, match="UM_SCOPE_BIAS"):
        Config(db_path=tmp_path / "d.db", scope_bias=-0.1)
    with pytest.raises(ValueError, match="UM_MMR_LAMBDA"):
        Config(db_path=tmp_path / "d.db", mmr_lambda=1.5)
