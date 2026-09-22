"""Smoke tests for the shared embedding kernel. No network, no model download:
fastembed-dependent cases skip when fastembed is absent.
"""

import os

import pytest

from unified_memory.embeddings import (
    MODEL_REGISTRY,
    DimensionMismatchError,
    FastembedBackend,
    check_store_dim,
)


def test_registry_has_dims():
    assert MODEL_REGISTRY
    for spec in MODEL_REGISTRY.values():
        assert spec.dim > 0


def test_unknown_model_rejected():
    with pytest.raises(ValueError):
        FastembedBackend(model="nope/not-a-model")


def test_dim_mismatch_is_loud():
    def meta(key):
        return {"embedding_model": "BAAI/bge-small-en-v1.5"}.get(key)

    with pytest.raises(DimensionMismatchError):
        check_store_dim(1024, "intfloat/multilingual-e5-large", meta)


def test_fresh_store_passes():
    check_store_dim(384, "whatever", lambda key: None)


fastembed = pytest.importorskip("fastembed", reason="pip install -e .[local-embed]")


@pytest.mark.skipif(not os.environ.get("UM_LIVE_EMBED_TEST"),
                    reason="downloads a model; set UM_LIVE_EMBED_TEST=1 to run")
def test_embed_query_dim(tmp_path):
    from unified_memory.embeddings import DEFAULT_MODEL
    b = FastembedBackend(model=os.environ.get("UM_LIVE_MODEL", DEFAULT_MODEL),
                         cache_dir=tmp_path)
    b.warm()
    vec = b.embed_query("проверка связи")
    assert len(vec) == b.dim
