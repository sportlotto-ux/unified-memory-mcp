"""v0.8 B6: импорт дампа — round-trip, legacy, валидация, аддитивность, owner."""

import base64
import json

import pytest
from fake_backend import FakeBackend

from unified_memory.config import Config
from unified_memory.export import COMPLETE_FORMAT, export_store
from unified_memory.import_dump import import_dump, read_dump
from unified_memory.ingest import Ingest
from unified_memory.store import Store


def _cfg(tmp_path, name):
    return Config(db_path=tmp_path / name, archive_path=tmp_path / (name + ".arch"),
                  context_tokens=10**9)


def _source(tmp_path):
    cfg = _cfg(tmp_path, "src.db")
    st = Store(cfg)
    ing = Ingest(st, FakeBackend(), cfg=cfg)
    ing.remember_message("s1", "user", "привет мир")
    ing.remember_message("s1", "assistant", "как дела")
    ing.remember_fact("t", "node", "тело узла", subject="иван", predicate="любит",
                      obj="чай")
    fid = ing.remember_fact("t", "plain", "просто факт")
    fid2 = ing.remember_fact("t", "other", "другой факт")
    st.link("um_facts", fid, "um_facts", fid2, "supports", weight=2.5)
    st.add_summary("s1", "сводка диалога", 0)
    ing.update_fact(fid2, valid_until=1.0)   # истёкшая версия → история как данные
    return st


def _counts(st):
    return {t: st.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("um_facts", "um_messages", "um_summaries", "um_entities",
                      "um_edges", "um_links", "um_vectors")}


def _fresh(tmp_path, name="dst.db"):
    return Store(_cfg(tmp_path, name))


def _header(sv="1"):
    return {"format": "um-export-jsonl", "schema_version": sv, "exported_at": 0,
            "counts": {}, "complete": True}


def _write(tmp_path, name, header, rows):
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write(json.dumps({"format": COMPLETE_FORMAT}) + "\n")
    return p


def _fact_row(i, name="n", body="b", owner="", **kw):
    row = {"id": i, "owner": owner, "category": "c", "name": name, "body": body,
           "importance": 0.5, "created_at": 10.0, "updated_at": 10.0,
           "valid_until": 0.0, "superseded_by": 0}
    row.update(kw)
    return {"table": "um_facts", "row": row}


# ---------- round-trip ----------

def test_roundtrip_all_tables(tmp_path):
    src = _source(tmp_path)
    try:
        before = _counts(src)
        dump = tmp_path / "d.jsonl"
        export_store(src, dump)
        dst = _fresh(tmp_path)
        try:
            rep = import_dump(dst, dump)
            assert rep["schema_version"] == "1" and rep["dry_run"] is False
            assert _counts(dst) == before, (rep, _counts(dst), before)
            # FTS пересобран → поиск работает без backend
            got = dst.fts_search("иван", scope="all")
            assert any("иван" in (h.body or "") for h in got)
            # вектора из дампа (base64), без пересчёта
            assert dst.conn.execute(
                "SELECT count(*) FROM um_vectors").fetchone()[0] >= 1
            # links ремапнуты на НОВЫЕ id и связывают живые факты
            st, did = dst.conn.execute(
                "SELECT src_id, dst_id FROM um_links").fetchone()
            kinds = {r[0] for r in dst.conn.execute(
                "SELECT valid_until FROM um_facts WHERE id IN (?,?)", (st, did))}
            assert kinds  # оба конца существуют
            # истёкшая версия перенесена как данные
            assert dst.conn.execute(
                "SELECT count(*) FROM um_facts WHERE valid_until>0").fetchone()[0] == 1
        finally:
            dst.close()
    finally:
        src.close()


def test_import_works_without_backend(tmp_path):
    src = _source(tmp_path)
    try:
        dump = tmp_path / "d.jsonl"
        export_store(src, dump)
        dst = _fresh(tmp_path)
        try:
            import_dump(dst, dump)          # dst без backend — это фича
            assert dst.conn.execute(
                "SELECT count(*) FROM um_vectors").fetchone()[0] >= 1
        finally:
            dst.close()
    finally:
        src.close()


def test_import_invalidates_existing_pressure_counters(tmp_path):
    from unified_memory.engine import ActiveWindow

    src = _source(tmp_path)
    dst = _fresh(tmp_path, "existing.db")
    try:
        dump = tmp_path / "existing.jsonl"
        export_store(src, dump)
        cfg = _cfg(tmp_path, "existing.db")
        dst.add_message("s1", "user", "existing message")
        ActiveWindow(dst, None, cfg).pressure("s1")
        assert dst.meta_get("raw_tokens:s1") is not None
        import_dump(dst, dump)
        assert dst.meta_get("raw_tokens:s1") is None
    finally:
        dst.close()
        src.close()


