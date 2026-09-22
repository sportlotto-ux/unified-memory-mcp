"""v0.7.1 hygiene: удалён мёртвый холодный поиск (P3.4B) — grep-контроль + поведение."""

import pathlib
import time

import pytest

from unified_memory.config import Config
from unified_memory.store import Store

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "unified_memory"


def test_dead_cold_api_removed():
    # archive.search удалён целиком
    assert "def search(" not in (SRC / "archive.py").read_text(encoding="utf-8")
    # include_archived не воскрес нигде
    for p in SRC.glob("*.py"):
        assert "include_archived" not in p.read_text(encoding="utf-8"), p.name


@pytest.fixture
def store(tmp_path):
    s = Store(Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                     context_tokens=10**9))
    yield s
    s.close()


def test_archived_stub_hidden_from_context(store):
    mid = store.add_message("s", "user", "hello world")
    store.conn.execute("UPDATE um_messages SET externalized_ref=? WHERE id=?",
                       ("cold.db#1", mid))
    store.conn.commit()
    assert store.session_messages("s") == []          # контекст/компакшн
    got = store.recent(0.0, time.time() + 10, limit=10)
    assert all(i["id"] != mid for i in got)           # temporal
