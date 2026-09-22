import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store


@pytest.fixture
def ing(tmp_path):
    store = Store(Config(db_path=tmp_path / "g.db"))
    yield Ingest(store, FakeBackend())
    store.close()


def test_triple_roundtrip(ing):
    ing.remember_fact("preference", "чай", "любит зелёный",
                      subject="Иван", predicate="любит", obj="зелёный чай")
    nbs = ing.store.neighbors("иван")
    assert len(nbs) == 1
    assert nbs[0]["predicate"] == "любит"
    assert nbs[0]["object"] == "зелёный чай"


def test_entity_dedup_case_insensitive(ing):
    ing.store.add_edge("Иван", "любит", "чай")
    ing.store.add_edge("иван", "пьёт", "кофе")
    assert len(ing.store.neighbors("ИВАН")) == 2
    st = ing.store.stats()
    assert st["um_entities"] == 3  # иван, чай, кофе


def test_graph_arm_in_recall(ing):
    ing.remember_fact("f", "x", "что-то про отпуск",
                      subject="Иван", predicate="планирует", obj="отпуск")
    ing.remember_message("s1", "user", "совершенно другой текст про серверы")
    hits = ing.router().recall("Иван")
    assert any(h.owner_table == "um_edges" for h in hits)
    edge = next(h for h in hits if h.owner_table == "um_edges")
    assert "планирует" in edge.body


def test_facts_scope_excludes_graph(ing):
    ing.remember_fact("f", "x", "y", subject="Иван", predicate="любит", obj="чай")
    hits = ing.router().recall("Иван", scope="facts")
    assert all(h.owner_table == "um_facts" for h in hits)
