"""Exact object metadata and read-only inspection primitives."""

from unified_memory.config import Config
from unified_memory.store import Store


def test_ref_details_returns_metadata_vector_and_links(tmp_path):
    store = Store(Config(
        db_path=tmp_path / "inspect.db",
        archive_path=tmp_path / "inspect.archive.db",
        vec_index="off",
    ))
    try:
        message = store.add_message("s", "user", "message", owner="alice")
        fact = store.add_fact("c", "name", "fact", owner="alice")
        store.link("um_messages", message, "um_facts", fact,
                   "supports", owner="alice")
        store.add_vector("um_facts", fact, [0.1] * 16,
                         "fake/model-16", owner="alice")

        details = store.ref_details("um_facts", fact, owner="alice")
        assert details is not None
        assert details["metadata"]["name"] == "name"
        assert details["vector"] == {
            "present": True, "model": "fake/model-16", "dim": 16}
        assert details["links"][0]["rel"] == "supports"
        assert store.ref_details("um_facts", fact, owner="bob") is None
    finally:
        store.close()
