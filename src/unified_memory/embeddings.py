"""Shared embedding kernel for the unified memory server.

Replaces two divergent providers with one:
- hermes-lcm ``embedding_provider.py`` (fastembed / voyage / ollama)
- mnemosyne ``core/embeddings.py`` (fastembed / OpenAI-compatible API)

Этап 0 покрывает общий знаменатель обоих: **локальный fastembed**.
Cloud-бэкенды (voyage / OpenAI-совместимый) дотягиваются на этапе 3 через тот же
интерфейс ``EmbeddingBackend``.

Ключевая деталь, ради которой слой общий, а не «просто обёртка»:
защита от расхождения размерностей. Mnemosyne уже болел этим:
смена ``MNEMOSYNE_EMBEDDING_MODEL`` без reindex даёт silent-деградацию
(chunks на чужих векторах). Здесь модель записывается в ``um_meta``,
а несовпадение dim → loud error + требование reindex, а не тишина.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


# dim зафиксированы за реестром fastembed на момент написания.
# Новая модель добавляется одной строкой — и обязана прийти с dim.
@dataclass(frozen=True)
class ModelSpec:
    name: str
    dim: int
    multilingual: bool = False
    query_prefix: str = ""  # E5-style: "query: " / "passage: "
    doc_prefix: str = ""


MODEL_REGISTRY: dict[str, ModelSpec] = {
    # Легаси mnemosyne-дефолт (English-only).
    "BAAI/bge-small-en-v1.5": ModelSpec("BAAI/bge-small-en-v1.5", 384),
    # Популярный лёгкий стенд.
    "sentence-transformers/all-MiniLM-L6-v2": ModelSpec(
        "sentence-transformers/all-MiniLM-L6-v2", 384),
    # Активная модель стенда 09.2026 (mnemosyne + skill-recall + rerank делят её).
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": ModelSpec(
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384, multilingual=True
    ),
    # Остаток мультиязычного реестра fastembed.
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": ModelSpec(
        "sentence-transformers/paraphrase-multilingual-mpnet-base-v2", 768, multilingual=True
    ),
    "intfloat/multilingual-e5-large": ModelSpec(
        "intfloat/multilingual-e5-large", 1024, multilingual=True,
        query_prefix="query: ", doc_prefix="passage: ",
    ),
}

# Дефолт репо — ПОЛНАЯ mpnet-base-v2 (768, не дистиллят).
# Дистиллированная MiniLM-L12 (384) остаётся для лёгких стендов
# (например, Hermes-инсталляция автора) через UM_EMBEDDING_MODEL.
DEFAULT_MODEL = os.environ.get(
    "UM_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
)


class EmbeddingBackend(Protocol):
    dim: int
    model_name: str

    def embed_docs(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class DimensionMismatchError(RuntimeError):
    """Вектора в сторе эмбеддились другой моделью — нужен reindex, не silent-fallback."""


class FastembedBackend:
    """Локальный CPU-бэкенд. Единственный обязательный на этапе 0."""

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: Path | None = None) -> None:
        spec = MODEL_REGISTRY.get(model)
        if spec is None:
            raise ValueError(
                f"model {model!r} not in MODEL_REGISTRY — добавь с dim, "
                "вектора без объявленной размерности не храним"
            )
        self.spec = spec
        self.dim = spec.dim
        self._model_name = model
        self._cache_dir = str(cache_dir) if cache_dir else None
        self._emb = None

    def _ensure(self):
        if self._emb is None:
            from fastembed import TextEmbedding

            kwargs = {"model_name": self._model_name}
            if self._cache_dir:
                kwargs["cache_dir"] = self._cache_dir
            self._emb = TextEmbedding(**kwargs)
        return self._emb

    def embed_docs(self, texts: list[str]) -> list[list[float]]:
        emb = self._ensure()
        prefixed = [f"{self.spec.doc_prefix}{t}" if self.spec.doc_prefix else t for t in texts]
        return [list(map(float, v)) for v in emb.embed(prefixed)]

    def embed_query(self, text: str) -> list[float]:
        emb = self._ensure()
        q = f"{self.spec.query_prefix}{text}" if self.spec.query_prefix else text
        return list(map(float, next(iter(emb.embed([q])))))

    def warm(self) -> None:
        self._ensure()

    @property
    def model_name(self) -> str:
        return self._model_name


def check_store_dim(expected_dim: int, model_name: str, meta_getter) -> None:
    """Сверить dim стора с активной моделью. Вызывать при open() БД.

    ``meta_getter(key)`` читает ``um_meta``. Первый запуск (пусто) —
    записывать должен вызывающий код, здесь только проверка.
    """
    stored = meta_getter("embedding_model")
    if stored is None:
        return  # fresh store — caller stamps it
    spec = MODEL_REGISTRY.get(stored)
    stored_dim = spec.dim if spec else int(meta_getter("embedding_dim") or 0)
    if stored_dim != expected_dim:
        raise DimensionMismatchError(
            f"store embedded with {stored!r} (dim={stored_dim}), "
            f"active model {model_name!r} (dim={expected_dim}). "
            "Запусти reindex, смена модели без него роняет recall молча."
        )


if __name__ == "__main__":  # smoke-test: python -m unified_memory.embeddings
    try:
        b = FastembedBackend()
    except ValueError as e:
        raise SystemExit(e)
    try:
        b.warm()
    except ImportError:
        raise SystemExit("fastembed not installed: pip install -e .[local-embed]")
    v = b.embed_query("проверка связи")
    assert len(v) == b.dim, (len(v), b.dim)
    print(f"OK: {b.spec.name} dim={b.dim}")
