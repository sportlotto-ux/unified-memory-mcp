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


def _default_arm_limit() -> int:
    # 10 = историческая ширина плеча для малых страниц (limit=5 → было limit*2).
    # Больше — шумовые кандидаты получают второй голос в RRF и вытесняют точные
    # попадания: golden-eval проекта краснеет уже при 20 (графовый хит выпадает
    # из топ-3), на реальной памяти промах «обновление зависимостей» ведёт себя
    # так же. Поэтому дефолт — узкое постоянное плечо, а не широкое.
    return _strict_int("UM_RECALL_ARM_LIMIT", 10)


def _default_edge_weight() -> float:
    # Ребро — наводка на связь, а не ответ. При весе 1.0 (legacy) графовое
    # плечо давало рёбрам голос верхнего ранга, и они вытесняли факты из топ-1:
    # замер 18.10.2026 на 19 реальных запросах — scope=all hit@1 26% → 63% при
    # весе 0.5, scope=facts не сдвинулся. Связи ищутся mem_graph_query.
    return _strict_float("UM_EDGE_WEIGHT", 0.5)


def _default_message_weight() -> float:
    # Стенограмма — контекст, а не знание: при весе 1.0 захваченная история
    # топила дефолтный поиск (замер 18.10.2026: после захвата 1287 сообщений
    # scope=all hit@1 63% → 58%, hit@3 95% → 84%; при весе 0.5 — 68% / 95%).
    # Сообщения остаются находимыми, просто ниже фактов.
    return _strict_float("UM_MESSAGE_WEIGHT", 0.5)


def _default_rerank() -> str:
    # Реранкер — опция, а не дефолт: ходит в сеть (документы уходят наружу) и
    # добавляет ~0.8 с к вызову. Включается осознанно: UM_RERANK=jev.
    raw = (os.environ.get("UM_RERANK") or "off").strip().lower()
    if raw not in ("off", "jev"):
        raise ValueError(f"UM_RERANK must be 'off' or 'jev', got {raw!r}")
    return raw


def _default_rerank_url() -> str:
    from .rerank import DEFAULT_URL
    return os.environ.get("UM_RERANK_URL") or DEFAULT_URL


def _default_rerank_model() -> str:
    from .rerank import DEFAULT_MODEL
    return os.environ.get("UM_RERANK_MODEL") or DEFAULT_MODEL


def _default_rerank_threshold() -> float:
    # 0.5 — порог плагина Jev и замера: на вопросах без ответа в памяти
    # топ-оценка была 0.03–0.33, там где ответ есть — 0.84–0.97.
    return _strict_float("UM_RERANK_THRESHOLD", 0.5)


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
    from .redact import ALL, KNOWN

    raw = os.environ.get("UM_REDACT_PATTERNS")
    names = [p.strip().lower() for p in raw.split(",")] if raw else list(ALL)
    unknown = [p for p in names if p not in KNOWN]
    if unknown:
        raise ValueError(f"UM_REDACT_PATTERNS unknown: {unknown}, known: {list(KNOWN)}")
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
    # Ширина плеч recall (FTS/вектора/граф), независимая от limit: иначе порядок
    # выдачи плывёт от размера запрошенной страницы (замер 18.10.2026: топ-1
    # совпадал при разных limit лишь у 9/19 запросов).
    recall_arm_limit: int = field(default_factory=_default_arm_limit)
    # Вес ребра в RRF-слиянии: <1 не пускает «наводки»-связи вперёд
    # содержимого (фактов/сообщений).
    edge_weight: float = field(default_factory=_default_edge_weight)
    # Вес сообщения в RRF-слиянии: <1 не пускает стенограммы вперёд фактов.
    message_weight: float = field(default_factory=_default_message_weight)
    # Реранкер recall (Jev): судит пары (вопрос, кандидат) поверх широкого пула.
    # Замер 18.10.2026: hit@1 в scope=all 79% → 89%, а на вопросах без ответа в
    # памяти он же даёт «не нашёл» (топ-оценка 0.03–0.33 против порога 0.5).
    rerank: str = field(default_factory=_default_rerank)
    rerank_url: str = field(default_factory=_default_rerank_url)
    # Zen-канал ключа не требует ('public'); второй канал — ключ TypeSafe.
    rerank_key: str = field(
        default_factory=lambda: os.environ.get("UM_RERANK_KEY") or "public")
    rerank_model: str = field(default_factory=_default_rerank_model)
    rerank_threshold: float = field(default_factory=_default_rerank_threshold)
    # Ширина пула для судейства и кап текста кандидата (после редакции).
    rerank_pool: int = field(default_factory=lambda: _strict_int("UM_RERANK_POOL", 25))
    rerank_timeout: float = field(default_factory=lambda: _strict_float("UM_RERANK_TIMEOUT", 20.0))
    rerank_doc_chars: int = field(default_factory=lambda: _strict_int("UM_RERANK_DOC_CHARS", 900))
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
        if self.recall_arm_limit < 1:
            raise ValueError("UM_RECALL_ARM_LIMIT must be >= 1")
        if self.edge_weight < 0:
            raise ValueError("UM_EDGE_WEIGHT must be >= 0")
        if self.message_weight < 0:
            raise ValueError("UM_MESSAGE_WEIGHT must be >= 0")
        if self.rerank not in ("off", "jev"):
            raise ValueError("UM_RERANK must be 'off' or 'jev'")
        if not 0.0 <= self.rerank_threshold <= 1.0:
            raise ValueError("UM_RERANK_THRESHOLD must be in [0, 1]")
        if self.rerank_pool < 1:
            raise ValueError("UM_RERANK_POOL must be >= 1")
        if self.rerank_timeout <= 0:
            raise ValueError("UM_RERANK_TIMEOUT must be > 0")
        if self.rerank_doc_chars < 0:
            raise ValueError("UM_RERANK_DOC_CHARS must be >= 0 (0 = без обрезки)")
        if self.rerank != "off" and not self.rerank_url:
            raise ValueError("UM_RERANK_URL must be non-empty when rerank is on")
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
