"""v0.7 п.5, ядро: транзакционный контур Store (savepoint) + _commit-флаг.

Критический guardrail: внутренние commit() не должны разрывать batch/dry-run.
Эти тесты доказывают «ничего не записалось», а не «вернулся rollback».
"""

import sqlite3

import pytest

from unified_memory.config import Config
from unified_memory.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                     context_tokens=10**9))
    yield s
    s.close()


def _n(st, table):
    return st.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_transaction_commit(store):
    n = _n(store, "um_facts")
    with store.transaction():
        store.add_fact("a", "b", "c", _commit=False)
    assert _n(store, "um_facts") == n + 1


def test_transaction_rollback_on_error(store):
    n = _n(store, "um_facts")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.add_fact("a", "b", "c", _commit=False)
            raise RuntimeError("boom")
    assert _n(store, "um_facts") == n  # ничего не записано


def test_dry_run_writes_nothing(store):
    nf, nm = _n(store, "um_facts"), _n(store, "um_messages")
    with store.transaction(dry_run=True):
        store.add_fact("a", "b", "c", _commit=False)
        store.add_message("s", "user", "hello", _commit=False)
    assert _n(store, "um_facts") == nf
    assert _n(store, "um_messages") == nm


def test_savepoint_isolates_failed_op(store):
    with store.transaction():
        store.add_fact("a", "k", "v1", _commit=False)
        with pytest.raises(ValueError):
            with store.savepoint():
                store.add_fact("a", "j", "v2", _commit=False)
                raise ValueError("bad op")
        store.add_fact("a", "l", "v3", _commit=False)
    names = {r[0] for r in store.conn.execute("SELECT name FROM um_facts")}
    assert {"k", "l"} <= names and "j" not in names


def test_upsert_supersede_inside_batch(store):
    with store.transaction():
        store.add_fact("a", "slot", "one", _commit=False)
        store.add_fact("a", "slot", "two", _commit=False)  # supersede в batch
    rows = list(store.conn.execute(
        "SELECT body, valid_until FROM um_facts WHERE name='slot' ORDER BY id"))
    assert [r[0] for r in rows] == ["one", "two"]
    assert rows[1][1] == 0.0          # новое тело живое
    assert rows[0][1] > 0.0           # старое вытеснено


def test_legacy_path_still_commits(store):
    store.add_fact("a", "m", "v")  # default _commit=True
    other = sqlite3.connect(store._db_path)
    try:
        assert other.execute("SELECT count(*) FROM um_facts").fetchone()[0] == 1
    finally:
        other.close()


def test_nested_savepoints_reentrant(store):
    # RLock реентерабелен; вложенные savepoint'ы не путаются
    with store.transaction():
        with store.savepoint():
            store.add_fact("a", "x", "1", _commit=False)
            with store.savepoint():
                store.add_fact("a", "y", "2", _commit=False)
    assert _n(store, "um_facts") == 2
