"""Пункт 0 v0.5 на уровне сервера: mem_update, include_expired, expand-даты, parse_when."""

from datetime import datetime, timezone

import pytest

from unified_memory.recent import parse_when

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def test_parse_when():
    assert parse_when("") is None
    assert parse_when("   ") is None
    assert parse_when("open") == 0.0
    assert parse_when("0") == 0.0
    assert parse_when("now", NOW) == NOW.timestamp()
    assert parse_when("2026-01-15", NOW) == \
        datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
    assert parse_when("1700000000", NOW) == 1700000000.0
    with pytest.raises(ValueError, match="valid_until"):
        parse_when("завтра", NOW)
    with pytest.raises(ValueError, match="must be >= 0"):
        parse_when("-5", NOW)  # молчаливо-истёкшее запрещено


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "srv.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda cfg: None)  # FTS-only: без загрузки модели
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield m
    if m._STATE.get("store") is not None:
        m._STATE["store"].close()
    m._STATE.update(ingest=None, store=None, cfg=None)


def test_update_creates_version_and_history(srv):
    import json
    fid = json.loads(srv.mem_fact("preference", "чай", "зелёный"))["id"]
    out = json.loads(srv.mem_update(kind="fact", id=fid, body="чёрный"))
    assert out["status"] == "superseded" and out["id"] != fid
    live = json.loads(srv.mem_recall(query="чай", scope="facts"))
    assert [h["body"] for h in live] == ["чай: чёрный"]
    hist = json.loads(srv.mem_recall(query="чай", scope="facts", include_expired=True))
    assert {h["body"] for h in hist} == {"чай: чёрный", "чай: зелёный"}
    # expand отдаёт версионные поля
    ex = json.loads(srv.mem_expand(kind="fact", id=fid))
    assert ex["valid_until"] > 0 and ex["superseded_by"] == out["id"]


def test_update_expire_and_reopen(srv):
    import json
    fid = json.loads(srv.mem_fact("preference", "чай", "зелёный"))["id"]
    assert json.loads(srv.mem_update(kind="fact", id=fid,
                                     valid_until="now"))["status"] == "expired"
    assert json.loads(srv.mem_recall(query="чай", scope="facts")) == []
    assert json.loads(srv.mem_update(kind="fact", id=fid,
                                     valid_until="open"))["status"] == "reopened"
    assert len(json.loads(srv.mem_recall(query="чай", scope="facts"))) == 1


def test_update_owner_guard_server(srv):
    import json
    fid = json.loads(srv.mem_fact("preference", "чай", "зелёный", owner="alice"))["id"]
    with pytest.raises(Exception):
        srv.mem_update(kind="fact", id=fid, body="чёрный", owner="bob")


def test_update_edge_expire_server(srv):
    import json
    srv.mem_fact("p", "x", "y", subject="Иван", predicate="любит", object="чай")
    edges = [h for h in json.loads(srv.mem_recall(query="Иван"))
             if h["kind"] == "um_edges"]
    assert edges, "ребро не нашлось"
    out = json.loads(srv.mem_update(kind="edge", id=edges[0]["id"], valid_until="now"))
    assert out["updated"] is True and out["status"] == "expired"
    after = json.loads(srv.mem_recall(query="Иван"))
    assert not [h for h in after if h["kind"] == "um_edges"], after
    hist = json.loads(srv.mem_recall(query="Иван", include_expired=True))
    assert [h for h in hist if h["kind"] == "um_edges"], "история не отдалась"


def test_update_bad_kind_and_edge_without_when(srv):
    with pytest.raises(Exception):
        srv.mem_update(kind="message", id=1)
    with pytest.raises(Exception):
        srv.mem_update(kind="edge", id=1, valid_until="")
