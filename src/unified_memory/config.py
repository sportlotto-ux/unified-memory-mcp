"""Env-based config. Один контракт вместо LCM_* + MNEMOSYNE_* зоопарка."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .embeddings import DEFAULT_MODEL


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    db_path: Path = _home() / "unified_memory.db"
    embedding_model: str = DEFAULT_MODEL
    # Активное окно (наследники LCM-настроек, префикс UM_):
    context_tokens: int = 200000   # эффективное окно хоста
    compact_threshold: float = 0.35  # доля окна — триггер компакшна
    fresh_tail: int = 20           # сообщений не жмём никогда
    dag_fanin: int = 5             # нод одного уровня → одна выше
    assembly_budget: int = 8000    # токенов в mem_assemble по дефолту

    def __post_init__(self) -> None:
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
    db = os.environ.get("UM_DATABASE_PATH")
    return Config(
        db_path=Path(db).expanduser() if db else _home() / "unified_memory.db",
        embedding_model=os.environ.get("UM_EMBEDDING_MODEL", DEFAULT_MODEL),
        context_tokens=_int("UM_CONTEXT_TOKENS", 200000),
        compact_threshold=_float("UM_COMPACT_THRESHOLD", 0.35),
        fresh_tail=_int("UM_FRESH_TAIL_COUNT", 20),
        dag_fanin=_int("UM_DAG_FANIN", 5),
        assembly_budget=_int("UM_ASSEMBLY_BUDGET", 8000),
    )
