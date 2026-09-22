"""v0.8 E21: redact-fuzz (hypothesis). Только dev-зависимость (см. extra `dev`).

Свойства:
- каталог не находит совпадений в выходе (redact → нет матчей ни одного паттерна);
- идемпотентность: redact(redact(x)) == redact(x);
- deadline против ReDoS на злом корпусе (вложенные квантификаторы, длинные раны,
  незакрытый PEM, юникод).

Без hypothesis тест скипается (в CI по умолчанию его нет → importorskip).
"""

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from unified_memory.redact import PATTERNS, redact_text  # noqa: E402

_TEXT = st.text(max_size=240)
_SET = settings(max_examples=200, deadline=500,
                suppress_health_check=[HealthCheck.too_slow])


@_SET
@given(_TEXT)
def test_catalog_finds_nothing_in_output(text):
    out = redact_text(text)
    for name, rx in PATTERNS.items():
        assert rx.search(out) is None, (name, repr(out))


@_SET
@given(_TEXT)
def test_idempotent(text):
    once = redact_text(text)
    assert redact_text(once) == once


_REDOS = [
    "-----BEGIN RSA PRIVATE KEY-----" + "A" * 50_000,      # незакрытый PEM
    "A" * 100_000,                                          # длинный ран
    "Bearer " + "A" * 100_000,
    "password=" + "\"" * 5_000,
    "api_key=" + "a." * 30_000,
    "-----BEGIN  PRIVATE KEY-----" + "\n" * 20_000,
    "\u0301" * 20_000 + "🔥" * 5_000,                       # юникод/комбайнинг
    ("secret_key=" + "x" * 40 + " " * 1_000) * 20,
]


@settings(max_examples=len(_REDOS), deadline=500)
@given(text=st.sampled_from(_REDOS))
def test_redos_deadline(text):
    """Злой корпус: redact обязан уложиться в deadline (0.5s), не уйти в ReDoS."""
    out = redact_text(text)
    for name, rx in PATTERNS.items():
        assert rx.search(out) is None, name
