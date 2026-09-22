"""v0.7 п.4: BFS recall по um_links ∪ um_edges (guardrails D5/ADR-001).

hops=1 обязан идти старым _graph_arm (регресс by construction); BFS — hops>1.
"""

import json
import time

import pytest

from unified_memory.config import Config
from unified_memory.recall import Router
from unified_memory.store import Store


def _cfg(tmp_path, **kw):
    return Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                  context_tokens=10**9, **kw)


@pytest.fixture
def rig(tmp_path):
    cfg = _cfg(tmp_path)
    st = Store(cfg)
    r = Router(st, backend=None, cfg=cfg)
    yield st, r
    st.close()


def _fact(st, cat, name, body, owner=""):
    return st.add_fact(cat, name, body, owner=owner)


def _ids(hits):
    return {(h.owner_table, h.owner_id) for h in hits}


# ---------- guardrail 1: hops=1 — старый путь, линки не читаются ----------

def test_hops1_ignores_links(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко")
    b = _fact(st, "fruit", "банан", "жёлтый банан")
    st.link("um_facts", a, "um_facts", b, "derives_from")
    assert ("um_facts", b) not in _ids(r.recall("яблоко", scope="facts", hops=1))
    assert ("um_facts", b) in _ids(r.recall("яблоко", scope="facts", hops=2))


# ---------- guardrail 3: decay ----------

def test_decay_by_depth(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко")
    b = _fact(st, "fruit", "банан", "жёлтый банан")
    c = _fact(st, "fruit", "киви", "зелёный киви")
    st.link("um_facts", a, "um_facts", b, "derives_from")
    st.link("um_facts", b, "um_facts", c, "derives_from")
    hits = r._graph_bfs("яблоко", "facts", "", 20, "", False, None, 3, "",
                        [("um_facts", a)])
    d = {(h.owner_table, h.owner_id): h.score for h in hits}
    assert d[("um_facts", b)] == pytest.approx(1.0)                  # 1 линк = depth 1
    assert d[("um_facts", c)] == pytest.approx(r.cfg.graph_decay)    # 2 линка = 0.5


# ---------- guardrail 4: просрочка в обе стороны ----------

def test_live_link_to_expired_fact_hidden(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко")
    b = _fact(st, "fruit", "банан", "жёлтый банан")
    st.link("um_facts", a, "um_facts", b, "supports")
    st.update_fact(b, valid_until=time.time())  # факт истёк, линк жив
    assert ("um_facts", b) not in _ids(r.recall("яблоко", scope="facts", hops=2))


def test_expired_link_to_live_fact_hidden(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко")
    b = _fact(st, "fruit", "банан", "жёлтый банан")
    lid = st.link("um_facts", a, "um_facts", b, "supports")["id"]
    st.update_link(lid, time.time())  # линк истёк, факт жив
    assert ("um_facts", b) not in _ids(r.recall("яблоко", scope="facts", hops=2))


def test_include_expired_shows_linked_expired(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко")
    b = _fact(st, "fruit", "банан", "жёлтый банан")
    st.link("um_facts", a, "um_facts", b, "supports")
    st.update_fact(b, valid_until=time.time())
    got = _ids(r.recall("яблоко", scope="facts", hops=2, include_expired=True))
    assert ("um_facts", b) in got


def test_as_of_cuts_through_link(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко")
    b = _fact(st, "fruit", "банан", "жёлтый банан")
    st.link("um_facts", a, "um_facts", b, "derives_from")
    now = time.time()
    assert st.link_neighbors("um_facts", a, as_of=now - 10) == []  # линк ещё не создан
    assert st.link_neighbors("um_facts", a, as_of=now + 10)


# ---------- owner / scope / rel ----------

def test_owner_isolation(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко", owner="tenant-a")
    b = _fact(st, "fruit", "банан", "жёлтый банан", owner="tenant-a")
    st.link("um_facts", a, "um_facts", b, "supports", owner="tenant-a")
    assert ("um_facts", b) in _ids(
        r.recall("яблоко", scope="facts", hops=2, owner="tenant-a"))
    assert _ids(r.recall("яблоко", scope="facts", hops=2, owner="tenant-b")) == set()


def test_rel_filter_and_case(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко")
    b = _fact(st, "fruit", "банан", "жёлтый банан")
    st.link("um_facts", a, "um_facts", b, "supports")
    assert ("um_facts", b) not in _ids(
        r.recall("яблоко", scope="facts", hops=2, rel="contradicts"))
    assert ("um_facts", b) in _ids(
        r.recall("яблоко", scope="facts", hops=2, rel="supports"))
    assert ("um_facts", b) in _ids(
        r.recall("яблоко", scope="facts", hops=2, rel="SUPPORTS"))  # регистр


# ---------- guardrail 5: identity (table,id), циклы, fan-out ----------

def test_node_identity_table_id(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко")
    m = st.add_message("s1", "user", "заметка про яблоко")
    st.link("um_facts", a, "um_messages", m, "supports")
    hits = r._graph_bfs("яблоко", "all", "", 20, "", False, None, 2, "",
                        [("um_facts", a)])
    assert ("um_messages", m) in _ids(hits)


def test_cycle_terminates(rig):
    st, r = rig
    a = _fact(st, "fruit", "яблоко", "красное яблоко")
    b = _fact(st, "fruit", "банан", "жёлтый банан")
    st.link("um_facts", a, "um_facts", b, "derives_from")
    st.link("um_facts", b, "um_facts", a, "derives_from")  # A→B→A
    keys = [(h.owner_table, h.owner_id) for h in
            r._graph_bfs("яблоко", "facts", "", 50, "", False, None, 3, "",
                         [("um_facts", a)])]
    assert keys.count(("um_facts", b)) == 1


def test_fanout_cap(tmp_path):
    cfg = _cfg(tmp_path, link_fanout=3)
    st = Store(cfg)
    try:
        r = Router(st, backend=None, cfg=cfg)
        a = _fact(st, "t", "a", "старт")
        for i in range(6):
            b = _fact(st, "t", f"b{i}", f"тело {i}")
            st.link("um_facts", a, "um_facts", b, "derives_from")
        hits = r._graph_bfs("a", "facts", "", 50, "", False, None, 2, "",
                            [("um_facts", a)])
        assert len(hits) <= 3
    finally:
        st.close()


# ---------- server wiring + потолок hops ----------

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
    m._STATE.update(ingest=None, store=None, cfg=None)


def test_server_hops_wiring_and_cap(srv):
    a = json.loads(srv.mem_fact("fruit", "яблоко", "красное яблоко"))["id"]
    b = json.loads(srv.mem_fact("fruit", "банан", "жёлтый банан"))["id"]
    srv.mem_link(f"fact:{a}", f"fact:{b}", "derives_from")
    out = json.loads(srv.mem_recall(query="яблоко", scope="facts", hops=2))
    assert any(x["id"] == b for x in out)
    with pytest.raises(ValueError, match="UM_RECALL_MAX_HOPS"):
        srv.mem_recall(query="яблоко", hops=99)
    with pytest.raises(ValueError, match="hops must be"):
        srv.mem_recall(query="яблоко", hops=0)
