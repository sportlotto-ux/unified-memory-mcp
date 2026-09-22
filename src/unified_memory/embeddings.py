"""Shared embedding kernel for the unified memory server.

Replaces two divergent providers with one:
- hermes-lcm ``embedding_provider.py`` (fastembed / voyage / ollama)
- mnemosyne ``core/embeddings.py`` (fastembed / OpenAI-compatible API)

Этап 0 покрывает общий знаменатель обоих: **локальный fastembed**.
v0.4-п.0 добавил второй бэкенд — **OpenAI-протокол** (`OpenAIBackend`,
stdlib urllib): локальный model2vec-сервер Hermes (potion, 256 dim,
авто-детект). Выбор — `make_backend(cfg)` по `UM_EMBEDDING_BACKEND`.

Ключевая деталь, ради которой слой общий, а не «просто обёртка»:
защита от расхождения размерностей. Mnemosyne уже болел этим:
смена ``MNEMOSYNE_EMBEDDING_MODEL`` без reindex даёт silent-деградацию
(chunks на чужих векторах). Здесь модель записывается в ``um_meta``,
а несовпадение dim → loud error + требование reindex, а не тишина.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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
    # Стенд Hermes автора: pruned-int8 potion через OpenAI-протокол (см. OpenAIBackend).
    # В fastembed её НЕТ — запись только объявляет dim для dimension-guard,
    # local-бэкенд с ней упадёт громко на warm (модель не найдена в fastembed).
    "minishlab/potion-multilingual-128M": ModelSpec(
        "minishlab/potion-multilingual-128M", 256, multilingual=True),
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
    def warm(self) -> None: ...


class DimensionMismatchError(RuntimeError):
    """Вектора в сторе эмбеддились другой моделью — нужен reindex, не silent-fallback."""


class EmbedServerError(RuntimeError):
    """OpenAI-протокол backend недоступен или отвечает мусором. Всегда громко."""


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


class OpenAIBackend:
    """OpenAI-протокол `/v1/embeddings` поверх stdlib urllib, ноль зависимостей.

    Основной кейс — локальный model2vec-сервер Hermes (127.0.0.1:8127,
    potion-multilingual-128M-pruned-int8-ruen): StaticModel — чистый numpy,
    fastembed/onnx (~2.6 GB на процесс) не нужен. Формат запроса/ответа —
    как у `.hermes/services/embeddings/embed_client.py`.

    dim авто-детектится пробой при первом обращении (pruned potion = 256)
    или задаётся явно (UM_EMBEDDING_DIM) — оффлайн-конструктор для тестов.

    Ловушка static-моделей (из комментариев embed-server): encode() МОЛЧА
    режет всё после 512-го токена, если на сервере выставлен EMBED_MAX_TOKENS.
    Держи сервер uncapped (дефолт) — тексты шлём как есть, как остальные
    консьюмеры. Недоступность сервера — всегда EmbedServerError, сервер
    падает в FTS-only с флагом в mem_status, а не в нули.
    """

    def __init__(self, model: str, base_url: str, timeout: float = 30.0,
                 dim: int = 0, max_tries: int = 2) -> None:
        if not base_url:
            raise ValueError("OpenAIBackend needs a non-empty base_url")
        self._model_name = model
        self._url = base_url.rstrip("/") + "/v1/embeddings"
        self._timeout = timeout
        self._max_tries = max(1, max_tries)
        env_dim = os.environ.get("UM_EMBEDDING_DIM", "").strip()
        self._dim = dim or (int(env_dim) if env_dim.isdigit() else 0)

    @property
    def dim(self) -> int:
        if not self._dim:
            self._dim = len(self.embed_query("warmup"))
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    def warm(self) -> None:
        _ = self.dim  # проба связи + детект dim, ошибка — громко

    def _post(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self._model_name, "input": texts}).encode()
        last: Exception | None = None
        for _ in range(self._max_tries):
            try:
                req = urllib.request.Request(
                    self._url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                return self._parse(body)
            except EmbedServerError:
                raise
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                raise EmbedServerError(
                    f"embedding server {self._url} HTTP {e.code}: {detail!r}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
        raise EmbedServerError(
            f"embedding server {self._url} unreachable after {self._max_tries} tries: {last}")

    @staticmethod
    def _parse(body: str) -> list[list[float]]:
        try:
            data = json.loads(body)["data"]
            rows = sorted(data, key=lambda r: r["index"])
            vecs = [[float(x) for x in r["embedding"]] for r in rows]
        except (ValueError, KeyError, TypeError) as e:
            raise EmbedServerError(f"bad /v1/embeddings response: {e}; body={body[:200]!r}")
        if not vecs or any(len(v) != len(vecs[0]) or not vecs[0] for v in vecs):
            raise EmbedServerError(f"ragged/empty embeddings; body={body[:200]!r}")
        return vecs

    def embed_docs(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._post(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._post([text])[0]


def make_backend(cfg) -> EmbeddingBackend:
    """Фабрика по cfg.embedding_backend: 'local' (fastembed) | 'openai' (8127/...)."""
    if cfg.embedding_backend == "openai":
        return OpenAIBackend(model=cfg.embedding_model,
                             base_url=cfg.embedding_base_url,
                             timeout=cfg.embedding_timeout)
    if cfg.embedding_backend == "local":
        return FastembedBackend(model=cfg.embedding_model)
    raise ValueError(f"unknown embedding backend {cfg.embedding_backend!r}")


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
