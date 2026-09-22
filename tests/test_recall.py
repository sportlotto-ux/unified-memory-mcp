import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing():
    import tempfile
    from pathlib import Path
    db = Path(tempfile.mkdtemp()) / "t.db"
    store = Store(Config(db_path=db))
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer())
    store.close()


def test_recall_fuses_fts_and_vectors(ing):
    ing.remember_message("s1", "user", "встреча в четверг по бюджету проекта")
    ing.remember_message("s1", "user", "кот сидит на ковре")
    hits = ing.router().recall("бюджет проекта")
    assert hits
    assert "бюджет" in hits[0].body


def test_recall_facts_scope(ing):
    ing.remember_fact("preference", "чай", "любит зелёный чай по утрам")
    hits = ing.router().recall("чай", scope="facts")
    assert hits
    assert hits[0].owner_table == "um_facts"


def test_compact_keeps_raw(ing):
    for i in range(5):
        ing.remember_message("s1", "user", f"сообщение номер {i} про бюджет и планы")
    res = ing.compact_session("s1", keep_tail=2)
    assert res["status"] == "compacted"
    assert res["covered"] == 3
    # lossless: сырьё на месте
    assert len(ing.store.session_messages("s1", limit=100)) == 5
    # summary ищется
    hits = ing.router().recall("бюджет")
    assert any(h.owner_table == "um_summaries" for h in hits)


def test_compact_noop(ing):
    ing.remember_message("s1", "user", "одно сообщение")
    assert ing.compact_session("s1", keep_tail=20)["status"] == "noop"


def test_extractive_is_deterministic():
    s = ExtractiveSummarizer()
    texts = ["Кот сидит на ковре. Бюджет утверждён в четверг.",
             "В четверг встреча. Кот мурлычет."]
    assert s.summarize(texts, 2) == s.summarize(texts, 2)
    assert "четверг" in s.summarize(texts, 2)
