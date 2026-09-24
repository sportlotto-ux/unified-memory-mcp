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


def _default_halflife() -> float:
    return _strict_float("UM_RECENCY_HALFLIFE_DAYS", 30.0)


def _default_scope_bias() -> float:
    return _strict_float("UM_SCOPE_BIAS", 0.15)


def _default_mmr() -> float:
    return _strict_float("UM_MMR_LAMBDA", 0.7)


def _default_importance_weight() -> float:
    return _strict_float("UM_IMPORTANCE_WEIGHT", 0.0)


def _default_working_ttl_s() -> int:
    return _strict_int("UM_WORKING_TTL_S", 0)


def _default_working_limit() -> int:
    return _strict_int("UM_WORKING_LIMIT", 20)


def _default_retention() -> int:
    return _strict_int("UM_RETENTION_DAYS", 0)  # 0 = вечно (lossless-дефолт)


def _default_archive_mb() -> int:
    return _strict_int("UM_ARCHIVE_SIZE_MB", 1024)


def _default_archive_path() -> Path:
    raw = os.environ.get("UM_ARCHIVE_PATH")
    if raw:
        return Path(raw).expanduser()
    return _home() / "unified_memory.archive.db"


def _default_archive_batch() -> int:
    return _strict_int("UM_ARCHIVE_BATCH", 500)


def _default_archive_recall_scan() -> int:
    return _strict_int("UM_ARCHIVE_RECALL_SCAN_LIMIT", 2000)


def _default_ev_refs() -> int:
    return _strict_int("UM_EVIDENCE_MAX_REFS", 50)


def _default_ev_chars() -> int:
    return _strict_int("UM_EVIDENCE_MAX_CHARS", 8000)


def _default_ev_partial() -> float:
    return _strict_float("UM_EVIDENCE_PARTIAL", 0.5)


def _default_max_hops() -> int:
    return _strict_int("UM_RECALL_MAX_HOPS", 3)


def _default_link_fanout() -> int:
    return _strict_int("UM_LINK_FANOUT", 20)


def _default_graph_decay() -> float:
    return _strict_float("UM_GRAPH_DECAY", 0.5)


def _default_batch_max_ops() -> int:
    return _strict_int("UM_BATCH_MAX_OPS", 100)


def _default_max_text_chars() -> int:
    return _strict_int("UM_MAX_TEXT_CHARS", 200000)


def _default_compact_max_msgs() -> int:
    return _strict_int("UM_COMPACT_MAX_MSGS", 10000)


def _default_batch_max_chars() -> int:
    return _strict_int("UM_BATCH_MAX_CHARS", 200000)


