"""Deterministic secret redaction at the ingest boundary (v0.4-п.1).

Порт каталога hermes-lcm ``ingest_protection._SENSITIVE_PATTERN_CATALOG``:
те же 4 имени, те же регулярки — чтобы секреты, пережившие LCM-гейт или
пришедшие напрямую в MCP, резались одинаково. Без LCM-наворотов: нет
sha-доказательств, нет таймаут-потоков, плейсхолдер необратимый.

Сверх LCM-каталога добавлены два высокоточных шаблона (18.10.2026):
``token_prefix`` (провайдерские ключи по префиксу: sk-, ghp_, AKIA, AIza, hf_,
xoxb- и т.п.) и ``jwt``. Причина — измеренный пробел: ключ формы ``sk-…``
переживал гейт, потому что все четыре LCM-шаблона требуют ключевого слова
(``api_key=``, ``Bearer``) рядом. Шаблоны с ложными срабатываниями на
безобидном (``long_hex``, ``long_base64`` — docker sha256, хэши сборок) лежат
в ``OPT_IN`` и включаются только явно через ``UM_REDACT_PATTERNS``.

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
    # Провайдерские ключи опознаются по префиксу, а не по присваиванию: такой
    # ключ узнаётся и без `api_key=` рядом. Пробел был измерен 18.10.2026 —
    # в захваченной стенограмме строка формы `sk-…` пережила гейт (в проекте
    # есть и запись аудита: «13 strings in tests/… match the shape of a live
    # API key»). По умолчанию включён: точность высокая, ложных срабатываний
    # на обычном тексте нет.
    "token_prefix": re.compile(
        r"\b(?P<secret>"
        r"sk-[A-Za-z0-9_-]{16,}"                      # OpenAI/совместимые
        r"|sk_live_[A-Za-z0-9]{16,}|pk_live_[A-Za-z0-9]{16,}"          # Stripe
        r"|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
        r"|ghu_[A-Za-z0-9]{20,}|ghs_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"                               # GitHub
        r"|glpat-[A-Za-z0-9_-]{16,}"                                   # GitLab
        r"|xox[baprs]-[A-Za-z0-9-]{10,}"                               # Slack
        r"|AKIA[0-9A-Z]{16}"                                           # AWS
        r"|AIza[0-9A-Za-z_-]{20,}"                                     # Google
        r"|gsk_[A-Za-z0-9]{20,}"                                       # Groq
        r"|hf_[A-Za-z0-9]{20,}"                                        # HuggingFace
        r"|npm_[A-Za-z0-9]{20,}"                                       # npm
        r"|pypi-[A-Za-z0-9_-]{20,}"                                    # PyPI
        r"|dop_v1_[A-Za-z0-9]{20,}"                                    # DigitalOcean
        r"|r8_[A-Za-z0-9]{20,}"                                        # Replicate
        r")",
    ),
    # JWT: три base64url-сегмента, начинается с eyJ ({"alg"…). Часто единственный
    # след сессии — токен в заголовке или в тексте без ключевого слова рядом.
    "jwt": re.compile(
        r"\b(?P<secret>eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,})",
    ),
}

# Опциональные (НЕ по умолчанию): высокая полнота, но ложные срабатывания на
# безобидном — docker image sha256 и хэши сборок выглядят как «энтропия».
# Включаются явно: UM_REDACT_PATTERNS=long_hex,long_base64
OPT_IN: dict[str, re.Pattern[str]] = {
    "long_hex": re.compile(r"\b(?P<secret>[0-9a-fA-F]{40,})\b"),
    "long_base64": re.compile(
        r"\b(?P<secret>(?=[A-Za-z0-9+/]{40,}={0,2}\b)(?=[^\s]*[A-Z])(?=[^\s]*[a-z])"
        r"(?=[^\s]*[0-9])[A-Za-z0-9+/]{40,}={0,2})\b"),
}

# KNOWN — всё, что можно назвать в настройках; ALL — набор по умолчанию.
KNOWN: dict[str, re.Pattern[str]] = {**PATTERNS, **OPT_IN}
ALL = tuple(PATTERNS)

PLACEHOLDER_PREFIX = "[UM redaction:"


def _placeholder(name: str, secret: str) -> str:
    return f"{PLACEHOLDER_PREFIX} name={name}; chars={len(secret)}]"


def redact_text(text: str, active: tuple[str, ...] | frozenset[str] = ALL) -> str:
    """Вырезать секреты, оставить префиксы (видно ЧТО было, не видно ЗНАЧЕНИЕ)."""
    unknown = set(active) - set(KNOWN)
    if unknown:
        raise ValueError(f"unknown redaction patterns: {sorted(unknown)}")
    out = text
    for name in active:
        if name == "private_key" and "PRIVATE KEY" not in out.upper():
            continue  # prefilter: не будить ленивый DOTALL без якоря
        rx = KNOWN[name]
        if name == "private_key":
            out = rx.sub(lambda m: _placeholder(name, m.group(0)), out)
            continue

        def _sub(m: re.Match[str], _name: str = name) -> str:
            # groupdict() содержит ВСЕ имена (неучаствовавшие = None) —
            # смотрим на значение, а не на наличие ключа.
            secret = m.groupdict().get("secret") or next(
                g for k, g in m.groupdict().items()
                if k.startswith("secret_") and g)
            # prefix/suffix есть не у всех шаблонов (у провайдерских ключей
            # ключевое слово не требуется) — отсутствие значит пустую строку.
            prefix = m.groupdict().get("prefix") or ""
            suffix = m.groupdict().get("suffix") or ""
            return f"{prefix}{_placeholder(_name, secret)}{suffix}"

        out = rx.sub(_sub, out)
    return out
