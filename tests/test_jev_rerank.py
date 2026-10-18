"""Приёмка реранкера recall: судейство Jev поверх широкого пула.

Замер 18.10.2026 (19 реальных запросов, пул 25, scope=all): hit@1 79% → 89%,
hit@3 95% → 100%; на 5 вопросах, ответов на которые в памяти нет, все кандидаты
оказались ниже порога 0.5 (топ-оценка 0.03–0.33 против 0.84–0.97 там, где ответ
есть), а need_wider_search — 0.65–0.88.

Тесты фиксируют свойства: порядок по оценке, отсечение по порогу, редакция и
обрезка перед отправкой, громкий fail-open. Ни один тест в сеть не ходит —
транспорт инъектируется.
"""

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.recall import Router
from unified_memory.redact import PLACEHOLDER_PREFIX
from unified_memory.rerank import rerank
from unified_memory.store import Store

CFG = {"url": "http://rerank.invalid", "key": "public", "model": "m",
       "threshold": 0.5, "timeout": 1.0, "doc_chars": 40}


def transport(values, widen: float = 0.0, capture: list | None = None):
    """Транспорт с заданными оценками по индексам кандидатов."""
    def _t(url, key, model, state, questions, timeout):
        if capture is not None:
            capture.append({"state": state, "questions": questions,
                            "url": url, "key": key})
        answers = {f"rel_{i}": {"noul": v} for i, v in enumerate(values)}
        answers["need_wider_search"] = {"noul": widen}
        return {"answers": answers, "usage": {"input_tokens": 11}}
    return _t


def transport_by_key(score_by_key: dict, default: float = 0.1, widen: float = 0.1):
    """Транспорт, который читает id кандидатов прямо из state."""
    def _t(url, key, model, state, questions, timeout):
        answers = {}
        for line in state.splitlines():
            if line.startswith("--- ["):
                idx, _, rest = line[5:].partition("] ")
                cid = rest.split(" ---")[0]
                answers[f"rel_{idx}"] = {"noul": score_by_key.get(cid, default)}
        answers["need_wider_search"] = {"noul": widen}
        return {"answers": answers}
    return _t


def docs(n: int):
    return [{"key": f"um_facts:{i}", "text": f"текст кандидата {i}"} for i in range(n)]


def test_orders_by_score_and_drops_below_threshold():
    res = rerank("вопрос", docs(3), CFG, transport=transport([0.9, 0.1, 0.6]))
    assert res["order"] == ["um_facts:0", "um_facts:2"]
    assert res["kept"] == 2 and res["dropped"] == 1
    assert res["scores"]["um_facts:0"] == pytest.approx(0.9)
    assert res["tokens"] == 11


def test_all_below_threshold_gives_empty_means_not_found():
    res = rerank("вопрос", docs(3), CFG, transport=transport([0.11, 0.33, 0.24]))
    assert res["order"] == []
    assert res["kept"] == 0 and res["dropped"] == 3


def test_redaction_and_truncation_before_send():
    captured = []
    text = ("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx12 " + "я" * 200)
    rerank("вопрос", [{"key": "um_facts:1", "text": text}], CFG,
           transport=transport([0.9], capture=captured))
    state = captured[0]["state"]
    assert "sk-abcdefghijklmnopqrstuvwx12" not in state
    assert PLACEHOLDER_PREFIX in state
    assert len(state) < 400          # doc_chars=40 в тестовом конфиге


def test_transport_error_is_returned_not_raised():
    def boom(*_a, **_k):
        raise OSError("сеть недоступна")

    res = rerank("вопрос", docs(2), CFG, transport=boom)
    assert res["error"] and "OSError" in res["error"]


def test_widen_signal_is_reported():
    res = rerank("вопрос", docs(2), CFG, transport=transport([0.9, 0.8], widen=0.85))
    assert res["widen"] == pytest.approx(0.85)


