"""Приёмка весов видов записей в RRF: «контекст» не должен вытеснять содержимое.

Ребро получает от графового плеча голос верхнего ранга, а его короткое тело
(`субъект --предикат--> объект`) легко совпадает с запросом, поэтому при весе
1.0 рёбра занимали топ-1 в fact-запросах. Замер 18.10.2026 на 19 реальных
запросах: scope=all hit@1 26% → 63% при весе 0.5, scope=facts не сдвинулся.
"""

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.recall import Router, rrf_fuse
from unified_memory.store import Hit, Store


def _h(table: str, oid: int, body: str) -> Hit:
    return Hit(table, oid, body, 1.0)


@pytest.fixture
def make_cfg(tmp_path):
    def _make(**kw):
        cfg = Config(db_path=tmp_path / "e.db",
                     archive_path=tmp_path / "e.archive.db",
                     context_tokens=10**9, **kw)
        return cfg, Store(cfg)
    return _make


def test_edge_weight_damps_edges_in_fusion():
    fact = _h("um_facts", 1, "сервис слушает порт 8127")
    edge = _h("um_edges", 2, "сервис --использует--> порт 8127")
    # ребро приходит двумя плечами, факт — одним: именно так возникал перевес
    lists = [[fact], [edge], [edge]]
    legacy = rrf_fuse(lists, edge_weight=1.0)
    damped = rrf_fuse(lists, edge_weight=0.5)
    assert legacy[0].owner_table == "um_edges"
    assert damped[0].owner_table == "um_facts"
    # ребро не исчезает — оно остаётся находимым, просто ниже содержимого
    assert any(h.owner_table == "um_edges" for h in damped)


def test_edges_still_surface_when_query_is_lexical(make_cfg):
    cfg, store = make_cfg(edge_weight=1.0)
    ing = Ingest(store, FakeBackend(), cfg=cfg)
    ing.remember_fact("infra", "порт", "сервис слушает порт 8127",
                      subject="сервис", predicate="использует", obj="порт 8127")
    hits = Router(store, FakeBackend(), cfg).recall(
        "сервис использует", scope="all", limit=5, expire_working=False)
    assert any(h.owner_table == "um_edges" for h in hits), \
        [h.owner_table for h in hits]
    store.close()


def test_recall_keeps_fact_above_relation(make_cfg):
    cfg, store = make_cfg()
    ing = Ingest(store, FakeBackend(), cfg=cfg)
    ing.remember_fact("infra", "порт", "эмбеддинги слушают на порту 8127",
                      subject="unified-memory", predicate="использует",
                      obj="порт 8127")
    hits = Router(store, FakeBackend(), cfg).recall(
        "порт 8127 эмбеддинги", scope="all", limit=5, expire_working=False)
    assert hits, "recall ничего не вернул"
    assert hits[0].owner_table == "um_facts", [h.owner_table for h in hits]
    store.close()


def test_message_weight_damps_transcripts():
    fact = _h("um_facts", 1, "сертификат BUS202 сдан 24.09.2026")
    note = _h("um_messages", 2, "разбирали сертификат BUS202 и порт 8127")
    # стенограмма приходит двумя плечами, факт — одним: тот же перевес, что у рёбер
    lists = [[fact], [note], [note]]
    legacy = rrf_fuse(lists, message_weight=1.0)
    damped = rrf_fuse(lists, message_weight=0.5)
    assert legacy[0].owner_table == "um_messages"
    assert damped[0].owner_table == "um_facts"
    assert any(h.owner_table == "um_messages" for h in damped)


def test_recall_prefers_fact_over_transcript(make_cfg):
    cfg, store = make_cfg()
    ing = Ingest(store, FakeBackend(), cfg=cfg)
    ing.remember_message("ses_1", "user", "какой порт слушают эмбеддинги",
                         source="opencode")
    ing.remember_message("ses_1", "assistant", "эмбеддинги слушают порт 8127",
                         source="opencode")
    ing.remember_fact("infra", "порт", "эмбеддинги слушают порт 8127")
    hits = Router(store, FakeBackend(), cfg).recall(
        "порт 8127 эмбеддинги", scope="all", limit=5, expire_working=False)
    assert hits and hits[0].owner_table == "um_facts", \
        [h.owner_table for h in hits]
    store.close()


def test_message_weight_default_and_validation(tmp_path):
    assert Config(db_path=tmp_path / "mw.db",
                  archive_path=tmp_path / "mw.arc.db").message_weight == 0.5
    with pytest.raises(ValueError):
        Config(db_path=tmp_path / "mw2.db", archive_path=tmp_path / "mw2.arc.db",
               message_weight=-1)


def test_edge_weight_default_and_validation(tmp_path):
    assert Config(db_path=tmp_path / "a.db",
                  archive_path=tmp_path / "a.arc.db").edge_weight == 0.5
    with pytest.raises(ValueError):
        Config(db_path=tmp_path / "b.db", archive_path=tmp_path / "b.arc.db",
               edge_weight=-0.1)
