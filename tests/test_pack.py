"""P2.9: mem_evidence(mode=pack) — multi-ref collation, verdict-free (red-first).

Пак: per-ref cite/links/annotations (состав validate без вложенных conflicts)
+ один глобальный conflicts по явным refs. Без автопоиска, без вердикта.
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
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "pack.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = Config(db_path=tmp_path / "pack.db")
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


def test_empty_pack(srv):
    out = json.loads(srv.mem_evidence(mode="pack", refs=[]))
    assert out["mode"] == "pack"
    assert out["count"] == 0 and out["items"] == []
    assert out["conflicts"]["count"] == 0
    assert out["needs_judgment"] is True
    assert "verdict" not in out


def test_per_ref_cite_and_global_conflicts_empty(srv):
    a = json.loads(srv.mem_fact("long", "cap-a", "Paris is capital"))["id"]
    b = json.loads(srv.mem_fact("long", "cap-b",
                                "Quantum tunneling calibrates sensors"))["id"]
    out = json.loads(srv.mem_evidence(mode="pack", claim="Paris is capital",
                                      refs=[f"fact:{a}", f"fact:{b}"]))
    assert out["count"] == 2
    by_id = {i["id"]: i for i in out["items"]}
    assert by_id[a]["found"] is True
    assert by_id[a]["cite"]["verdict"] == "supported"
    assert by_id[b]["cite"]["verdict"] == "unsupported"
    assert out["conflicts"]["count"] == 0
    assert out["needs_judgment"] is True


def test_slot_versions_candidate(ing):
    from unified_memory import evidence as ev
    a = ing.remember_fact("long", "capital", "Paris is capital")
    b = ing.remember_fact("long", "capital", "Berlin is capital")
    assert a != b  # supersede создал вторую живую версию слота... или цепочку
    out = ev.run_pack(ing.store, "", [f"fact:{a}", f"fact:{b}"])
    slots = [c for c in out["conflicts"]["candidates"]
             if c["reason_code"] == "slot_versions"]
    assert len(slots) == 1
    assert out["needs_judgment"] is True


def test_owner_bank_isolation_and_legacy(srv):
    fid = json.loads(srv.mem_fact("long", "secret", "family recipe",
                                  owner="alice", bank="family"))["id"]
    foreign = json.loads(srv.mem_evidence(mode="pack", refs=[f"fact:{fid}"],
                                          owner="bob"))
    assert foreign["items"][0]["found"] is False
    codes = [r["reason_code"] for r in foreign["conflicts"]["rejections"]]
    assert "bank_hidden" in codes
    legacy = json.loads(srv.mem_evidence(mode="pack", refs=[f"fact:{fid}"]))
    assert legacy["items"][0]["found"] is True


def test_budget_and_bad_ref_no_throw(ing):
    from unified_memory import evidence as ev
    a = ing.remember_fact("long", "n1", "alpha body")
    b = ing.remember_fact("long", "n2", "beta body")
    c = ing.remember_fact("long", "n3", "gamma body")
    out = ev.run_pack(ing.store, "", [f"fact:{a}", f"fact:{b}", f"fact:{c}",
                                      "bogus:1"], max_refs=2)
    assert len(out["items"]) == 2
    codes = [r["reason_code"] for r in out["conflicts"]["rejections"]]
    assert "budget" in codes and "bad_ref" in codes


def test_readonly_and_server_passthrough(ing, srv):
    fid = json.loads(srv.mem_fact("long", "capital", "Paris is capital"))["id"]
    before = [h.owner_id for h in
              ing.router().recall("capital", scope="facts")]
    from unified_memory import evidence as ev
    first = ev.run_pack(ing.store, "Paris", [f"fact:{fid}"])
    second = ev.run_pack(ing.store, "Paris", [f"fact:{fid}"])
    assert first == second
    after = [h.owner_id for h in
             ing.router().recall("capital", scope="facts")]
    assert before == after
    out = json.loads(srv.mem_evidence(mode="pack", refs=[f"fact:{fid}"]))
    assert out["mode"] == "pack" and out["count"] == 1
    with pytest.raises(ValueError):
        srv.mem_evidence(mode="nope", refs=[f"fact:{fid}"])
