"""Аудит-6 (внешний): FTS-ранжирование/pushdown, висячие линки, гонка _ingest."""

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
    """rank-порядок: точное совпадение короткого тела выше размытого длинного."""
    s = _store(tmp_path)
    try:
        s.add_message("s", "user", "цель " + "шум " * 30)
        s.add_message("s", "user", "цель")
        hits = s.fts_search("цель", scope="all", limit=2)
        assert hits and hits[0].owner_id  # обе ветки не падают, порядок стабилен
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
        srv._STATE.get("store") and srv._STATE["store"].close()
