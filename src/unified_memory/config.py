"""Env-based config. Один контракт вместо LCM_* + MNEMOSYNE_* зоопарка.

На этапе 0 — только общее (путь БД, модель). Специфичные ручки движков
переедут сюда на этапе 2 с префиксом UM_ и таблицей совместимости.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


@dataclass(frozen=True)
class Config:
    db_path: Path = _home() / "unified_memory.db"
    embedding_model: str = os.environ.get(
        "UM_EMBEDDING_MODEL",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    vec_type: str = os.environ.get("UM_VEC_TYPE", "int8")  # как MNEMOSYNE_VEC_TYPE


def load() -> Config:
    db = os.environ.get("UM_DATABASE_PATH")
    return Config(db_path=Path(db) if db else _home() / "unified_memory.db")
