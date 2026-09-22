"""Аудит-6 (внешний): FTS-ранжирование/pushdown, висячие линки, гонка _ingest."""

import json
import threading

import pytest

from unified_memory.config import Config
from unified_memory.store import Store


def _store(tmp_path):
    return Store(Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                        context_tokens=10**9))


# ---------- FTS: ORDER BY rank + pushdown owner_table ----------

def test_fts_scope_pushdown_not_starved(tmp_path):
    """10 сообщений с термом не должны вытеснить единственный факт по лимиту."""
    s = _store(tmp_path)
    try:
        for i in range(10):
            s.add_message("s", "user", f"отчёт по проекту номер {i}")
        fid = s.add_fact("c", "n", "отчёт по бюджету")
        hits = s.fts_search("отчёт", scope="facts", limit=1)
        assert ("um_facts", fid) in {(h.owner_table, h.owner_id) for h in hits}
    finally:
        s.close()


def test_fts_returns_relevance_order(tmp_path):
    """rank-порядок: короткое точное тело должно обойти длинное размытое."""
    s = _store(tmp_path)
    try:
        noisy = s.add_message("s", "user", "цель " + "шум " * 30)
        exact = s.add_message("s", "user", "цель")
        hits = s.fts_search("цель", scope="all", limit=2)
        assert {h.owner_id for h in hits} >= {exact, noisy}
        assert hits[0].owner_id == exact  # bm25 length-norm: короче → лучше
    finally:
        s.close()


# ---------- Висячие um_links: отчёт hygiene + чистка repair ----------

def test_dangling_links_reported_and_repaired(tmp_path):
    s = _store(tmp_path)
    try:
        a = s.add_fact("c", "a", "тело a")
        b = s.add_fact("c", "b", "тело b")
        s.link("um_facts", a, "um_facts", b, "supports")
        assert s.hygiene()["dangling_links"] == []

        s.delete_fact(b)  # каскад рёбер/векторов факта b, но линк остаётся
        assert s.hygiene()["dangling_links"]  # мусор виден доктору

        rep = s.repair(dim=0)
        assert rep["purged_links"] >= 1
        assert s.hygiene()["dangling_links"] == []
    finally:
        s.close()


# ---------- Гонка ленивой _ingest() ----------

def test_lazy_ingest_initialized_once(tmp_path, monkeypatch):
    import unified_memory.server as srv

    calls = {"load": 0, "backend": 0}

    class FakeBackend:
        dim = 256

    def fake_load():
        calls["load"] += 1
        return Config(db_path=tmp_path / "r.db", context_tokens=10**9)

    def fake_backend(_cfg):
        calls["backend"] += 1
        return FakeBackend()  # warm уже подменён целиком

    monkeypatch.setattr(srv, "load", fake_load)
    monkeypatch.setattr(srv, "_backend", fake_backend)
    monkeypatch.setattr(srv, "_maybe_maintenance", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_STATE",
                        {"ingest": None, "store": None, "cfg": None,
                         "backend_error": None})
    got: list = []

    def worker():
        got.append(srv._ingest())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert calls["load"] == 1 and calls["backend"] == 1
        assert len({id(g) for g in got}) == 1
    finally:
        if srv._STATE.get("store") is not None:
            srv._STATE["store"].close()


# ---------- v0.7.3 B10: кап входного текста (один гейт в _clean) ----------

def test_max_text_chars_gate(tmp_path):
    from unified_memory.ingest import Ingest

    cfg = Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                 context_tokens=10**9, max_text_chars=10)
    st = Store(cfg)
    ing = Ingest(st, None, None, cfg)
    try:
        with pytest.raises(ValueError, match="UM_MAX_TEXT_CHARS"):
            ing.remember_message("s", "user", "x" * 11)
        r = ing.remember_message("s", "user", "x" * 10)  # граница — ок
        assert r["id"]
    finally:
        st.close()


def test_max_text_chars_rejects_bad_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UM_MAX_TEXT_CHARS", "0")
    with pytest.raises(ValueError, match="UM_MAX_TEXT_CHARS"):
        Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db")


# ---------- v0.7.3 B9: WAL-витрина + checkpoint в maintenance ----------

@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "srv.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda cfg: None)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield m
    if m._STATE.get("store") is not None:
        m._STATE["store"].close()
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)


def test_status_wal_and_maintenance_never_raises(srv):
    srv.mem_remember(session_id="s", role="user", content="привет")
    st = json.loads(srv.mem_status())
    assert isinstance(st["wal_bytes"], int) and st["wal_bytes"] >= 0
    # maintenance с checkpoint(TRUNCATE) не должен бросать
    srv._maybe_maintenance(srv._STATE["store"], srv._STATE["cfg"])


# ---------- v0.7.3 B7: archive_check (read-only) ----------

def test_archive_check_reports_and_orphans(srv):
    import sqlite3

    import unified_memory.archive as arc
    srv.mem_remember(session_id="s", role="user", content="уедет в архив")
    st, cfg = srv._STATE["store"], srv._STATE["cfg"]
    conn = arc.open_archive(cfg.archive_path)
    arc.move_oldest(st, conn, 10)
    conn.close()
    out = json.loads(srv.mem_doctor(mode="archive_check"))
    assert out["file_exists"] and out["stubs"] == 1 and out["archived_rows"] == 1
    assert out["orphans"] == 0

    c = sqlite3.connect(str(cfg.archive_path))
    c.execute("DELETE FROM ar_messages")
    c.commit()
    c.close()
    out2 = json.loads(srv.mem_doctor(mode="archive_check"))
    assert out2["orphans"] == 1  # заглушка без строки в архиве


# ---------- v0.7.3 B8: secret_scan без утечки значений ----------

def test_secret_scan_reports_without_values(srv):
    fid = json.loads(srv.mem_fact("c", "n", "обычное тело"))["id"]
    secret = "sk-" + "A" * 20
    st = srv._STATE["store"]
    st.conn.execute("UPDATE um_facts SET body=? WHERE id=?",
                    (f"api_key={secret}", fid))
    st.conn.commit()
    raw = srv.mem_doctor(mode="secret_scan")
    out = json.loads(raw)
    assert out["total"] >= 1
    assert any(h["pattern"] == "api_key" and h["kind"] == "um_facts"
               and h["id"] == fid for h in out["hits"])
    assert secret not in raw  # отчёт не должен содержать значение


# ---------- v0.7.3 A5: детект near-дублей фактов (read-only, owner-scoped) ----------

def test_hygiene_detects_duplicate_facts_owner_scoped(tmp_path):
    s = _store(tmp_path)
    try:
        a = s.add_fact("c", "a", "тело")
        b = s.add_fact("c", "b", "тело2")
        v = [1.0, 0.0, 0.0, 0.0]
        s.add_vector("um_facts", a, list(v), "m")
        s.add_vector("um_facts", b, list(v), "m")
        c = s.add_fact("c", "c", "иное", owner="bob")
        s.add_vector("um_facts", c, list(v), "m", owner="bob")
        pairs = s.hygiene()["duplicate_facts"]
        assert any({p[1], p[2]} == {a, b} for p in pairs)
        assert all(c not in (p[1], p[2]) for p in pairs)  # owner-изоляция
    finally:
        s.close()


def test_hygiene_no_vectors_no_duplicate_scan(tmp_path):
    s = _store(tmp_path)
    try:
        s.add_fact("c", "a", "тело")
        assert s.hygiene()["duplicate_facts"] == []
    finally:
        s.close()
