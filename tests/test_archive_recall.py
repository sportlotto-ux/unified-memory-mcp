import json

import pytest

from unified_memory import archive
from unified_memory.config import Config
from unified_memory.recall import Router
from unified_memory.store import Store


def _config(tmp_path, **kwargs):
    return Config(
        db_path=tmp_path / "hot.db",
        archive_path=tmp_path / "cold.db",
        context_tokens=10**9,
        **kwargs,
    )


def _archive_one(tmp_path, *, content="cold phrase", session_id="s1",
                 owner="alice", source="mcp"):
    cfg = _config(tmp_path)
    store = Store(cfg)
    mid = store.add_message(session_id, "user", content, source=source, owner=owner)
    conn = archive.open_archive(cfg.archive_path)
    try:
        archive.move_oldest(store, conn, limit=1, label=str(cfg.archive_path))
    finally:
        conn.close()
    return cfg, store, mid


def test_archive_search_is_bounded_and_scope_aware(tmp_path):
    cfg, store, mid = _archive_one(
        tmp_path, content="needle in cold storage", session_id="wanted",
        owner="alice", source="mcp")
    conn = archive.open_archive_readonly(cfg.archive_path)
    try:
        rows = archive.search_messages(
            conn, "needle", scope="session", session_id="wanted",
            owner="alice", source="mcp", max_scan=10,
            label=str(cfg.archive_path))
        assert [row["id"] for row in rows] == [mid]
        assert rows[0]["archive_ref"] == f"{cfg.archive_path}#{mid}"
        assert archive.search_messages(
            conn, "needle", scope="session", session_id="other",
            owner="alice", max_scan=10) == []
        assert archive.search_messages(
            conn, "needle", scope="facts", owner="alice", max_scan=10) == []
        assert archive.search_messages(
            conn, "needle", scope="session", session_id="wanted",
            owner="alice", source="other", max_scan=10) == []
    finally:
        conn.close()
        store.close()


def test_archive_recall_cap_limits_scanned_rows(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    first = store.add_message("s", "user", "needle one", source="mcp")
    second = store.add_message("s", "user", "needle two", source="mcp")
    conn = archive.open_archive(cfg.archive_path)
    try:
        archive.move_oldest(store, conn, limit=2, label=str(cfg.archive_path))
    finally:
        conn.close()
    conn = archive.open_archive_readonly(cfg.archive_path)
    try:
        rows = archive.search_messages(conn, "needle", max_scan=1)
        assert len(rows) == 1
        assert rows[0]["id"] in {first, second}
    finally:
        conn.close()
        store.close()


def test_include_archived_does_not_create_missing_archive(tmp_path):
    cfg = _config(tmp_path)
    store = Store(cfg)
    try:
        router = Router(store, cfg=cfg)
        assert router.recall("cold phrase", include_archived=True) == []
        assert not cfg.archive_path.exists()
    finally:
        store.close()


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "srv.db"))
    monkeypatch.setenv("UM_ARCHIVE_PATH", str(tmp_path / "srv.archive.db"))
    import unified_memory.server as server
    monkeypatch.setattr(server, "_backend", lambda cfg: None)
    server._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield server
    if server._STATE.get("store") is not None:
        server._STATE["store"].close()
    server._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)


def test_mem_recall_archive_flag_preserves_filters_and_exact_recovery(srv):
    message = json.loads(srv.mem_remember(
        session_id="s1", role="user", content="архивная фраза",
        owner="alice"))
    store = srv._STATE["store"]
    cfg = srv._STATE["cfg"]
    conn = archive.open_archive(cfg.archive_path)
    try:
        archive.move_oldest(store, conn, limit=1, label=str(cfg.archive_path))
    finally:
        conn.close()

    assert json.loads(srv.mem_recall(query="архивная фраза", scope="session",
                                     session_id="s1", owner="alice")) == []
    out = json.loads(srv.mem_recall(
        query="архивная фраза", scope="session", session_id="s1",
        owner="alice", source="mcp", include_archived=True,
        diagnostics=True))
    assert out["diagnostics"]["arms"]["archive"] == 1
    assert out["hits"][0]["id"] == message["id"]
    assert out["hits"][0]["archived"] is True
    assert out["hits"][0]["archive_ref"].endswith(f"#{message['id']}")
    assert out["hits"][0]["body"] == "архивная фраза"

    assert json.loads(srv.mem_recall(
        query="архивная фраза", scope="session", session_id="s1",
        owner="bob", include_archived=True)) == []
    assert json.loads(srv.mem_recall(
        query="архивная фраза", scope="session", session_id="s1",
        owner="alice", source="other", include_archived=True)) == []
    assert json.loads(srv.mem_recall(
        query="архивная фраза", scope="facts", include_archived=True)) == []

    expanded = json.loads(srv.mem_expand("message", message["id"], owner="alice"))
    assert expanded["body"] == "архивная фраза"
    assert expanded["archived"] is True


def test_archive_recall_scan_limit_is_validated(monkeypatch, tmp_path):
    monkeypatch.setenv("UM_ARCHIVE_RECALL_SCAN_LIMIT", "0")
    with pytest.raises(ValueError, match="UM_ARCHIVE_RECALL_SCAN_LIMIT"):
        _config(tmp_path)
