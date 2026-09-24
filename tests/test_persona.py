"""P2.5: persona поверх um_facts category="persona" (red-first).

Без новых таблиц: трейт — слот (owner, "persona", trait); get — весь живой
профиль одним bounded-вызовом.
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
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "persona.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = Config(db_path=tmp_path / "persona.db")
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


def test_set_and_get_roundtrip(srv):
    first = json.loads(srv.mem_persona("set", trait="tone", body="concise"))
    assert first["status"] == "created"
    out = json.loads(srv.mem_persona("get"))
    assert out["traits"] == {"tone": "concise"} and out["count"] == 1
    second = json.loads(srv.mem_persona("set", trait="tone", body="warm"))
    assert second["status"] == "superseded"
    assert json.loads(srv.mem_persona("get"))["traits"] == {"tone": "warm"}
    third = json.loads(srv.mem_persona("set", trait="tone", body="warm"))
    assert third["status"] == "noop" and third["id"] == second["id"]


def test_rejects(srv):
    with pytest.raises(ValueError):
        srv.mem_persona("set", trait="", body="x")
    with pytest.raises(ValueError):
        srv.mem_persona("set", trait="tone", body="   ")
    with pytest.raises(ValueError):
        srv.mem_persona("drop")


def test_owner_isolation_and_legacy(ing):
    ing.persona_set("tone", "concise", owner="alice")
    assert ing.persona_profile(owner="bob") == {}
    assert ing.persona_profile() == {"tone": "concise"}
    assert ing.persona_profile(owner="alice") == {"tone": "concise"}


def test_persona_body_is_redacted(ing):
    ing.persona_set("secret-note", "api_key = SECRET1234567890")
    body = ing.persona_profile()["secret-note"]
    assert "SECRET1234567890" not in body
    assert "[UM redaction:" in body


def test_export_import_keeps_persona(ing, tmp_path):
    ing.persona_set("tone", "concise")
    ing.persona_set("lang", "russian")
    from unified_memory import export as exp
    from unified_memory import import_dump as imp
    dump = tmp_path / "persona.jsonl"
    exp.export_store(ing.store, dump)
    cfg2 = Config(db_path=tmp_path / "restore.db")
    store2 = Store(cfg2)
    try:
        imp.import_dump(store2, dump)
        rows = dict(store2.select(
            "SELECT name, body FROM um_facts WHERE category='persona'"
            " AND valid_until=0"))
        assert rows == {"tone": "concise", "lang": "russian"}
    finally:
        store2.close()


def test_get_is_bounded_and_sorted(ing):
    for i in range(5):
        ing.persona_set(f"trait-{4 - i}", f"value-{i}")
    got = ing.persona_profile(limit=3)
    assert list(got) == ["trait-0", "trait-1", "trait-2"]
    assert len(ing.persona_profile()) == 5