# ---------- legacy single-JSON ----------

def test_legacy_single_json_read(tmp_path):
    legacy = {"schema_version": "1", "exported_at": 1, "tables": {
        "um_facts": [{"id": 7, "owner": "", "category": "c", "name": "старый",
                      "body": "легаси тело", "importance": 0.5, "created_at": 1.0,
                      "updated_at": 1.0, "valid_until": 0.0, "superseded_by": 0}],
        "um_fts": [{"id": 1, "owner_table": "um_facts", "owner_id": 7,
                    "body": "старый легаси тело"}],   # производная — игнорируется
    }}
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps(legacy), encoding="utf-8")
    sv, tables = read_dump(p)
    assert sv == "1" and len(tables["um_facts"]) == 1
    dst = _fresh(tmp_path)
    try:
        import_dump(dst, p)
        assert dst.conn.execute("SELECT body FROM um_facts").fetchone()[0] == "легаси тело"
        assert dst.fts_search("легаси", scope="facts")  # пересобрано, не из дампа
    finally:
        dst.close()


# ---------- валидация ----------

def test_unknown_table_is_loud(tmp_path):
    p = _write(tmp_path, "x.jsonl", _header(), [{"table": "um_secret", "row": {}}])
    dst = _fresh(tmp_path)
    try:
        with pytest.raises(ValueError, match="unknown table"):
            import_dump(dst, p)
    finally:
        dst.close()


def test_newer_schema_version_rejected(tmp_path):
    p = _write(tmp_path, "x.jsonl", _header("2"), [_fact_row(1)])
    dst = _fresh(tmp_path)
    try:
        with pytest.raises(ValueError, match="schema_version"):
            import_dump(dst, p)
    finally:
        dst.close()


def test_incomplete_jsonl_rejected_before_import(tmp_path):
    header = _header()
    header["counts"] = {"um_facts": 2}
    p = tmp_path / "partial.jsonl"
    p.write_text(
        json.dumps(header) + "\n" + json.dumps(_fact_row(1)) + "\n",
        encoding="utf-8",
    )
    dst = _fresh(tmp_path)
    try:
        with pytest.raises(ValueError, match="incomplete"):
            import_dump(dst, p)
        assert dst.conn.execute("SELECT count(*) FROM um_facts").fetchone()[0] == 0
    finally:
        dst.close()


# ---------- без redaction (плейсхолдеры байт-в-байт) ----------

def test_no_clean_placeholders_preserved(tmp_path):
    body = "ключ: [REDACTED:api_key] и password=secret123"
    p = _write(tmp_path, "x.jsonl", _header(), [_fact_row(1, name="k", body=body)])
    dst = _fresh(tmp_path)
    try:
        import_dump(dst, p)
        assert dst.conn.execute("SELECT body FROM um_facts").fetchone()[0] == body
    finally:
        dst.close()


# ---------- owner override ----------

def test_owner_override(tmp_path):
    p = _write(tmp_path, "x.jsonl", _header(), [_fact_row(1, owner="alice")])
    dst = _fresh(tmp_path)
    try:
        import_dump(dst, p, owner="bob")
        assert dst.conn.execute("SELECT owner FROM um_facts").fetchone()[0] == "bob"
    finally:
        dst.close()


# ---------- аддитивность + skip слота ----------

def test_additive_and_slot_conflict_skipped(tmp_path):
    dst = _fresh(tmp_path)
    try:
        ing = Ingest(dst, None, cfg=_cfg(tmp_path, "dst.db"))
        ing.remember_fact("c", "n", "существующее")
        p = _write(tmp_path, "x.jsonl", _header(),
                   [_fact_row(1, name="n", body="импортируемое"),      # конфликт слота
                    _fact_row(2, name="fresh", body="новое")])          # вставится
        rep = import_dump(dst, p)
        assert rep["um_facts"] == {"inserted": 1, "skipped": 1}
        bodies = {r[0] for r in dst.conn.execute("SELECT body FROM um_facts")}
        assert bodies == {"существующее", "новое"}   # существующее не перезаписано
    finally:
        dst.close()


def test_dry_run_changes_nothing(tmp_path):
    dst = _fresh(tmp_path)
    try:
        p = _write(tmp_path, "x.jsonl", _header(), [_fact_row(1)])
        rep = import_dump(dst, p, dry_run=True)
        assert rep["um_facts"]["inserted"] == 1     # отчёт есть
        assert dst.conn.execute("SELECT count(*) FROM um_facts").fetchone()[0] == 0
    finally:
        dst.close()
