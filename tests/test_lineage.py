"""Summary lineage and lossless session transcript pagination."""

from unified_memory.config import Config
from unified_memory.store import Store


def test_summary_lineage_and_transcript_pagination(tmp_path):
    store = Store(Config(
        db_path=tmp_path / "lineage.db",
        archive_path=tmp_path / "lineage.archive.db",
    ))
    try:
        first = store.add_message("s", "user", "first")
        second = store.add_message("s", "assistant", "second")
        store.add_message("s", "user", "third")
        leaf = store.add_summary(
            "s", "leaf", covers_from=first, covers_to=second,
            sources=[("um_messages", first), ("um_messages", second)])
        parent = store.add_summary(
            "s", "parent", depth=1, sources=[("um_summaries", leaf)])

        assert store.summary_lineage(leaf)["sources"] == [
            {"kind": "message", "table": "um_messages", "id": first,
             "position": 0},
            {"kind": "message", "table": "um_messages", "id": second,
             "position": 1},
        ]
        assert store.summary_lineage(parent)["sources"][0]["kind"] == "summary"

        page1 = store.session_transcript("s", limit=2)
        page2 = store.session_transcript("s", after_id=page1[-1]["id"], limit=2)
        assert [m["body"] for m in page1 + page2] == ["first", "second", "third"]
        assert all(not m["archived"] for m in page1 + page2)
    finally:
        store.close()
