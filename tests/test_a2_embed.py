"""v0.8 A2: вектора на update/reopen на уровне Ingest.

Store.update_fact не знает о backend. Ingest-обёртка после supersede (новый id)
и reopen (id тот же, вектор удалён P4.8 при expire) переэмбеддивает факт —
иначе он слеп для вектор-плеча до reindex.
"""

import json
import time

from fake_backend import FakeBackend

from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store


def _rig(tmp_path, backend=True):
    cfg = Config(db_path=tmp_path / "a2.db", archive_path=tmp_path / "a2-arch.db",
                 context_tokens=10**9)
    st = Store(cfg)
    ing = Ingest(st, FakeBackend() if backend else None, cfg=cfg)
    return st, ing


def _nvec(st, fid):
    return st.select(
        "SELECT count(*) FROM um_vectors WHERE owner_table='um_facts' AND owner_id=?",
        (fid,))[0][0]


def test_update_body_embeds_new_version(tmp_path):
    st, ing = _rig(tmp_path)
    try:
        fid = ing.remember_fact("c", "n", "старое тело")
        assert _nvec(st, fid) == 1
        out = ing.update_fact(fid, body="новое тело")
        assert out["status"] == "superseded"
        assert _nvec(st, out["id"]) == 1     # новая версия видима вектор-плечу
        assert _nvec(st, fid) == 0           # вектор старой снят (P4.8)
    finally:
        st.close()


def test_reopen_reembeds(tmp_path):
    st, ing = _rig(tmp_path)
    try:
        fid = ing.remember_fact("c", "n", "тело")
        ing.update_fact(fid, valid_until=time.time() - 1)   # expire → P4.8 убирает вектор
        assert _nvec(st, fid) == 0
        out = ing.update_fact(fid, valid_until=0.0)         # reopen
        assert out["status"] == "reopened"
        assert _nvec(st, fid) == 1           # reopen не оставляет слепым
    finally:
        st.close()


def test_no_backend_update_is_noop(tmp_path):
    st, ing = _rig(tmp_path, backend=False)
    try:
        fid = ing.remember_fact("c", "n", "старое")
        out = ing.update_fact(fid, body="новое")
        assert out["status"] == "superseded"
        assert _nvec(st, fid) == 0 and _nvec(st, out["id"]) == 0
    finally:
        st.close()


def test_batch_update_rollback_drops_new_vector(tmp_path):
    st, ing = _rig(tmp_path)
    try:
        fid = ing.remember_fact("c", "n", "старое")
        before = st.select("SELECT count(*) FROM um_vectors")[0][0]
        out = ing.batch([{"op": "update", "kind": "fact", "id": fid, "body": "новое"},
                         {"op": "bogus"}])
        assert out["ok"] is False and out["error"] is not None
        assert st.select("SELECT count(*) FROM um_vectors")[0][0] == before  # откат унёс и вектор
        body = st.select("SELECT body FROM um_facts WHERE valid_until=0")[0][0]
        assert body == "старое"              # живая версия не изменилась
    finally:
        st.close()


def test_server_update_embeds(tmp_path, monkeypatch):
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda cfg: FakeBackend())
    monkeypatch.setattr(m, "_maybe_maintenance", lambda *a, **k: None)
    cfg = Config(db_path=tmp_path / "srv.db", archive_path=tmp_path / "srv-arch.db",
                 context_tokens=10**9)
    monkeypatch.setattr(m, "load", lambda: cfg)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    try:
        fid = json.loads(m.mem_fact("c", "n", "старое"))["id"]
        out = json.loads(m.mem_update(kind="fact", id=fid, body="новое"))
        assert out["status"] == "superseded"
        st = m._STATE["store"]
        assert _nvec(st, out["id"]) == 1
    finally:
        if m._STATE.get("store") is not None:
            m._STATE["store"].close()
        m._STATE.update(ingest=None, store=None, cfg=None)
