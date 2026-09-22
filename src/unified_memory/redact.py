"""Deterministic secret redaction at the ingest boundary (v0.4-п.1).

Порт каталога hermes-lcm ``ingest_protection._SENSITIVE_PATTERN_CATALOG``:
те же 4 имени, те же регулярки — чтобы секреты, пережившие LCM-гейт или
пришедшие напрямую в MCP, резались одинаково. Без LCM-наворотов: нет
sha-доказательств, нет таймаут-потоков, плейсхолдер необратимый.

От backtracking-защиты — быстрый substring-prefilter перед private_key:
ленивый ``.*?`` под DOTALL без якоря пересканирует хвост на каждой позиции,
поэтому без ``PRIVATE KEY`` в тексте regex-движок вообще не запускаем.

Режется ДО SQLite/FTS/vectors/summaries: все производные (саммари компакшна,
граф, вектора) строятся из уже чистого текста. Forward-only: что попало
в стор до включения гейта — там и осталось, чистить руками + reindex.
"""

from __future__ import annotations

import re

# Имена = ключи LCM-каталога, совместимость настроек один в один.
PATTERNS: dict[str, re.Pattern[str]] = {
    "api_key": re.compile(
        r"(?P<prefix>(?:\\?[\"']?)\b(?:api[_-]?key|api[_-]?token|access[_-]?token|secret[_-]?key|client[_-]?secret)\b\s*(?:\\?[\"']?)\s*[:=]\s*(?:\\?[\"']?))"
        r"(?P<secret>[A-Za-z0-9._~+/=-]{12,})"
        r"(?P<suffix>\\?[\"']?)",
        re.IGNORECASE,
    ),
    "bearer_token": re.compile(
        r"(?P<prefix>\bBearer\s+)"
        r"(?P<secret>[A-Za-z0-9._~+/=-]{12,})",
        re.IGNORECASE,
    ),
    "password_assignment": re.compile(
        r"(?P<prefix>\b(?:password|passwd|pwd|passphrase)\b\s*[\"']?\s*[:=]\s*)"
        r"(?:(?P<quote>[\"'])(?P<secret_quoted>[^\r\n\]\}]{6,}?)(?P=quote)|"
        r"(?P<secret_unquoted>[^\s,\"'\]}]{6,}))",
        re.IGNORECASE,
    ),
    "private_key": re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.IGNORECASE | re.DOTALL,
    ),
}

ALL = tuple(PATTERNS)

PLACEHOLDER_PREFIX = "[UM redaction:"


def _placeholder(name: str, secret: str) -> str:
    return f"{PLACEHOLDER_PREFIX} name={name}; chars={len(secret)}]"


def redact_text(text: str, active: tuple[str, ...] | frozenset[str] = ALL) -> str:
    """Вырезать секреты, оставить префиксы (видно ЧТО было, не видно ЗНАЧЕНИЕ)."""
    unknown = set(active) - set(PATTERNS)
    if unknown:
        raise ValueError(f"unknown redaction patterns: {sorted(unknown)}")
    out = text
    for name in active:
        if name == "private_key" and "PRIVATE KEY" not in out.upper():
            continue  # prefilter: не будить ленивый DOTALL без якоря
        rx = PATTERNS[name]
        if name == "private_key":
            out = rx.sub(lambda m: _placeholder(name, m.group(0)), out)
            continue
        def _sub(m: re.Match[str], _name: str = name) -> str:
            # groupdict() содержит ВСЕ имена (неучаствовавшие = None) —
            # смотрим на значение, а не на наличие ключа.
            secret = m.groupdict().get("secret") or next(
                g for k, g in m.groupdict().items()
                if k.startswith("secret_") and g)
            suffix = m.groupdict().get("suffix") or ""
            return f"{m.group('prefix')}{_placeholder(_name, secret)}{suffix}"
        out = rx.sub(_sub, out)
    return out
