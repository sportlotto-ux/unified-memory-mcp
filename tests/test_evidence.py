"""v0.6 — mem_evidence: cite (проверка опоры) и compute (агрегация над refs).

Контракт: без LLM. Тул проверяет/агрегирует ТОЛЬКО те refs, что передал
вызывающий. NL-интент парсит хост-агент.
"""

import json
import time

import pytest

from unified_memory import archive
from unified_memory.config import Config
from unified_memory.evidence import (coverage, parse_ref, run_cite,
                                     run_compute, run_conflicts)
from unified_memory.store import Store

DAY = 86400


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                  context_tokens=10**9)


@pytest.fixture
def store(cfg):
    s = Store(cfg)
    yield s
    s.close()


# ---------- helpers ----------

def test_parse_ref_aliases_and_errors():
    assert parse_ref("fact:3") == ("um_facts", 3)
    assert parse_ref("um_facts:3") == ("um_facts", 3)
    assert parse_ref("message:12") == ("um_messages", 12)
    assert parse_ref("edge:4") == ("um_edges", 4)
    with pytest.raises(ValueError):
        parse_ref("nonsense")
    with pytest.raises(ValueError):
        parse_ref("fact:notanum")


def test_coverage_verbatim_and_partial():
    assert coverage("горячий чай", "я люблю горячий чай каждый день")[1] is True
    cov, verb = coverage("горячий суп", "горячий чай и молоко")
    assert verb is False and 0.0 < cov < 1.0


def test_coverage_short_claim_not_trivial_substring():
    # «да» не должно матчиться внутри «удар»
    cov, verb = coverage("да", "удар по мячу")
    assert verb is False and cov == 0.0


# ---------- cite ----------

def test_cite_supported_verbatim(store):
    fid = store.add_fact("city", "capital", "Москва — столица России")
    out = run_cite(store, "Москва — столица России", [f"fact:{fid}"])
    assert out["verdict"] == "supported"
    assert out["refs"][0]["verbatim"] is True


def test_cite_partial(store):
    fid = store.add_fact("city", "capital", "Москва стоит на реке Москве")
    out = run_cite(store, "Москва река Волга", [f"fact:{fid}"])
    assert out["verdict"] == "partial"
    assert 0.0 < out["refs"][0]["coverage"] < 1.0


def test_cite_unsupported(store):
    fid = store.add_fact("city", "capital", "Москва — столица России")
    out = run_cite(store, "Лондон крупный порт", [f"fact:{fid}"])
    assert out["verdict"] == "unsupported"


def test_cite_ref_not_found(store):
    out = run_cite(store, "что-то", ["fact:99999"])
    assert out["verdict"] == "unsupported"
    assert out["rejections"][0]["reason_code"] == "not_found"


def test_cite_owner_mismatch(store):
    fid = store.add_fact("city", "capital", "Москва — столица России", owner="alice")
    out = run_cite(store, "Москва — столица России", [f"fact:{fid}"], owner="bob")
    assert out["verdict"] == "unsupported"
    assert out["rejections"][0]["reason_code"] == "owner_mismatch"
    # верный owner — supported
    ok = run_cite(store, "Москва — столица России", [f"fact:{fid}"], owner="alice")
    assert ok["verdict"] == "supported"


def test_cite_max_refs_budget(store):
    ids = [store.add_fact("c", f"n{i}", f"тело {i} про город") for i in range(5)]
    refs = [f"fact:{i}" for i in ids]
    out = run_cite(store, "про город", refs, max_refs=2)
    assert len(out["refs"]) == 2
    assert any(r["reason_code"] == "budget" for r in out["rejections"])


def test_cite_bad_ref_is_rejection(store):
    fid = store.add_fact("c", "n", "тело")
    out = run_cite(store, "тело", [f"fact:{fid}", "мусор"])
    assert any(r["reason_code"] == "bad_ref" for r in out["rejections"])
    assert out["verdict"] == "supported"  # валидный ref спас


def test_cite_archived_resolves_via_callback(cfg, store):
    mid = store.add_message("s", "user", "бюджет проекта 500 тысяч", "mcp")
    conn = archive.open_archive(cfg.archive_path)
    archive.move_oldest(store, conn, limit=1, label=str(cfg.archive_path))

    def fetch(table, oid):
        if table == "um_messages":
            got = archive.fetch_message(conn, oid)
            return got["content"] if got else None
        return None

    try:
        out = run_cite(store, "бюджет проекта 500 тысяч",
                       [f"message:{mid}"], archived_fetch=fetch)
        assert out["verdict"] == "supported"
        assert out["refs"][0]["archived"] is True
        # без callback архивный ref неразрешим
        out2 = run_cite(store, "бюджет проекта 500 тысяч", [f"message:{mid}"])
        assert out2["rejections"][0]["reason_code"] == "archived"
    finally:
        conn.close()


def test_cite_empty_claim(store):
    fid = store.add_fact("c", "n", "тело")
    out = run_cite(store, "", [f"fact:{fid}"])
    assert out["verdict"] == "unsupported"


# ---------- compute ----------

def test_compute_sum(store):
    a = store.add_fact("budget", "a", "потратили 1000 руб")
    b = store.add_fact("budget", "b", "потратили 2500 руб")
    out = run_compute(store, [f"fact:{a}", f"fact:{b}"], op="sum")
    assert out["result"] == 3500.0 and out["n"] == 2
    assert out["verdict"] == "supported"


