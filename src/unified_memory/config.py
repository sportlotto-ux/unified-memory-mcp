"""Env-based config. Один контракт вместо LCM_* + MNEMOSYNE_* зоопарка."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .embeddings import DEFAULT_MODEL


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _default_db() -> Path:
    db = os.environ.get("UM_DATABASE_PATH")
    return Path(db).expanduser() if db else _home() / "unified_memory.db"


def _default_model() -> str:
    return os.environ.get("UM_EMBEDDING_MODEL", DEFAULT_MODEL)


def _strict_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer")


def _strict_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number")


def _default_ctx() -> int:
    return _strict_int("UM_CONTEXT_TOKENS", 200000)


def _default_thr() -> float:
    return _strict_float("UM_COMPACT_THRESHOLD", 0.35)


def _default_tail() -> int:
    return _strict_int("UM_FRESH_TAIL_COUNT", 20)


def _default_fanin() -> int:
    return _strict_int("UM_DAG_FANIN", 5)


def _default_budget() -> int:
    return _strict_int("UM_ASSEMBLY_BUDGET", 8000)


def _default_backend() -> str:
    return os.environ.get("UM_EMBEDDING_BACKEND", "local").strip().lower()


def _default_base_url() -> str:
    return os.environ.get("UM_EMBEDDING_BASE_URL", "http://127.0.0.1:8127").rstrip("/")


def _default_timeout() -> float:
    return _strict_float("UM_EMBEDDING_TIMEOUT", 30.0)


def _strict_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name}={raw!r} is not a boolean")


def _default_redact() -> bool:
    # Публичный продукт: default ON (у LCM off — то для себя).
    return _strict_bool("UM_REDACT_ENABLED", True)


def _default_redact_patterns() -> tuple[str, ...]:
    from .redact import ALL, PATTERNS

    raw = os.environ.get("UM_REDACT_PATTERNS")
    names = [p.strip().lower() for p in raw.split(",")] if raw else list(ALL)
    unknown = [p for p in names if p not in PATTERNS]
    if unknown:
        raise ValueError(f"UM_REDACT_PATTERNS unknown: {unknown}, known: {list(ALL)}")
    return tuple(names)


@dataclass(frozen=True)
class Config:
    # default_factory читают env при каждой инстанциации —
    # Config() без load() больше не протухший.
    db_path: Path = field(default_factory=_default_db)
    embedding_model: str = field(default_factory=_default_model)
    # Активное окно (наследники LCM-настроек, префикс UM_):
    context_tokens: int = field(default_factory=_default_ctx)
    compact_threshold: float = field(default_factory=_default_thr)
    fresh_tail: int = field(default_factory=_default_tail)
    dag_fanin: int = field(default_factory=_default_fanin)
    assembly_budget: int = field(default_factory=_default_budget)
    # Эмбеддинги: local (fastembed, default) | openai (OpenAI-протокол,
    # например локальный model2vec-сервер Hermes на 127.0.0.1:8127).
    embedding_backend: str = field(default_factory=_default_backend)
    embedding_base_url: str = field(default_factory=_default_base_url)
    embedding_timeout: float = field(default_factory=_default_timeout)
    # Redaction-гейт (v0.4-п.1): default ON, каталог как у LCM.
    redact_enabled: bool = field(default_factory=_default_redact)
    redact_patterns: tuple[str, ...] = field(default_factory=_default_redact_patterns)

    def __post_init__(self) -> None:
        if self.embedding_backend not in ("local", "openai"):
            raise ValueError(
                "UM_EMBEDDING_BACKEND must be 'local' or 'openai', "
                f"got {self.embedding_backend!r}")
        if self.embedding_backend == "openai" and not self.embedding_base_url:
            raise ValueError("UM_EMBEDDING_BASE_URL must be non-empty for backend='openai'")
        if self.embedding_timeout <= 0:
            raise ValueError("UM_EMBEDDING_TIMEOUT must be > 0")
        if self.redact_enabled and not self.redact_patterns:
            raise ValueError("UM_REDACT_PATTERNS must be non-empty when redaction is enabled")
        if self.context_tokens <= 0:
            raise ValueError("UM_CONTEXT_TOKENS must be > 0")
        if not 0.0 < self.compact_threshold <= 1.0:
            raise ValueError("UM_COMPACT_THRESHOLD must be in (0, 1]")
        if self.fresh_tail < 0:
            raise ValueError("UM_FRESH_TAIL_COUNT must be >= 0")
        if self.dag_fanin < 2:
            raise ValueError("UM_DAG_FANIN must be >= 2 (1 плодит мусорные уровни)")
        if self.assembly_budget <= 0:
            raise ValueError("UM_ASSEMBLY_BUDGET must be > 0")


def load() -> Config:
    return Config()
