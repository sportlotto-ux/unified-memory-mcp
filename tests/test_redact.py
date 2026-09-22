"""Пункт 1 v0.4: redaction-гейт. Секреты не должны пережить ingest."""

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.redact import PLACEHOLDER_PREFIX, redact_text
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer

CASES = [
    ("api_key", "deploy with api_key=sk-SECRETAPIKEY1234567890 done", "sk-SECRETAPIKEY1234567890"),
    ("bearer_token", "header Bearer SECRETBEARERTOKEN9988776655 here", "SECRETBEARERTOKEN9988776655"),
    ("password_assignment", "login password=MySecretPass123 ok", "MySecretPass123"),
    ("private_key", "key -----BEGIN RSA PRIVATE KEY-----\nSECRETKEYBODY999\n-----END RSA PRIVATE KEY----- end",
     "SECRETKEYBODY999"),
]


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", context_tokens=10**9)
    assert cfg.redact_enabled  # default ON — публичный продукт
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


def _leaked(store, secret):
    """Сырой секрет нигде в текстовых таблицах."""
    tables = [("um_messages", "content"), ("um_facts", "body"),
              ("um_facts", "name"), ("um_summaries", "body")]
    return [t for t, c in tables
            if store.select(f"SELECT 1 FROM {t} WHERE {c} LIKE ?", (f"%{secret}%",))]


@pytest.mark.parametrize("name,text,secret", CASES)
def test_each_pattern_redacted(ing, name, text, secret):
    ing.remember_message("s", "user", text)
    rows = ing.store.select("SELECT content FROM um_messages")
    assert len(rows) == 1
    body = rows[0][0]
    assert secret not in body
    assert f"{PLACEHOLDER_PREFIX} name={name};" in body
    assert _leaked(ing.store, secret) == []


def test_fact_path_redacted(ing):
    ing.remember_fact("cred", "deploy", "api_key=sk-FACTSECRET1234567890")
    assert _leaked(ing.store, "sk-FACTSECRET1234567890") == []
    rows = ing.store.select("SELECT body FROM um_facts")
    assert PLACEHOLDER_PREFIX in rows[0][0]


def test_prefix_preserved_not_just_deleted(ing):
    ing.remember_message("s", "user", "use api_key=sk-KEEPME1234567890ABCDEF now")
    body = ing.store.select("SELECT content FROM um_messages")[0][0]
    assert body.startswith("use api_key=")  # видно ЧТО было
    assert "KEEPME" not in body


def test_compact_summary_inherits_redaction(ing):
    ing.remember_message("s", "user", "first note api_key=sk-SUMMARYSECRET1234567890 here")
    ing.remember_message("s", "user", "second note plain")
    ing.compact_session("s", keep_tail=0)
    assert _leaked(ing.store, "sk-SUMMARYSECRET1234567890") == []
    bodies = [r[0] for r in ing.store.select("SELECT body FROM um_summaries")]
    assert bodies and all("SUMMARYSECRET" not in b for b in bodies)


def test_disabled_passes_through(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", context_tokens=10**9, redact_enabled=False)
    store = Store(cfg)
    try:
        ing = Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
        ing.remember_message("s", "user", "api_key=sk-PASSTHROUGH1234567890")
        body = store.select("SELECT content FROM um_messages")[0][0]
        assert "sk-PASSTHROUGH1234567890" in body
    finally:
        store.close()


def test_idempotent():
    once = redact_text("api_key=sk-IDEMPOTENT1234567890!")
    assert redact_text(once) == once


def test_unknown_pattern_rejected():
    with pytest.raises(ValueError, match="unknown redaction"):
        redact_text("x", ("api_key", "telepathy"))


def test_empty_patterns_with_enabled_rejected(tmp_path):
    with pytest.raises(ValueError, match="UM_REDACT_PATTERNS"):
        Config(db_path=tmp_path / "d.db", redact_enabled=True, redact_patterns=())


def test_env_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_REDACT_ENABLED", "off")
    monkeypatch.setenv("UM_REDACT_PATTERNS", "bearer_token")
    c = Config(db_path=tmp_path / "d.db")
    assert c.redact_enabled is False
    assert c.redact_patterns == ("bearer_token",)
    monkeypatch.setenv("UM_REDACT_ENABLED", "maybe")
    with pytest.raises(ValueError, match="UM_REDACT_ENABLED"):
        Config(db_path=tmp_path / "d.db")
