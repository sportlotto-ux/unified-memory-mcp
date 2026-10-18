"""Приёмка внешнего импорта сообщений: исходный штамп и id источника.

Нужно захвату сессий: без `created_at` все импортированные сообщения получают
время записи (ломает recency-приор), без `source_ref` нет ключа идемпотентности
для повторного прогона.
"""

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "m.db", archive_path=tmp_path / "m.arc.db",
                 context_tokens=10**9)
    store = Store(cfg)
    yield cfg, store, Ingest(store, FakeBackend(), cfg=cfg)
    store.close()


def test_created_at_and_source_ref_are_preserved(ing):
    _cfg, store, ingest = ing
    out = ingest.remember_message("ses_1", "user", "привет, память",
                                  source="opencode", created_at=1_700_000_000.5,
                                  source_ref="msg_abc", source_order=7)
    row = store.select(
        "SELECT session_id, role, source, created_at, source_ref, source_order"
        " FROM um_messages WHERE id=?", (out["id"],))[0]
    assert row == ("ses_1", "user", "opencode", 1_700_000_000.5, "msg_abc", 7)


def test_defaults_keep_previous_behaviour(ing):
    _cfg, store, ingest = ing
    out = ingest.remember_message("ses_2", "assistant", "ответ")
    session, role, source, created, ref = store.select(
        "SELECT session_id, role, source, created_at, source_ref"
        " FROM um_messages WHERE id=?", (out["id"],))[0]
    assert (session, role, source, ref) == ("ses_2", "assistant", "mcp", "")
    assert created > 1_600_000_000  # текущее время, не ноль


def test_message_is_searchable_after_import(ing):
    from unified_memory.recall import Router

    _cfg, store, ingest = ing
    ingest.remember_message("ses_3", "user", "реквизиты сертификата BUS202",
                            source="opencode", source_ref="msg_bus")
    hits = Router(store, FakeBackend(), _cfg).recall(
        "сертификат BUS202", scope="session", session_id="ses_3",
        expire_working=False)
    assert hits and hits[0].session_id == "ses_3"
