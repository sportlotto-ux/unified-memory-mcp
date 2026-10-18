"""Пункт 1 v0.4: redaction-гейт. Секреты не должны пережить ingest.

Провайдерские ключи (``sk-``, ``ghp_``, ``AKIA`` …) были пробелом: все четыре
LCM-шаблона требуют ключевого слова рядом, поэтому ``OPENAI_API_KEY=sk-…``
проходил гейт. С 18.10.2026 их ловит ``token_prefix``, JWT — ``jwt``; фикстуры
ниже проверяют это явно, плюс что безобидное (короткие git-хэши, sha256 образа)
не режется.
"""

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.redact import PLACEHOLDER_PREFIX, redact_text
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer

CASES = [
    ("api_key", "deploy with api_key=FAKE_APIKEY_1234567890 done", "FAKE_APIKEY_1234567890"),
    ("bearer_token", "header Bearer SECRETBEARERTOKEN9988776655 here", "SECRETBEARERTOKEN9988776655"),
    ("password_assignment", "login password=MySecretPass123 ok", "MySecretPass123"),
    ("private_key", "key -----BEGIN RSA PRIVATE KEY-----\nSECRETKEYBODY999\n-----END RSA PRIVATE KEY----- end",
     "SECRETKEYBODY999"),
    # Провайдерские ключи: ключевого слова рядом нет, опознаются по префиксу.
    ("token_prefix", "env OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx12 done",
     "sk-abcdefghijklmnopqrstuvwx12"),
    ("token_prefix", "remote https://ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345@github.com/a/b",
     "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
    ("token_prefix", "aws profile AKIAIOSFODNN7EXAMPLE для бэкапа",
     "AKIAIOSFODNN7EXAMPLE"),
    ("token_prefix", "slack bot xoxb-1234567890-abcdefghijkl в логе",
     "xoxb-1234567890-abcdefghijkl"),
    ("token_prefix", "huggingface hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345 accepted",
     "hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
    ("jwt", "auth eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U ok",
     "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
     "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
     "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
]

# Безобидное, что резать НЕЛЬЗЯ: иначе память теряет полезное содержимое.
BENIGN = [
    "фикс зафиксирован в коммитах 53ca400 и 441a709",
    "образ production sha256:7f5c4b2a9d1e8f3a6b0c5d4e7f8a9b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a",
    "порт 8127, локальные эмбеддинги, vps4 130.61.174.27",
    "почта user@example.com и ссылка https://example.com/a/b?c=d",
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
    ing.remember_fact("cred", "deploy", "api_key=FAKE_FACTSECRET_1234567890")
    assert _leaked(ing.store, "FAKE_FACTSECRET_1234567890") == []
    rows = ing.store.select("SELECT body FROM um_facts")
    assert PLACEHOLDER_PREFIX in rows[0][0]


def test_update_fact_path_redacted(ing):
    fid = ing.remember_fact("cred", "deploy", "old body")
    ing.update_fact(fid, body="new api_key=FAKE_UPDATESECRET_1234567890")
    assert _leaked(ing.store, "FAKE_UPDATESECRET_1234567890") == []
    body = ing.store.select(
        "SELECT body FROM um_facts WHERE valid_until=0")[0][0]
    assert PLACEHOLDER_PREFIX in body


def test_prefix_preserved_not_just_deleted(ing):
    ing.remember_message("s", "user", "use api_key=FAKE_KEEPME_1234567890ABCDEF now")
    body = ing.store.select("SELECT content FROM um_messages")[0][0]
    assert body.startswith("use api_key=")  # видно ЧТО было
    assert "KEEPME" not in body


def test_compact_summary_inherits_redaction(ing):
    ing.remember_message("s", "user", "first note api_key=FAKE_SUMMARYSECRET_1234567890 here")
    ing.remember_message("s", "user", "second note plain")
    ing.compact_session("s", keep_tail=0)
    assert _leaked(ing.store, "FAKE_SUMMARYSECRET_1234567890") == []
    bodies = [r[0] for r in ing.store.select("SELECT body FROM um_summaries")]
    assert bodies and all("SUMMARYSECRET" not in b for b in bodies)


def test_disabled_passes_through(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", context_tokens=10**9, redact_enabled=False)
    store = Store(cfg)
    try:
        ing = Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
        ing.remember_message("s", "user", "api_key=FAKE_PASSTHROUGH_1234567890")
        body = store.select("SELECT content FROM um_messages")[0][0]
        assert "FAKE_PASSTHROUGH_1234567890" in body
    finally:
        store.close()


def test_idempotent():
    once = redact_text("api_key=FAKE_IDEMPOTENT_1234567890!")
    assert redact_text(once) == once


@pytest.mark.parametrize("text", BENIGN)
def test_benign_text_not_masked(text):
    """Ложное срабатывание хуже пропуска: полезное содержимое должно уцелеть."""
    assert redact_text(text) == text


def test_opt_in_patterns_off_by_default_on_when_requested():
    digest = "sha256:" + "ab12" * 16        # docker image digest / хэш сборки
    assert redact_text(digest) == digest
    assert PLACEHOLDER_PREFIX in redact_text(digest, ("long_hex",))
    blob = "ABCdef123456" * 4               # база64-подобный блоб
    assert redact_text(blob) == blob
    assert PLACEHOLDER_PREFIX in redact_text(blob, ("long_base64",))


def test_opt_in_pattern_accepted_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_REDACT_PATTERNS", "token_prefix,long_hex")
    assert Config(db_path=tmp_path / "d.db").redact_patterns == ("token_prefix", "long_hex")
    monkeypatch.setenv("UM_REDACT_PATTERNS", "token_prefix,telepathy")
    with pytest.raises(ValueError, match="UM_REDACT_PATTERNS"):
        Config(db_path=tmp_path / "d.db")


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
