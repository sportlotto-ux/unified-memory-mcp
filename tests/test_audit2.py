"""Регрессия по третьему аудиту."""

import json
import os

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store, estimate_tokens
from unified_memory.summarize import EndpointSummarizer, ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "c.db", context_tokens=200,
                 compact_threshold=0.5, fresh_tail=2, dag_fanin=2)
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


def test_short_query_trigram_fallback(ing):
    ing.remember_message("s1", "user", "он пришёл вечером")
    hits = ing.store.fts_search("он")
    assert any("пришёл" in h.body for h in hits), "trigram<3 без LIKE-fallback"


def test_session_scope_requires_id(ing):
    ing.store.add_edge("Иван", "любит", "чай", session_id="s2")
    assert ing.router().recall("Иван", scope="session", session_id="") == []


def test_expand_bad_kind_valueerror(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "e.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as srv
    with pytest.raises(ValueError):
        srv.mem_expand(kind="битый", id=1)


def test_endpoint_empty_content_guard(monkeypatch):
    s = EndpointSummarizer(url="http://x/v1", model="m")

    class FakeResp:
        def read(self):
            return b'{"choices": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: FakeResp())
    with pytest.raises(ValueError):
        s.summarize(["текст"])


def test_superseded_skipped_in_assemble(ing):
    for i in range(8):
        ing.remember_message("s1", "user", f"факт {i} про запуск и отчёт" * 3)
    asm = ing.window.assemble("s1", budget=100000)
    live = {x["id"] for x in asm["summaries"]}
    sup = {r[0] for r in ing.store.select(
        "SELECT id FROM um_summaries WHERE superseded_by != 0")}
    assert sup, "конденсации не было"
    assert live & sup == set(), "parent и дети в сборке одновременно"


def test_token_counter_incremental(ing):
    ing.remember_message("s1", "user", "раз два три")
    v1 = int(ing.store.meta_get("tokens:s1"))
    assert v1 == estimate_tokens("раз два три")
    ing.remember_message("s1", "user", "четыре")
    v2 = int(ing.store.meta_get("tokens:s1"))
    assert v2 == v1 + estimate_tokens("четыре")
    assert ing.window.pressure("s1").tokens_total == v2