def test_compute_min_max(store):
    a = store.add_fact("m", "a", "значение 3.5")
    b = store.add_fact("m", "b", "значение 10")
    assert run_compute(store, [f"fact:{a}", f"fact:{b}"], op="min")["result"] == 3.5
    assert run_compute(store, [f"fact:{a}", f"fact:{b}"], op="max")["result"] == 10.0


def test_compute_count_refs(store):
    a = store.add_fact("m", "a", "нет чисел тут")
    b = store.add_fact("m", "b", "тоже нет")
    out = run_compute(store, [f"fact:{a}", f"fact:{b}"], op="count")
    assert out["result"] == 2  # count = число разрешённых refs
    assert out["n"] == 0      # чисел нет, но count валиден


def test_compute_pattern(store):
    a = store.add_fact("m", "a", "цена: 1500 руб, скидка 200")
    out = run_compute(store, [f"fact:{a}"], op="sum", pattern=r"(\d+)\s*руб")
    assert out["result"] == 1500.0


def test_compute_no_numbers_unsupported(store):
    a = store.add_fact("m", "a", "никаких цифр")
    out = run_compute(store, [f"fact:{a}"], op="sum")
    assert out["verdict"] == "unsupported"
    assert out["rejections"][0]["reason_code"] == "no_numbers"


def test_compute_only_caller_refs(store):
    store.add_fact("m", "hidden", "секретное число 9999")
    a = store.add_fact("m", "a", "число 1")
    out = run_compute(store, [f"fact:{a}"], op="sum")
    assert out["result"] == 1.0  # 9999 не подмешалось (нет авто-поиска)


def test_compute_owner_isolation(store):
    a = store.add_fact("m", "a", "число 5", owner="alice")
    out = run_compute(store, [f"fact:{a}"], op="sum", owner="bob")
    assert out["verdict"] == "unsupported"
    assert out["rejections"][0]["reason_code"] == "owner_mismatch"


# ---------- conflicts (verdict-free) ----------

def test_conflicts_slot_versions(store):
    old = store.add_fact("city", "capital", "Москва")
    new = store.update_fact(old, body="Санкт-Петербург")["id"]
    out = run_conflicts(store, [f"fact:{old}", f"fact:{new}"])
    assert out["needs_judgment"] is True
    assert out["count"] == 1
    cand = out["candidates"][0]
    assert cand["reason_code"] == "slot_versions"
    assert cand["slot"]["name"] == "capital"


def test_conflicts_negation(store):
    a = store.add_fact("c", "a", "Москва столица")
    b = store.add_fact("c", "b", "не Москва столица")
    out = run_conflicts(store, [f"fact:{a}", f"fact:{b}"])
    assert any(c["reason_code"] == "negation" for c in out["candidates"])


def test_conflicts_no_false_positive(store):
    a = store.add_fact("c", "a", "Москва")
    b = store.add_fact("c", "b", "Московская область")
    out = run_conflicts(store, [f"fact:{a}", f"fact:{b}"])
    assert out["count"] == 0


def test_conflicts_only_caller_refs(store):
    a = store.add_fact("c", "a", "Москва")
    store.add_fact("c", "a2", "не Москва")  # скрытый оппонент, не передан
    out = run_conflicts(store, [f"fact:{a}"])
    assert out["count"] == 0


def test_conflicts_records_rejections(store):
    out = run_conflicts(store, ["мусор", "fact:99999"])
    assert {r["reason_code"] for r in out["rejections"]} == {"bad_ref", "not_found"}
    assert out["count"] == 0


def test_compute_bad_pattern_is_clean_error(store):
    a = store.add_fact("m", "a", "число 1")
    with pytest.raises(ValueError, match="bad pattern"):
        run_compute(store, [f"fact:{a}"], op="sum", pattern="(")
    with pytest.raises(ValueError, match="unknown op"):
        run_compute(store, [f"fact:{a}"], op="nope")


# ---------- config + server wiring ----------

def test_evidence_config_defaults_and_validation(tmp_path):
    c = Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db")
    assert c.evidence_max_refs == 50
    assert c.evidence_max_chars == 8000
    assert c.evidence_partial == 0.5
    with pytest.raises(ValueError, match="UM_EVIDENCE"):
        Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
               evidence_partial=0.0)


def test_server_mem_evidence_wiring(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("UM_ARCHIVE_PATH", str(tmp_path / "s.archive.db"))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda c: None)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    try:
        fid = json.loads(m.mem_fact(category="city", name="capital",
                                    body="Москва — столица России"))["id"]
        out = json.loads(m.mem_evidence(
            claim="Москва — столица России", refs=[f"fact:{fid}"]))
        assert out["verdict"] == "supported"
        # compute через тул
        json.loads(m.mem_fact(category="budget", name="a", body="потратили 1000"))
        bids = [fid]
        nums = json.loads(m.mem_evidence(mode="compute", refs=[f"fact:{bids[0]}"],
                                         op="count"))
        assert nums["result"] == 1
        new_id = json.loads(m.mem_update(kind="fact", id=fid,
                                         body="Санкт-Петербург"))["id"]
        conf = json.loads(m.mem_evidence(mode="conflicts",
                                         refs=[f"fact:{fid}", f"fact:{new_id}"]))
        assert conf["needs_judgment"] is True and conf["count"] == 1
        with pytest.raises(ValueError):
            m.mem_evidence(mode="nope", refs=[f"fact:{fid}"])
    finally:
        if m._STATE.get("store") is not None:
            m._STATE["store"].close()
        m._STATE.update(ingest=None, store=None, cfg=None)
