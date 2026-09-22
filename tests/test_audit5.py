"""Регрессия по само-аудиту v0.5 (внешние ревьюеры недоступны — ревью своё).

Каждый тест падает на коде до фикса. Findings:
- A1: update_fact молча игнорирует body, если задан valid_until.
- A2: reopen вытеснённого факта ломает partial unique index (сырой IntegrityError).
- A3: reopen ручного expire не возвращает рёбра.
- A4: архивные заглушки текут в session_messages/assemble/compact/recent.
"""

import sqlite3
import time

import pytest

from unified_memory import archive
from unified_memory.config import Config
from unified_memory.engine import ActiveWindow
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def store(tmp_path):
    s = Store(Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                     context_tokens=10**9))
    yield s
    s.close()


def test_a1_update_body_ignored_when_valid_until_given(store):
    fid = store.add_fact("p", "чай", "зелёный")
    store.update_fact(fid, valid_until=time.time())          # истекло
    out = store.update_fact(fid, valid_until=0, body="чёрный")  # reopen+edit одним вызовом
    live = store.select("SELECT id, body FROM um_facts WHERE valid_until=0")
    assert len(live) == 1 and live[0][1] == "чёрный", f"body потерян: {live} (out={out})"


def test_a2_reopen_superseded_is_clean_error(store):
    fid = store.add_fact("p", "чай", "зелёный")
    store.update_fact(fid, body="чёрный")  # фид вытеснен, есть живой преемник
    with pytest.raises(ValueError, match="live version"):
        store.update_fact(fid, valid_until=0)  # не должно быть сырого IntegrityError
    # стор цел и консистентен
    assert store.select("SELECT count(*) FROM um_facts WHERE valid_until=0")[0][0] == 1


def test_a3_reopen_manual_expire_revives_edges(store):
    fid = store.add_fact("p", "x", "y")
    store.add_edge("Иван", "любит", "чай", fact_id=fid)
    assert len(store.neighbors("иван")) == 1
    store.update_fact(fid, valid_until=time.time())  # ручное истечение факта
    assert store.neighbors("иван") == []  # ребро факта тоже истекло
    store.update_fact(fid, valid_until=0)  # reopen
    assert len(store.neighbors("иван")) == 1, "reopen не вернул рёбра факта"


def test_a4_archived_stubs_do_not_leak(store):
    mid = store.add_message("s", "user", "старое сообщение про бюджет", "mcp")
    store.conn.execute("UPDATE um_messages SET created_at=? WHERE id=?",
                       (time.time() - 999 * 86400, mid))
    store.conn.commit()
    conn = archive.open_archive(":memory:")
    # явный вынос
    from unified_memory.archive import move_oldest
    move_oldest(store, conn, limit=1, label="mem")
    conn.close()
    # session_messages не должен отдавать заглушку
    rows = store.session_messages("s")
    assert all(r["content"] != "[archived]" for r in rows), rows
    # assemble — тоже
    w = ActiveWindow(store, ExtractiveSummarizer(), store.cfg
                     if hasattr(store, "cfg") else Config(context_tokens=10**9))
    asm = w.assemble("s")
    assert all("[archived]" not in m["content"] for m in asm["tail"]), asm["tail"]
    # recent — тоже
    now = time.time()
    rec = store.recent(now - 1000 * 86400, now + 86400)
    assert all(r["body"] != "[archived]" for r in rec), rec
