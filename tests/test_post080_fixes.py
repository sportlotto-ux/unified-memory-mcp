"""Пост-0.8.0 хотфиксы: vecidx+dim в импорте, kind-тайбрейкер recent,
read_locked сканеров, audit с ?/# в пути."""

import base64
import json

import pytest

from unified_memory import archive
from unified_memory.config import Config
from unified_memory.export import export_store
from unified_memory.import_dump import import_dump
from unified_memory.store import Store, pack_vector, vec_extension_available


def _mkstore(d, name="t"):
    return Store(Config(db_path=d / f"{name}.db", archive_path=d / f"{name}.arch"))


def _b64(vec):
    return base64.b64encode(pack_vector(vec)).decode()


def test_import_rebuilds_vecidx_and_knn_finds(tmp_path):
    if not vec_extension_available():
        pytest.skip("needs local-vec extra")
    src = _mkstore(tmp_path, "s")
    mid = src.conn.execute(
        "INSERT INTO um_messages(session_id,owner,role,content,created_at,source)"
        " VALUES('s','','user','hello',100.0,'t')").lastrowid
    src.conn.execute(
        "INSERT INTO um_vectors(owner_table,owner_id,embedding,model,owner)"
        " VALUES('um_messages',?,?,?,?)", (mid, pack_vector([1.0, 0.0, 0.0]),
                                            "test", ""))
    src.conn.commit()
    dump = export_store(src, tmp_path / "d.jsonl")["path"]
    src.close()
    dst = _mkstore(tmp_path, "d")
    try:
        rep = import_dump(dst, dump)
        assert rep["um_vectors"]["inserted"] == 1
        assert rep["vec_index"] == 1  # индекс пересобран импортом, не reindex
        new_mid = dst.conn.execute(
            "SELECT owner_id FROM um_vectors").fetchone()[0]
        hits = dst.knn([1.0, 0.0, 0.0], ["um_messages"], k=5)
        assert hits is not None and any(h[1] == new_mid for h in hits)
    finally:
        dst.close()


def _hand_dump(path, vec_rows):
    lines = [{"format": "um-export-jsonl", "schema_version": "1"}]
    lines.append({"table": "um_messages", "row": {
        "id": 1, "session_id": "s", "owner": "", "role": "user",
        "content": "m", "created_at": 100.0, "source": "t",
        "externalized_ref": None}})
    for i, emb in enumerate(vec_rows, start=1):
        lines.append({"table": "um_vectors", "row": {
            "id": i, "owner_table": "um_messages", "owner_id": 1,
            "embedding": emb, "model": "test", "owner": ""}})
    path.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")


def test_import_dim_mismatch_counted_not_silent(tmp_path):
    p = tmp_path / "mix.jsonl"
    _hand_dump(p, [_b64([1.0, 0.0, 0.0]),   # dim 3 — эталон (первый валидный)
                   _b64([1.0, 0.0]),        # dim 2 — чужая
                   "!!!not-base64!!!"])     # битый blob
    st = _mkstore(tmp_path)
    try:
        rep = import_dump(st, p)
        assert rep["um_vectors"]["inserted"] == 1
        assert rep["um_vectors"]["dim_mismatch"] == 2
        n = st.conn.execute("SELECT count(*) FROM um_vectors").fetchone()[0]
        assert n == 1  # чужая dim не вставлена — recall бы её молча не нашёл
    finally:
        st.close()


def test_recent_kind_tiebreaker_no_loss(tmp_path):
    st = _mkstore(tmp_path)
    try:
        mid = st.conn.execute(
            "INSERT INTO um_messages(session_id,owner,role,content,created_at,source)"
            " VALUES('s','','user','tie-m',500.0,'t')").lastrowid
        sid = st.conn.execute(
            "INSERT INTO um_summaries(session_id,owner,depth,body,covers_from,"
            " covers_to,superseded_by,created_at)"
            " VALUES('s','',0,'tie-s',0,0,0,500.0)").lastrowid
        assert mid == sid == 1  # полный (created_at, id)-тий между таблицами
        st.conn.commit()
        p1 = st.recent(0, 10**9, limit=1)
        assert len(p1) == 1
        p2 = st.recent(0, 10**9, limit=1, before_ts=p1[0]["created_at"],
                       before_id=p1[0]["id"], before_kind=p1[0]["kind"])
        assert len(p2) == 1 and p2[0]["kind"] != p1[0]["kind"]  # близнец не потерян
        p3 = st.recent(0, 10**9, limit=1, before_ts=p2[0]["created_at"],
                       before_id=p2[0]["id"], before_kind=p2[0]["kind"])
        assert p3 == []
        # legacy-вызов без kind — старое поведение: вся (ts,id)-группа исключена
        assert st.recent(0, 10**9, limit=1, before_ts=500.0, before_id=1) == []
    finally:
        st.close()


def test_read_locked_reentrant_and_export(tmp_path):
    st = _mkstore(tmp_path)
    try:
        st.conn.execute(
            "INSERT INTO um_messages(session_id,owner,role,content,created_at,source)"
            " VALUES('s','','user','m',100.0,'t')")
        st.conn.commit()
        with st.read_locked() as conn:
            assert conn is st.conn
            assert st.meta_get("schema_version") is not None  # locked внутри — ок
            rep = export_store(st, tmp_path / "e.jsonl")      # экспорт внутри — ок
        assert (tmp_path / "e.jsonl").exists() and rep["counts"]["um_messages"] == 1
    finally:
        st.close()


def test_audit_special_chars_path(tmp_path):
    weird = tmp_path / "a?b#c" / "arch.db"
    conn = archive.open_archive(weird)
    conn.execute(
        "INSERT INTO ar_messages(id,session_id,owner,role,content,created_at,source)"
        " VALUES(7,'s','','user','x',1.0,'t')")
    conn.commit()
    conn.close()
    st = _mkstore(tmp_path)
    try:
        out = archive.audit(st, weird)
        assert out["file_exists"] is True
        assert out["archived_rows"] == 1  # без quote open падал → ловился → 0
    finally:
        st.close()
