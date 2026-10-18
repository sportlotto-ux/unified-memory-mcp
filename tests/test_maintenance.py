"""Этап 0: один проход обслуживания стора (вектора, индекс, гигиена, архив, WAL).

Проверяем ровно то, ради чего сервис и нужен: добивание пропущенных векторов,
ремонт гигиены (backup-first), вынос старого в архив, и что падение эмбеддера
деградирует, но не отменяет остальные шаги. Никаких мутаций в dry-run.
"""

import json

import pytest

from fake_backend import FakeBackend
from unified_memory import maintenance
from unified_memory.config import Config
from unified_memory.embeddings import EmbedServerError
from unified_memory.ingest import Ingest
from unified_memory.store import Store, vec_extension_available


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Изолированный стор на фейковом бэкенде: без сети и загрузки модели."""
    monkeypatch.setattr(maintenance, "make_backend", lambda c: FakeBackend())
    return Config(db_path=tmp_path / "m.db",
                  archive_path=tmp_path / "m.archive.db",
                  context_tokens=10**9)


def _seed(cfg, n=3):
    "N сообщений с векторами. Стор закрыт — run() открывает свой."
    store = Store(cfg)
    ing = Ingest(store, FakeBackend(), cfg=cfg)
    ids = [ing.remember_message("s", "user", f"запись номер {i}") for i in range(n)]
    return store, ids


def _count(cfg, sql="SELECT count(*) FROM um_vectors"):
    store = Store(cfg)
    try:
        return store.select(sql)[0][0]
    finally:
        store.close()


def test_dry_run_reports_gap_without_mutation(cfg):
    store, _ = _seed(cfg)
    store.conn.execute("DELETE FROM um_vectors")
    store.conn.commit()
    store.close()

    rep = maintenance.run(cfg, apply=False)

    assert rep["status"] == "ok"
    assert rep["steps"]["vectors"]["missing"] == 3
    assert rep["steps"]["vectors"]["would_embed"] == 3
    assert rep["steps"]["hygiene"] == {"status": "clean"}
    assert rep["steps"]["wal"] == {"skipped": "dry-run"}
    assert _count(cfg) == 0  # ни одного вектора не записано


def test_fills_missing_vectors_and_writes_report(cfg):
    store, _ = _seed(cfg)
    store.conn.execute("DELETE FROM um_vectors")
    store.conn.commit()
    store.close()
    report_path = cfg.db_path.parent / "rep.json"

    rep = maintenance.run(cfg, report_path=report_path)

    assert rep["status"] == "ok"
    assert rep["steps"]["vectors"]["reindex"]["embedded"] == 3
    assert _count(cfg) == 3
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "ok"


def test_skips_reindex_when_nothing_missing(cfg):
    if not vec_extension_available():
        pytest.skip("sqlite-vec unavailable")
    store, _ = _seed(cfg)
    store.close()

    first = maintenance.run(cfg)  # индекса ещё нет → он его построит
    assert first["steps"]["vectors"]["missing"] == 0
    assert first["steps"]["vectors"]["reindex"]["vec_index"] == 3

    second = maintenance.run(cfg)  # пропусков нет и индекс готов → вхолостую не молотим
    v = second["steps"]["vectors"]
    assert v["missing"] == 0 and "reindex" not in v
    assert v["skipped"] == "no missing vectors, index ready"


def test_hygiene_repair_backup_first(cfg):
    store, _ = _seed(cfg)
    store.conn.execute(
        "INSERT INTO um_vectors(owner_table, owner_id, embedding, model, owner)"
        " VALUES('um_messages', 999, X'0000', 'fake/test-16', '')")
    store.conn.commit()
    store.close()

    rep = maintenance.run(cfg)

    hyg = rep["steps"]["hygiene"]
    assert hyg["candidates"]["orphan_vectors"] == [["um_messages", 999]]
    assert hyg["repair"]["purged_vectors"] == 1
    assert hyg["candidates_after"] == {}
    assert rep["status"] == "ok"


def test_dry_run_does_not_repair(cfg):
    store, _ = _seed(cfg)
    store.conn.execute(
        "INSERT INTO um_vectors(owner_table, owner_id, embedding, model, owner)"
        " VALUES('um_messages', 999, X'0000', 'fake/test-16', '')")
    store.conn.commit()
    store.close()

    rep = maintenance.run(cfg, apply=False)

    assert rep["steps"]["hygiene"]["apply_required"] is True
    assert _count(cfg) == 4  # сирота на месте


def test_model_mismatch_is_loud_and_skips_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "make_backend", lambda c: FakeBackend())
    cfg = Config(db_path=tmp_path / "m.db", archive_path=tmp_path / "m.archive.db",
                 context_tokens=10**9)
    store = Store(cfg)
    ing = Ingest(store, FakeBackend(), cfg=cfg)
    ing.remember_message("s", "user", "живая запись")
    # вектор «чужой» модели без пары — имитируем смену модели без reembed
    store.conn.execute("UPDATE um_vectors SET model='other/model-9', embedding=X'0000'")
    store.conn.commit()
    store.close()

    rep = maintenance.run(cfg)

    assert rep["status"] == "degraded"
    assert rep["steps"]["vectors"]["model_mismatch"] is True
    assert any("reembed" in e for e in rep["errors"])
    # чужую модель не переписали
    assert _count(cfg, "SELECT count(*) FROM um_vectors WHERE model='other/model-9'") == 1


def test_embedder_down_degrades_but_other_steps_run(tmp_path, monkeypatch):
    class Dead(FakeBackend):
        def warm(self):
            raise EmbedServerError("embedding server unreachable")

    monkeypatch.setattr(maintenance, "make_backend", lambda c: Dead())
    cfg = Config(db_path=tmp_path / "m.db", archive_path=tmp_path / "m.archive.db",
                 embedding_backend="openai",
                 embedding_base_url="http://127.0.0.1:9",
                 context_tokens=10**9)
    store = Store(cfg)
    Ingest(store, Dead(), cfg=cfg).remember_message("s", "user", "запись")
    store.close()

    rep = maintenance.run(cfg)

    assert rep["status"] == "degraded"
    assert rep["steps"]["vectors"]["embed_backend"] == "unavailable"
    assert rep["steps"]["hygiene"] == {"status": "clean"}
    assert "wal" in rep["steps"] and "archive" in rep["steps"]


def test_archive_moves_old_messages_by_age(cfg):
    store, _ = _seed(cfg, 3)
    store.conn.execute("UPDATE um_messages SET created_at = created_at - 30*86400")
    store.conn.commit()
    store.close()
    aged = Config(db_path=cfg.db_path, archive_path=cfg.archive_path,
                  retention_days=7, context_tokens=10**9)

    rep = maintenance.run(aged)

    a = rep["steps"]["archive"]
    assert a["due_age"] is True and a["moved_age"] == 3
    store = Store(aged)
    try:
        assert store.meta_get("retention_last_run") is not None
    finally:
        store.close()


def test_archive_not_due_when_small_and_retention_off(cfg):
    store, _ = _seed(cfg)
    store.close()

    rep = maintenance.run(cfg)

    a = rep["steps"]["archive"]
    assert a["retention_days"] == 0 and a["due_age"] is False
    assert a["due_size"] is False
    assert a["skipped"] == "nothing due"


def test_embedding_jobs_of_edges_and_entities_are_covered(cfg):
    """Пропуски у фактов/рёбер/сущностей тоже добиваются, не только сообщения."""
    store = Store(cfg)
    ing = Ingest(store, FakeBackend(), cfg=cfg)
    ing.remember_fact("t", "сервер", "продовый сервер на vps4",
                      subject="mediabot", predicate="живёт_на", obj="vps4")
    store.conn.execute("DELETE FROM um_vectors WHERE owner_table='um_entities'")
    store.conn.commit()
    store.close()

    rep = maintenance.run(cfg)

    assert rep["steps"]["vectors"]["missing"] >= 1
    assert rep["steps"]["vectors"]["reindex"]["embedded"] >= 1
