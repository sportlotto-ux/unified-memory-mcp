"""Токенизатор: детерминированная RU-aware эвристика + опциональный tiktoken.

Внешний аудит: `test_auto_compact_on_pressure` проходил только там, где случайно
стоял tiktoken (не объявлен в зависимостях) — на чистой инсталляции компакшн не
срабатывал, а математика давления молча различалась между окружениями.
"""

from unified_memory import store


def test_heuristic_ru_aware_matches_spec():
    txt = "сообщение 0 про бюджет и планы" * 2
    # ascii=12, non-ascii=48 → 3 + 24
    assert store.estimate_tokens(txt) == 27
    assert store.estimate_tokens(txt) > len(txt) // 4  # старая эвристика занижала


def test_heuristic_ascii_quarter():
    assert store.estimate_tokens("abcdefgh") == 2   # 8 ASCII → (8+3)//4
    assert store.estimate_tokens("") == 1           # не 0


def test_uses_encoder_when_present(monkeypatch):
    class FakeEnc:
        def encode(self, _s):
            return list(range(7))

    monkeypatch.setattr(store, "_TIKTOKEN", {"enc": FakeEnc(), "tried": True})
    assert store.estimate_tokens("любой текст") == 7  # encoder важнее эвристики


def test_missing_tiktoken_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(store, "_TIKTOKEN", {"enc": None, "tried": False})
    monkeypatch.setattr(store, "_tiktoken_enc", lambda: None)
    assert store.estimate_tokens("abc") >= 1


def test_compact_pressure_independent_of_tiktoken(tmp_path):
    """Тот самый падающий тест, но без зависимости от окружения."""
    from fake_backend import FakeBackend
    from unified_memory.config import Config
    from unified_memory.ingest import Ingest
    from unified_memory.store import Store
    from unified_memory.summarize import ExtractiveSummarizer

    store._TIKTOKEN.update(enc=None, tried=True)  # явно эвристика
    cfg = Config(db_path=tmp_path / "w.db", context_tokens=200,
                 compact_threshold=0.5, fresh_tail=2, dag_fanin=2,
                 assembly_budget=100)
    st = Store(cfg)
    ing = Ingest(st, FakeBackend(), ExtractiveSummarizer(), cfg)
    try:
        for i in range(6):
            r = ing.remember_message(
                "s1", "user", f"сообщение {i} про бюджет и планы" * 2)
        assert r["compaction"]["status"] == "compacted"
        assert st.stats()["um_summaries"] >= 1
        assert len(st.session_messages("s1", limit=100)) == 6  # lossless
    finally:
        st.close()