def _default_vec_index() -> str:
    v = os.environ.get("UM_VEC_INDEX", "auto").strip().lower()
    if v not in ("auto", "off"):
        raise ValueError(f"UM_VEC_INDEX must be 'auto' or 'off', got {v!r}")
    return v


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
    # Ререйтинг recall (v0.4-п.3): recency-приор, scope-bias, MMR-диверсификация.
    recency_halflife_days: float = field(default_factory=_default_halflife)
    scope_bias: float = field(default_factory=_default_scope_bias)
    mmr_lambda: float = field(default_factory=_default_mmr)
    # vec0-индекс (v0.4-п.6): auto = строить в reindex и использовать при совпадении dim.
    vec_index: str = field(default_factory=_default_vec_index)
    # Retention/архив (v0.5-п.3): 0 = копим вечно (lossless-дефолт).
    retention_days: int = field(default_factory=_default_retention)
    archive_size_mb: int = field(default_factory=_default_archive_mb)
    archive_path: Path = field(default_factory=_default_archive_path)
    archive_batch: int = field(default_factory=_default_archive_batch)
    # Evidence (v0.6): бюджеты cite/compute. Без LLM.
    evidence_max_refs: int = field(default_factory=_default_ev_refs)
    evidence_max_chars: int = field(default_factory=_default_ev_chars)
    evidence_partial: float = field(default_factory=_default_ev_partial)
    # v0.7-п.4: BFS по um_links ∪ um_edges (hops>1). hops<=1 — старый путь.
    recall_max_hops: int = field(default_factory=_default_max_hops)
    link_fanout: int = field(default_factory=_default_link_fanout)
    graph_decay: float = field(default_factory=_default_graph_decay)
    # v0.7-п.5b: капы mem_batch (валидируются ДО открытия контура).
    batch_max_ops: int = field(default_factory=_default_batch_max_ops)
    batch_max_chars: int = field(default_factory=_default_batch_max_chars)
    # v0.7.3 (B10): кап входного текста — один гейт в Ingest._clean.
    max_text_chars: int = field(default_factory=_default_max_text_chars)
    # v0.8 (D15): кап головы компакшна — вместо 1M-скана хвост берём bounded.
    compact_max_msgs: int = field(default_factory=_default_compact_max_msgs)
    # P1.3: bounded lexical scan cap for explicit archive recall.
    archive_recall_scan: int = field(default_factory=_default_archive_recall_scan)
    # P1.4: optional bounded importance multiplier; 0 preserves legacy ranking.
    importance_weight: float = field(default_factory=_default_importance_weight)
    # P2.1: optional TTL for working slot-facts and bounded assembly cap.
    working_ttl_s: int = field(default_factory=_default_working_ttl_s)
    working_limit: int = field(default_factory=_default_working_limit)

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
        if self.recency_halflife_days < 0:
            raise ValueError("UM_RECENCY_HALFLIFE_DAYS must be >= 0 (0 = recency off)")
        if self.scope_bias < 0:
            raise ValueError("UM_SCOPE_BIAS must be >= 0 (0 = no session boost)")
        if not 0.0 <= self.mmr_lambda <= 1.0:
            raise ValueError("UM_MMR_LAMBDA must be in [0, 1] (1 = pure relevance)")
        if not 0.0 <= self.importance_weight <= 1.0:
            raise ValueError("UM_IMPORTANCE_WEIGHT must be in [0, 1] (0 = off)")
        if self.working_ttl_s < 0:
            raise ValueError("UM_WORKING_TTL_S must be >= 0 (0 = off)")
        if self.working_limit <= 0:
            raise ValueError("UM_WORKING_LIMIT must be > 0")
        if self.retention_days < 0:
            raise ValueError("UM_RETENTION_DAYS must be >= 0 (0 = keep forever)")
        if self.archive_size_mb <= 0:
            raise ValueError("UM_ARCHIVE_SIZE_MB must be > 0")
        if self.archive_batch <= 0:
            raise ValueError("UM_ARCHIVE_BATCH must be > 0")
        if self.archive_recall_scan <= 0:
            raise ValueError("UM_ARCHIVE_RECALL_SCAN_LIMIT must be > 0")
        if self.archive_path == self.db_path:
            raise ValueError("UM_ARCHIVE_PATH must differ from the main DB")
        if self.evidence_max_refs <= 0:
            raise ValueError("UM_EVIDENCE_MAX_REFS must be > 0")
        if self.evidence_max_chars <= 0:
            raise ValueError("UM_EVIDENCE_MAX_CHARS must be > 0")
        if not 0.0 < self.evidence_partial <= 1.0:
            raise ValueError("UM_EVIDENCE_PARTIAL must be in (0, 1]")
        if self.recall_max_hops < 1:
            raise ValueError("UM_RECALL_MAX_HOPS must be >= 1")
        if self.link_fanout < 1:
            raise ValueError("UM_LINK_FANOUT must be >= 1")
        if not 0.0 < self.graph_decay <= 1.0:
            raise ValueError("UM_GRAPH_DECAY must be in (0, 1]")
        if self.batch_max_ops <= 0:
            raise ValueError("UM_BATCH_MAX_OPS must be > 0")
        if self.batch_max_chars <= 0:
            raise ValueError("UM_BATCH_MAX_CHARS must be > 0")
        if self.max_text_chars <= 0:
            raise ValueError("UM_MAX_TEXT_CHARS must be > 0")
        if self.compact_max_msgs <= 0:
            raise ValueError("UM_COMPACT_MAX_MSGS must be > 0")
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
