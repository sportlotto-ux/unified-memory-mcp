"""Atomicity tests for compaction and condensation writes."""

import pytest

from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store


class FailingSecondSummarizer:
    def __init__(self):
        self.calls = 0

    def summarize(self, texts, max_sentences=8):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("condensation failed")
        return "leaf summary"


def test_maybe_compact_rolls_back_leaf_when_condense_fails(tmp_path):
    cfg = Config(db_path=tmp_path / "compaction.db",
                 archive_path=tmp_path / "compaction-archive.db",
                 context_tokens=10, compact_threshold=0.5,
                 fresh_tail=0, dag_fanin=2)
    store = Store(cfg)
    summarizer = FailingSecondSummarizer()
    ingest = Ingest(store, None, summarizer, cfg)
    try:
        store.add_summary("s1", "existing summary")
        store.add_message("s1", "user", "new message")
        with pytest.raises(RuntimeError, match="condensation failed"):
            ingest.window.maybe_compact("s1")
        assert store.select(
            "SELECT id, depth, superseded_by FROM um_summaries ORDER BY id"
        ) == [(1, 0, 0)]
        assert store.meta_get("frontier:s1") is None
    finally:
        store.close()
