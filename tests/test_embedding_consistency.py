"""Embedding model and dimension consistency checks."""

import pytest

from unified_memory.config import Config
from unified_memory.embeddings import DimensionMismatchError
from unified_memory.store import Store


def _store(tmp_path, *, dim=0, model=""):
    return Store(
        Config(db_path=tmp_path / "embedding-consistency.db",
               archive_path=tmp_path / "embedding-consistency-archive.db",
               context_tokens=10**9),
        embedding_dim=dim,
        embedding_model=model,
    )


def test_first_vector_stamps_model_and_dimension(tmp_path):
    store = _store(tmp_path)
    try:
        store.add_vector("um_messages", 1, [0.1] * 4, "model-a")
        assert store.meta_get("embedding_model") == "model-a"
        assert store.meta_get("embedding_dim") == "4"
    finally:
        store.close()


def test_same_dimension_different_model_is_rejected(tmp_path):
    store = _store(tmp_path, dim=4, model="model-a")
    try:
        store.add_vector("um_messages", 1, [0.1] * 4, "model-a")
        with pytest.raises(DimensionMismatchError):
            store.add_vector("um_messages", 1, [0.2] * 4, "model-b")
        assert store.select(
            "SELECT model FROM um_vectors WHERE owner_table='um_messages'"
            " AND owner_id=1"
        ) == [("model-a",)]
    finally:
        store.close()


def test_wrong_dimension_is_rejected_before_replace(tmp_path):
    store = _store(tmp_path, dim=4, model="model-a")
    try:
        store.add_vector("um_messages", 1, [0.1] * 4, "model-a")
        with pytest.raises(DimensionMismatchError):
            store.add_vector("um_messages", 1, [0.2] * 3, "model-a")
        assert store.select(
            "SELECT length(embedding) FROM um_vectors"
            " WHERE owner_table='um_messages' AND owner_id=1"
        ) == [(16,)]
    finally:
        store.close()