@pytest.fixture
def env(tmp_path):
    cfg = Config(db_path=tmp_path / "r.db", archive_path=tmp_path / "r.arc.db",
                 context_tokens=10**9, rerank="jev", rerank_pool=5)
    store = Store(cfg)
    ing = Ingest(store, FakeBackend(), cfg=cfg)
    for i, body in enumerate((
            "эмбеддинги слушают порт 8127",
            "фикс в ветке fix/thumbnails",
            "сертификат Saylor BUS202 сдан",
            "правила Skillshop: одна попытка")):
        ing.remember_fact("t", f"fact{i}", body)
    yield cfg, store
    store.close()


def test_recall_reranks_and_cuts_by_threshold(env):
    cfg, store = env
    query = "порт 8127 эмбеддинги"
    worst = Router(store, FakeBackend(), cfg).recall(
        query, scope="facts", limit=3, expire_working=False)[-1]
    key = f"{worst.owner_table}:{worst.owner_id}"
    router = Router(store, FakeBackend(), cfg,
                    rerank_transport=transport_by_key({key: 0.95}))
    got = router.recall(query, scope="facts", limit=3, expire_working=False)
    # остальные ниже порога → в выдаче ровно один элемент, и это «бывший худший»
    assert len(got) == 1
    assert (got[0].owner_table, got[0].owner_id) == (worst.owner_table, worst.owner_id)
    assert router.last_stats["rerank"]["kept"] == 1
    assert router.last_stats["rerank"]["error"] is None


def test_recall_reports_rerank_in_diagnostics(env):
    cfg, store = env
    router = Router(store, FakeBackend(), cfg,
                    rerank_transport=transport_by_key({}))
    router.recall("порт 8127", scope="facts", limit=3, diagnostics=True,
                  expire_working=False)
    diag = router.last_stats["diagnostics"]
    assert diag["rerank"] is not None and "kept" in diag["rerank"]


def test_recall_fails_open_and_is_loud(tmp_path):
    """Ошибка транспорта: выдача как в RRF-порядке, причина видна в stats."""
    base = dict(db_path=tmp_path / "a.db", archive_path=tmp_path / "a.arc.db",
                context_tokens=10**9)
    cfg_off = Config(**base, rerank="off")
    store = Store(cfg_off)
    ing = Ingest(store, FakeBackend(), cfg=cfg_off)
    ing.remember_fact("t", "порт", "эмбеддинги слушают порт 8127")
    ing.remember_fact("t", "ветка", "фикс в ветке fix/thumbnails")
    try:
        plain = Router(store, FakeBackend(), cfg_off).recall(
            "порт 8127", scope="facts", expire_working=False)

        def boom(*_a, **_k):
            raise OSError("нет сети")

        # rerank_pool=1 → пул и плечи как у обычного режима, сравнение честное
        cfg_on = Config(**base, rerank="jev", rerank_pool=1)
        router = Router(store, FakeBackend(), cfg_on, rerank_transport=boom)
        got = router.recall("порт 8127", scope="facts", expire_working=False)
        assert [(h.owner_table, h.owner_id) for h in got] == \
               [(h.owner_table, h.owner_id) for h in plain]
        assert router.last_stats["rerank"]["error"]
    finally:
        store.close()


def test_rerank_is_off_by_default_and_validated(tmp_path):
    cfg = Config(db_path=tmp_path / "b.db", archive_path=tmp_path / "b.arc.db")
    assert cfg.rerank == "off"
    assert cfg.rerank_pool == 25 and cfg.rerank_threshold == pytest.approx(0.5)
    assert cfg.rerank_key == "public" and "opencode.ai" in cfg.rerank_url
    with pytest.raises(ValueError):
        Config(db_path=tmp_path / "c.db", archive_path=tmp_path / "c.arc.db",
               rerank="maybe")
    with pytest.raises(ValueError):
        Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "d.arc.db",
               rerank="jev", rerank_threshold=1.5)
    with pytest.raises(ValueError):
        Config(db_path=tmp_path / "e.db", archive_path=tmp_path / "e.arc.db",
               rerank="jev", rerank_pool=0)
