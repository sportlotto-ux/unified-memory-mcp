"""P2.3: mem_validate — read-only collation сигналов проверки (red-first).

Срез: цель + cite (если claim) + conflicts (цель + прямые соседи) +
живые supports/contradicts + annotations. Вердикта нет, needs_judgment всегда.
"""

import json

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "val.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = Config(db_path=tmp_path / "val.db")
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "server.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as module
    monkeypatch.setattr(module, "_backend", lambda cfg: None)
    module._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield module
    if module._STATE.get("store") is not None:
        module._STATE["store"].close()
    module._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)


def test_empty_signals(srv):
    fid = json.loads(srv.mem_fact("long", "capital", "Paris is capital"))["id"]
    out = json.loads(srv.mem_validate(f"fact:{fid}"))
    assert out["found"] is True
    assert out["cite"] is None
    assert out["conflicts"]["count"] == 0
    assert out["links"] == [] and out["annotations"] == []
    assert out["needs_judgment"] is True
    assert "verdict" not in out


def test_cite_supported_when_claim_matches(srv):
    fid = json.loads(srv.mem_fact("long", "capital", "Paris is capital"))["id"]
    out = json.loads(srv.mem_validate(f"fact:{fid}", claim="Paris is capital"))
    assert out["cite"]["verdict"] == "supported"


def test_links_and_annotations_visible(srv):
    a = json.loads(srv.mem_fact("long", "capital", "Paris is capital"))["id"]
    b = json.loads(srv.mem_fact("long", "other", "Berlin is capital"))["id"]
    json.loads(srv.mem_link(f"fact:{a}", f"fact:{b}", "contradicts"))
    json.loads(srv.mem_annotate(f"fact:{a}", "disputed", "check this"))
    out = json.loads(srv.mem_validate(f"fact:{a}"))
    assert {"src": f"fact:{a}", "dst": f"fact:{b}",
            "rel": "contradicts"} == {
        k: out["links"][0][k] for k in ("src", "dst", "rel")}
    assert [x["kind"] for x in out["annotations"]] == ["disputed"]


def test_negation_conflict_via_neighbour(ing):
    a = ing.remember_fact("long", "capital", "Paris is capital")
    b = ing.remember_fact("long", "rival", "not Paris is capital")
    ing.store.link("um_facts", a, "um_facts", b, "supports", 1.0, "", "")
    from unified_memory import evidence as ev
    out = ev.run_validate(ing.store, f"fact:{a}")
    negs = [c for c in out["conflicts"]["candidates"]
            if c["reason_code"] == "negation"]
    assert len(negs) == 1
    assert out["needs_judgment"] is True


def test_owner_isolation_and_bad_ref(srv):
    fid = json.loads(
        srv.mem_fact("long", "capital", "Paris", owner="alice"))["id"]
    foreign = json.loads(srv.mem_validate(f"fact:{fid}", owner="bob"))
    assert foreign["found"] is False
    assert foreign["needs_judgment"] is True
    legacy = json.loads(srv.mem_validate(f"fact:{fid}"))
    assert legacy["found"] is True
    with pytest.raises(ValueError):
        srv.mem_validate("bogus:1")


def test_readonly_invariance(ing):
    fid = ing.remember_fact("long", "capital", "Paris is capital")
    before = [h.owner_id for h in ing.router().recall("capital", scope="facts")]
    asm_before = ing.window.assemble("s1")
    from unified_memory import evidence as ev
    first = ev.run_validate(ing.store, f"fact:{fid}", claim="Paris")
    second = ev.run_validate(ing.store, f"fact:{fid}", claim="Paris")
    assert first == second
    after = [h.owner_id for h in ing.router().recall("capital", scope="facts")]
    assert before == after
    assert asm_before == ing.window.assemble("s1")
