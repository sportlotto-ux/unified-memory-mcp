"""Hermetic suite: ambient UM_* env never leaks into tests.

Фон: Config читает env при инстанциации — шелл контрибьютора с
UM_REDACT_ENABLED=off / UM_EMBEDDING_BACKEND=openai / UM_VEC_INDEX=off
молча ломал 12 тестов.

Две точки утечки:
1. import-time: `embeddings.DEFAULT_MODEL` читает UM_EMBEDDING_MODEL
   на импорте модуля — чистим ДО сбора тестов (на импорте conftest).
2. per-test: Config()/load() читают env при создании — autouse-фикстура.

UM_LIVE_* не трогаем: это opt-in гейты (skipif на collection), не конфиг.
Тесты, которым env нужен, выставляют его сами через monkeypatch (авто-откат).
"""

import os

import pytest

_PREFIX = "UM_"
_KEEP = ("UM_LIVE_",)


def _is_ambient(key: str) -> bool:
    return key.startswith(_PREFIX) and not key.startswith(_KEEP)


for _k in [k for k in os.environ if _is_ambient(k)]:
    os.environ.pop(_k, None)


@pytest.fixture(autouse=True)
def _clean_um_env(monkeypatch):
    for key in [k for k in os.environ if _is_ambient(k)]:
        monkeypatch.delenv(key, raising=False)
