"""v0.8 C13: batch-op 'remember' (сообщение) — атомарность и успех."""

from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store


def _rig(tmp_path):
    cfg = Config(db_path=tmp_path / "b.db", archive_path=tmp_path / "b.arch",
                 context_tokens=10**9)
    st = Store(cfg)
    return st, Ingest(st, None, cfg=cfg)


def _n(st, table):
    return st.select(f"SELECT count(*) FROM {table}")[0][0]


def test_batch_remember_atomic_rollback(tmp_path):
    st, ing = _rig(tmp_path)
    try:
        ops = [{"op": "remember", "session_id": "s", "role": "user",
                "content": f"msg {i}"} for i in range(5)]
        ops.append({"op": "bogus"})
        out = ing.batch(ops)
        assert out["ok"] is False and out["error"] is not None
        assert _n(st, "um_messages") == 0    # плохой op обнулил всю сессию
    finally:
        st.close()


def test_batch_remember_commit(tmp_path):
    st, ing = _rig(tmp_path)
    try:
        out = ing.batch([
            {"op": "remember", "session_id": "s", "role": "user", "content": "раз"},
            {"op": "remember", "session_id": "s", "role": "assistant", "content": "два"},
            {"op": "remember_fact", "category": "c", "name": "n", "body": "тело"},
        ])
        assert out["ok"] is True and out["applied"] is True
        assert _n(st, "um_messages") == 2 and _n(st, "um_facts") == 1
    finally:
        st.close()
