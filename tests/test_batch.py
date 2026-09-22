"""v0.7 п.5b: mem_batch — atomic all-or-nothing, dry-run, капы, redaction-гейт."""

import json

import pytest


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda c: None)  # FTS-only
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield m
    if m._STATE.get("store") is not None:
        m._STATE["store"].close()
    m._STATE.update(ingest=None, store=None, cfg=None)


def _counts(m):
    m._ingest()
    st = m._STATE["store"]
    return {t: st.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("um_facts", "um_edges", "um_links", "um_messages")}


def test_batch_happy_mixed_ops(srv):
    a = json.loads(srv.mem_fact("pref", "a", "первый"))["id"]
    b = json.loads(srv.mem_fact("pref", "b", "второй"))["id"]
    out = json.loads(srv.mem_batch([
        {"op": "remember_fact", "category": "pref", "name": "c", "body": "третий"},
        {"op": "update", "kind": "fact", "id": a, "body": "первый v2"},
        {"op": "forget", "kind": "fact", "id": b},
    ]))
    assert out["ok"] is True and out["applied"] is True and out["error"] is None
    assert [r["op"] for r in out["results"]] == ["remember_fact", "update", "forget"]
    st = srv._STATE["store"]
    live = {r[0] for r in st.conn.execute(
        "SELECT name FROM um_facts WHERE valid_until=0")}
    assert {"a", "c"} <= live and "b" not in live  # b удалён жёстко


def test_batch_rollback_all_or_nothing(srv):
    srv.mem_fact("pref", "a", "первый")
    before = _counts(srv)
    out = json.loads(srv.mem_batch([
        {"op": "remember_fact", "category": "pref", "name": "b", "body": "второй"},
        {"op": "update", "kind": "fact", "id": 999999, "body": "нет такого"},
    ]))
    assert out["ok"] is False and out["applied"] is False
    assert out["error"]["index"] == 1  # упал второй op
    assert _counts(srv) == before      # ничего не записалось


def test_batch_dry_run_writes_nothing(srv):
    before = _counts(srv)
    out = json.loads(srv.mem_batch([
        {"op": "remember_fact", "category": "pref", "name": "x", "body": "y"},
        {"op": "remember_fact", "category": "pref", "name": "z", "body": "w"},
    ], dry_run=True))
    assert out["ok"] is True and out["applied"] is False
    assert len(out["results"]) == 2
    assert _counts(srv) == before


def test_batch_dry_run_reports_bad_op(srv):
    before = _counts(srv)
    out = json.loads(srv.mem_batch([
        {"op": "remember_fact", "category": "pref", "name": "x", "body": "y"},
        {"op": "nonsense"},
    ], dry_run=True))
    assert out["ok"] is False and out["error"]["index"] == 1
    assert _counts(srv) == before


def test_batch_update_and_forget_link(srv):
    a = json.loads(srv.mem_fact("pref", "a", "первый"))["id"]
    b = json.loads(srv.mem_fact("pref", "b", "второй"))["id"]
    lid = json.loads(srv.mem_link(f"fact:{a}", f"fact:{b}", "supports"))["id"]
    out = json.loads(srv.mem_batch([
        {"op": "update", "kind": "link", "id": lid, "valid_until": "now"},
        {"op": "forget", "kind": "link", "id": lid},
    ]))
    assert out["applied"] is True
    assert out["results"][0]["status"] == "expired"
    assert out["results"][1]["deleted"] is True


def test_batch_redaction_gate(srv):
    out = json.loads(srv.mem_batch([
        {"op": "remember_fact", "category": "sec", "name": "k",
         "body": "api_key=ABCDEFGHIJKLMNOP1234"},
    ]))
    assert out["applied"] is True
    body = srv._STATE["store"].conn.execute(
        "SELECT body FROM um_facts WHERE name='k'").fetchone()[0]
    assert "[UM redaction:" in body  # батч идёт через _clean, не в обход


def test_batch_max_ops_cap_before_transaction(srv, monkeypatch):
    monkeypatch.setenv("UM_BATCH_MAX_OPS", "2")
    ops = [{"op": "remember_fact", "category": "c", "name": f"n{i}", "body": "b"}
           for i in range(3)]
    with pytest.raises(ValueError, match="UM_BATCH_MAX_OPS"):
        srv.mem_batch(ops)
    assert srv._STATE["store"].conn.execute(
        "SELECT count(*) FROM um_facts").fetchone()[0] == 0  # контур не открывался


def test_batch_max_chars_cap(srv, monkeypatch):
    monkeypatch.setenv("UM_BATCH_MAX_CHARS", "50")
    with pytest.raises(ValueError, match="UM_BATCH_MAX_CHARS"):
        srv.mem_batch([{"op": "remember_fact", "category": "c", "name": "n",
                        "body": "x" * 200}])


def test_batch_empty_rejected(srv):
    with pytest.raises(ValueError, match="non-empty"):
        srv.mem_batch([])
