"""Приёмка патча ширины плеч: порядок выдачи не должен зависеть от limit.

До патча плечи брали `limit * 2` кандидатов, поэтому один и тот же запрос
с разным `limit` давал разный топ-1 (замер 18.10.2026: стабильность 9/19 на
реальных данных — с limit=5 посторонние факты получали бонус двух плеч и
вытесняли точное попадание FTS).
"""

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.recall import Router
from unified_memory.store import Store


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=tmp_path / "a.db",
                  archive_path=tmp_path / "a.archive.db",
                  context_tokens=10**9)


def _seeded(cfg, n=30):
    store = Store(cfg)
    ing = Ingest(store, FakeBackend(), cfg=cfg)
    for i in range(n):
        ing.remember_fact("t", f"f{i}", f"сервис номер {i} слушает порт 8127")
    return store


def test_recall_arm_limit_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        Config(db_path=tmp_path / "x.db", archive_path=tmp_path / "y.db",
               recall_arm_limit=0)


def test_arm_limit_is_reported_and_follows_max(cfg):
    store = _seeded(cfg, 5)
    r = Router(store, FakeBackend(), cfg)
    r.recall("порт 8127", scope="facts", limit=3, diagnostics=True,
             expire_working=False)
    assert r.last_stats["diagnostics"]["arm_limit"] == cfg.recall_arm_limit
    r.recall("порт 8127", scope="facts", limit=cfg.recall_arm_limit + 10,
             diagnostics=True, expire_working=False)
    assert r.last_stats["diagnostics"]["arm_limit"] == cfg.recall_arm_limit + 10
    store.close()


def test_top1_is_stable_across_limits(cfg):
    """В пределах ширины плеча порядок не зависит от размера страницы.

    Выше `recall_arm_limit` плечи неизбежно расширяются («большая страница»),
    и это уже другой режим — он проверяется отдельным тестом.
    """
    store = _seeded(cfg, 30)
    r = Router(store, FakeBackend(), cfg)
    limits = [l for l in (3, 5, 10, 20) if l <= cfg.recall_arm_limit]
    for scope in ("facts", "all"):
        tops = []
        for lim in limits:
            hits = r.recall("сервис слушает порт", scope=scope, limit=lim,
                            expire_working=False)
            tops.append(hits[0].owner_id if hits else None)
        assert len(set(tops)) == 1, f"{scope}: топ-1 плывёт {tops}"
    store.close()


def test_arm_breadth_does_not_shrink_with_limit(cfg):
    """Малая страница не должна сужать плечи — иначе теряется контекст ранга."""
    store = _seeded(cfg, 40)
    r = Router(store, FakeBackend(), cfg)
    broad = {}
    for lim in (1, 3, cfg.recall_arm_limit):
        r.recall("сервис порт 8127", scope="all", limit=lim, diagnostics=True,
                 expire_working=False)
        broad[lim] = r.last_stats["diagnostics"]["arms"]
    assert broad[1] == broad[3] == broad[cfg.recall_arm_limit], broad
    store.close()
