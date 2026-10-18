"""Приёмка раздельных FTS-индексов: стенограммы не двигают ранжирование фактов.

Причина правки: bm25 считает IDF по корпусу своей таблицы, поэтому захват
истории (1245 сообщений) сдвигал ранжирование фактов — `scope=facts` hit@1 падал
68% → 63%, а вывод сообщений из общего индекса возвращал метрики к исходным
(замер 18.10.2026).
"""

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.recall import Router
from unified_memory.store import Store, fts_table_for


@pytest.fixture
def env(tmp_path):
    cfg = Config(db_path=tmp_path / "f.db", archive_path=tmp_path / "f.arc.db",
                 context_tokens=10**9)
    store = Store(cfg)
    yield cfg, store, Ingest(store, FakeBackend(), cfg=cfg)
    store.close()


def test_index_routing_by_kind(env):
    _cfg, store, ing = env
    assert fts_table_for("um_messages") == "um_fts_msg"
    assert fts_table_for("um_facts") == "um_fts"
    assert fts_table_for("um_edges") == "um_fts"
    ing.remember_message("s1", "user", "стенограмма про порт 8127")
    ing.remember_fact("infra", "порт", "эмбеддинги слушают порт 8127")
    assert store.select("SELECT count(*) FROM um_fts_msg")[0][0] == 1
    assert store.select("SELECT count(*) FROM um_fts")[0][0] == 1
    assert store.select(
        "SELECT count(*) FROM um_fts WHERE owner_table='um_messages'")[0][0] == 0


def test_messages_still_searchable(env):
    cfg, store, ing = env
    ing.remember_message("s1", "user", "реквизиты сертификата BUS202",
                         source="opencode")
    hits = Router(store, FakeBackend(), cfg).recall(
        "сертификат BUS202", scope="session", session_id="s1",
        expire_working=False)
    assert hits and hits[0].owner_table == "um_messages"


def test_fact_ranking_is_immune_to_transcript_volume(env):
    """Ключевое свойство: тонна сообщений с теми же словами не сдвигает факты."""
    cfg, store, ing = env
    ing.remember_fact("infra", "порт", "эмбеддинги слушают порт 8127")
    ing.remember_fact("infra", "ветка", "фикс в ветке fix/thumbnails")
    router = Router(store, FakeBackend(), cfg)
    query = "порт 8127 эмбеддинги"
    before = [h.owner_id for h in router.recall(query, scope="facts",
                                                expire_working=False)]
    for i in range(60):
        ing.remember_message(f"s{i}", "assistant",
                             f"разбирали порт 8127 и эмбеддинги, шаг {i}")
    after = [h.owner_id for h in router.recall(query, scope="facts",
                                               expire_working=False)]
    assert before, "факт должен находиться до налива стенограмм"
    assert before == after, "выдача scope=facts изменилась от объёма стенограмм"


def test_migration_moves_legacy_message_rows(tmp_path):
    """Старая БД: сообщение в общем индексе → переезжает в свой при открытии."""
    cfg = Config(db_path=tmp_path / "legacy.db",
                 archive_path=tmp_path / "legacy.arc.db", context_tokens=10**9)
    store = Store(cfg)
    ing = Ingest(store, FakeBackend(), cfg=cfg)
    ing.remember_message("s1", "user", "перенесённая запись про порт 8127")
    # эмулируем legacy-раскладку: строка в общем индексе, свой — пуст
    store.conn.execute(
        "INSERT INTO um_fts(owner_table, owner_id, body)"
        " SELECT 'um_messages', id, content FROM um_messages")
    store.conn.execute("DELETE FROM um_fts_msg")
    store.conn.commit()
    assert store.select("SELECT count(*) FROM um_fts_msg")[0][0] == 0
    store.close()

    store2 = Store(cfg)  # открытие должно выполнить миграцию
    try:
        assert store2.select("SELECT count(*) FROM um_fts_msg")[0][0] == 1
        assert store2.select(
            "SELECT count(*) FROM um_fts"
            " WHERE owner_table='um_messages'")[0][0] == 0
        assert store2.fts_search("перенесённая", scope="session",
                                 session_id="s1")
    finally:
        store2.close()
