"""P2.4: tasks поверх um_facts category="task" (red-first).

Без новых таблиц: статус в metadata_json live-строки, смена статуса —
metadata-only in-place, history тел — через supersede.
"""

import json

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "task.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = Config(db_path=tmp_path / "task.db")
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "server.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as module
    monkeypatch.setattr(module, "_backend", lambda cfg: None)
    module._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield module
    if module._STATE.get("store") is not None:
        module._STATE["store"].close()
    module._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)


def test_create_and_list_open(srv):
    created = json.loads(
        srv.mem_task("create", name="fix-bug", body="Fix login bug"))
    assert created["status"] == "open" and created["created"] is True
    listed = json.loads(srv.mem_task("list"))
    assert [(t["name"], t["status"]) for t in listed["tasks"]] == [
        ("fix-bug", "open")]
    with pytest.raises(ValueError):
        srv.mem_task("create", name="fix-bug", body="duplicate")


def test_transitions_reopen_and_noop(ing):
    tid = ing.task_create("fix-bug", "Fix login bug")["id"]
    assert ing.task_status(tid, "doing") == {"id": tid, "status": "doing",
                                            "changed": True}
    assert ing.task_status(tid, "done")["status"] == "done"
    assert ing.task_status(tid, "open")["status"] == "open"  # reopen
    assert ing.task_status(tid, "open") == {"id": tid, "status": "open",
                                           "changed": False}
    ing.task_status(tid, "done")
    with pytest.raises(ValueError):
        ing.task_status(tid, "doing")  # done→doing запрещён


def test_rejects(ing):
    with pytest.raises(ValueError):
        ing.task_create("", "body")
    with pytest.raises(ValueError):
        ing.task_create("name", "   ")
    tid = ing.task_create("fix-bug", "Fix login bug", owner="alice")["id"]
    with pytest.raises(ValueError):
        ing.task_status(tid, "shipped")
    with pytest.raises(ValueError):
        ing.task_status(999999, "done")
    with pytest.raises(ValueError):
        ing.task_status(tid, "done", owner="bob")


def test_owner_isolation_and_legacy(ing):
    ing.task_create("a-task", "Alice work", owner="alice")
    assert ing.task_list(owner="bob") == []
    assert [t["name"] for t in ing.task_list()] == ["a-task"]
    assert [t["name"] for t in ing.task_list(owner="alice")] == ["a-task"]
    assert [t["name"] for t in ing.task_list(status="doing")] == []


def test_task_body_is_redacted(ing):
    tid = ing.task_create("keys", "rotate api_key = SECRET1234567890")["id"]
    raw = ing.store.select(
        "SELECT body FROM um_facts WHERE id=?", (tid,))[0][0]
    assert "SECRET1234567890" not in raw


def test_status_survives_supersede_and_export(ing, tmp_path):
    tid = ing.task_create("fix-bug", "Fix login bug")["id"]
    ing.task_status(tid, "doing")
    new_id = ing.update_fact(tid, body="Fix login bug seriously")["id"]
    assert new_id != tid
    assert ing.task_list()[0]["status"] == "doing"
    from unified_memory import export as exp
    from unified_memory import import_dump as imp
    dump = tmp_path / "tasks.jsonl"
    exp.export_store(ing.store, dump)
    cfg2 = Config(db_path=tmp_path / "restore.db")
    store2 = Store(cfg2)
    try:
        rep = imp.import_dump(store2, dump)
        assert rep["um_facts"]["inserted"] >= 1
        rows = store2.select(
            "SELECT metadata_json FROM um_facts WHERE category='task'"
            " AND valid_until=0")
        assert len(rows) == 1 and '"doing"' in rows[0][0]
    finally:
        store2.close()
