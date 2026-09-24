import pytest

from unified_memory.config import Config
from unified_memory.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(Config(db_path=tmp_path / "t.db"))
    yield s
    s.close()


def test_remember_and_expand(store):
    mid = store.add_message("s1", "user", "кот сидит на ковре")
    msg = store.get_message(mid)
    assert msg["content"] == "кот сидит на ковре"
    assert msg["session_id"] == "s1"


def test_facts_capped_importance(store):
    fid = store.add_fact("preference", "чай", "любит зелёный", importance=5.0)
    row = store.conn.execute(
        "SELECT importance FROM um_facts WHERE id=?", (fid,)).fetchone()
    assert row[0] == 1.0  # кап вместо жёстких 0.95


def test_fts_search(store):
    store.add_message("s1", "user", "встреча в четверг по бюджету")
    store.add_message("s1", "user", "кот сидит на ковре")
    hits = store.fts_search("бюджет")
    assert len(hits) == 1
    assert "бюджет" in hits[0].body


def test_session_scope(store):
    store.add_message("s1", "user", "пароль от роутера 1234")
    store.add_message("s2", "user", "пароль от роутера 1234")
    hits = store.fts_search("пароль", scope="session", session_id="s1")
    assert {h.session_id for h in hits} == {"s1"}


def test_session_scope_like_fallback_filters_summaries(store):
    store.add_summary("s1", "бюджет встречи")
    store.add_summary("s2", "бюджет встречи")
    store.fts = False
    hits = store.fts_search("бюджет", scope="session", session_id="s1")
    assert len(hits) == 1
    assert hits[0].session_id == "s1"


def test_forget_fact(store):
    fid = store.add_fact("c", "n", "b")
    assert store.delete_fact(fid) is True
    assert store.delete_fact(fid) is False


def test_stats(store):
    store.add_message("s", "user", "hi")
    st = store.stats()
    assert st["um_messages"] == 1
