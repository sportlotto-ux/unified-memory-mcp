"""v0.8 E20: golden-eval — базлайн качества recall (до любых скоринговых правок).

Guardrails (план v0.8):
1. Метрика ДО фикстур: hit-in-top-N (бинарь) + MRR; порог зафиксирован числом.
2. Детерминизм: часы заморожены на NOW, created_at/валидность привязаны к NOW —
   цифры не зависят от дня прогона.
3. Дисциплина базлайна: тест зелёный на ТЕКУЩЕМ поведении. Следующий ранжирующий
   PR либо держит его зелёным, либо правит фикстуры громко (прецедент A4).
4. Не хрупко: ассерты на вхождение ожидаемого id в топ-N, НЕ на полный порядок.
5. Изоляция: стор строится в tmp_path, прод-БД не трогается.

Покрытие плеч: FTS-дословно, вектор-парафраз, граф-хоп, истёкшее-скрыто.

Базлайн (измерено на v0.7.3, 24 запроса): hit@3 = 1.000, MRR = 0.896.
Пороги (запас под шум синтетики): hit@3 ≥ 0.95, MRR ≥ 0.85. Любой
ранжирующий PR обязан их держать либо громко править фикстуры (прецедент A4).
"""

import time as _time
from dataclasses import replace

import pytest

from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.recall import Router
from unified_memory.store import Store
from fake_backend import LexicalBackend

NOW = 1_700_000_000.0  # фиксированный момент: created_at = NOW у всех записей
TOP_N = 3
MIN_HIT_RATE = 0.95
MIN_MRR = 0.85


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """Изолированный стор в tmp + замороженные часы (детерминизм)."""
    monkeypatch.setattr(_time, "time", lambda: NOW)
    cfg = Config(db_path=tmp_path / "eval.db", archive_path=tmp_path / "eval-arch.db")
    st = Store(cfg)
    be = LexicalBackend()
    ing = Ingest(st, be, cfg=cfg)
    r = Router(st, be, cfg=cfg)
    ids: dict[str, int] = {}

    def fact(key, cat, name, body):
        ids[key] = ing.remember_fact(cat, name, body)
        return ids[key]

    # --- корпус: RU-факты по темам (ни один не секрет → redaction не мешает) ---
    fact("subs", "product", "subscription",
         "Подписка Pro стоит 990 рублей в месяц, отмена в любой момент")
    fact("refund", "policy", "refund",
         "Возврат средств в течение 14 дней после покупки, деньги приходят на карту")
    fact("delivery", "logistics", "delivery",
         "Доставка курьером за два дня по Москве и области")
    fact("api_limit", "tech", "api_limit",
         "API принимает не более 100 запросов в секунду на один ключ")
    fact("database", "tech", "database",
         "Основное хранилище — SQLite в режиме WAL, единый файл базы")
    fact("backup", "ops", "backup",
         "Резервная копия снимается каждую ночь в три часа")
    fact("support", "support", "hours",
         "Поддержка отвечает с девяти до двадцати одного по Москве")
    fact("region", "business", "region", "Продажи идут в России и Казахстане")
    fact("company", "business", "company", "Компания Ромашка основана в 2019 году")
    fact("cheese", "product", "cheese",
         "Сыр Ромашка — твёрдый сорт с ореховым вкусом")
    fact("factory", "business", "factory", "Завод Ромашка находится в Подмосковье")
    fact("price_now", "product", "price_current",
         "Актуальная цена подписки — 990 рублей с первого января")
    fact("price_old", "product", "price_old",
         "Старая цена подписки — 1490 рублей до тридцать первого декабря")
    fact("security", "tech", "security",
         "Пароли хранятся в виде хешей, двухфакторная аутентификация обязательна")
    fact("export", "tech", "export",
         "Экспорт данных доступен в форматах JSON и CSV по запросу в поддержку")
    fact("trial", "product", "trial",
         "Пробный период длится 14 дней без привязки карты")
    # хоп-пара без общих токенов (иначе MMR-диверсификация демотивирует цель)
    fact("hub", "graph", "hub",
         "Платформа Аврора объединяет несколько сервисов")
    fact("spoke", "graph", "spoke",
         "Модуль Радара отвечает за оповещения клиентов")
    fact("spoke2", "graph", "spoke2",
         "Панель Диспетчера показывает состояние задач")

    L = lambda a, b, rel, w=1.0: st.link("um_facts", ids[a], "um_facts", ids[b], rel,
                                         weight=w)
    L("company", "cheese", "supports")          # граф-хоп: company → cheese
    L("factory", "cheese", "derives_from")      # граф-хоп: factory → cheese
    L("hub", "spoke", "supports")               # хоп без общих токенов
    L("spoke", "spoke2", "derives_from")        # цепочка hub → spoke → spoke2
    fact("w_a", "weights", "w_a", "якорь весов уникальный")
    fact("w_b", "weights", "w_b", "лёгкая связь")
    fact("w_c", "weights", "w_c", "тяжёлая связь")
    L("w_a", "w_b", "supports", 1.0)
    L("w_a", "w_c", "supports", 3.0)
    st.update_fact(ids["price_old"], valid_until=NOW - 1)  # истёк: скрыт по умолчанию

    yield st, r, ids
    st.close()


# query, scope, hops, expect (любой из — в топ-N), forbid (не должно быть вовсе)
CASES = [
    # FTS-дословно
    ("возврат средств", "facts", 1, ("refund",), []),
    ("резервная копия ночь", "facts", 1, ("backup",), []),
    ("двухфакторная аутентификация", "facts", 1, ("security",), []),
    ("экспорт CSV", "facts", 1, ("export",), []),
    ("пробный период", "facts", 1, ("trial",), []),
    ("лимит запросов ключ", "facts", 1, ("api_limit",), []),
    ("SQLite WAL", "facts", 1, ("database",), []),
    ("Компания Ромашка основана", "facts", 1, ("company",), []),
    ("Завод Ромашка", "facts", 1, ("factory",), []),
    # вектор-парафраз (слов запроса дословно в теле нет)
    ("сколько платить за тариф", "facts", 1, ("subs", "price_now"), []),
    ("как получить компенсацию за покупку", "facts", 1, ("refund",), []),
    ("как быстро доставят заказ", "facts", 1, ("delivery",), []),
    ("во сколько работает техподдержка", "facts", 1, ("support",), []),
    ("где физически находится производство", "facts", 1, ("factory",), []),
    ("из чего делают сыр", "facts", 1, ("cheese",), []),
    ("география продаж", "facts", 1, ("region",), []),
    ("где хранится информация приложения", "facts", 1, ("database",), []),
    # граф-хоп (hops>1 через um_links)
    ("Платформа Аврора", "facts", 2, ("spoke",), []),
    ("Модуль Радара", "facts", 3, ("spoke2",), []),
    ("Ромашка основана", "facts", 2, ("cheese",), []),
    # истёкшее-скрыто (price_old expired)
    ("цена подписки", "facts", 1, ("price_now",), "price_old"),
    ("старая цена 1490", "facts", 1, ("price_now", "subs"), "price_old"),
    # scope=all — те же плечи в общем режиме
    ("сыр Ромашка", "all", 2, ("cheese",), []),
    ("куда приходят деньги при возврате", "all", 1, ("refund",), []),
]


def _ranked(r, ids, query, scope, hops, limit=5):
    rev = {v: k for k, v in ids.items()}
    return [rev.get(h.owner_id, f"?{h.owner_id}")
            for h in r.recall(query, scope=scope, limit=limit, hops=hops)]


def test_eval_baseline(kb):
    st, r, ids = kb
    hit = 0
    rr_sum = 0.0
    for query, scope, hops, expect, forbid in CASES:
        ranked = _ranked(r, ids, query, scope, hops)
        top = ranked[:TOP_N]
        assert any(e in top for e in expect), \
            f"{query!r}: ожидался {expect} в топ-{TOP_N}, есть {top}"
        for bad in forbid:
            assert bad not in ranked, f"{query!r}: истёкший {bad} просочился: {ranked}"
        ranks = [ranked.index(e) for e in expect if e in ranked]
        if ranks:
            hit += 1
            rr_sum += 1.0 / (min(ranks) + 1)
    n = len(CASES)
    hit_rate, mrr = hit / n, rr_sum / n
    assert hit_rate >= MIN_HIT_RATE, f"hit@{TOP_N} {hit_rate:.3f} < {MIN_HIT_RATE}"
    assert mrr >= MIN_MRR, f"MRR {mrr:.3f} < {MIN_MRR}"


def test_eval_importance_case_is_bounded(kb, monkeypatch):
    """P1.4 ranking case: high importance adjusts, but does not override relevance."""
    st, r, _ = kb
    exact_body = "importancecase"
    weak_body = exact_body + " " + "distractor " * 40
    exact = st.add_fact("importance", "exact", exact_body, importance=0.0)
    weak = st.add_fact("importance", "weak", weak_body, importance=0.95)
    st.add_vector("um_facts", exact, r.backend.embed_docs([exact_body])[0],
                  r.backend.model_name)
    st.add_vector("um_facts", weak, r.backend.embed_docs([weak_body])[0],
                  r.backend.model_name)
    cfg = replace(r.cfg, recency_halflife_days=0.0, mmr_lambda=1.0,
                  importance_weight=0.25)
    monkeypatch.setattr(st, "fts_search", lambda *args, **kwargs: [])
    ranked_router = Router(st, r.backend, cfg)
    ranked = ranked_router.recall(
        exact_body, scope="facts", limit=2, diagnostics=True)
    assert [h.owner_id for h in ranked] == [exact, weak]
    importance = ranked_router.last_stats.get("diagnostics", {}).get("importance", {})
    assert importance["weight"] == pytest.approx(0.25)


def test_eval_weight_orders_within_arm(kb):
    """A1-инвариант: вес линка умножает графовый скор (сырой BFS-плечо).

    Публичный RRF-путь здесь не годится: разреженный синтетический backend
    отдаёт zero-cos кандидатов, которые становятся BFS-сидами и глушат эмиссию.
    В проде нулевых косинусов нет; инвариант веса проверяем на граф-плече.
    """
    st, r, ids = kb
    gd = {(h.owner_table, h.owner_id): h.score
          for h in r._graph_bfs("якорь весов уникальный", "all", "", 40, "", False,
                                None, 2, "", [("um_facts", ids["w_a"])])}
    assert gd[("um_facts", ids["w_c"])] == pytest.approx(3.0)
    assert gd[("um_facts", ids["w_b"])] == pytest.approx(1.0)


def test_eval_deterministic(kb):
    """Прогон дважды даёт идентичный порядок (замороженные часы)."""
    st, r, ids = kb
    for query, scope, hops, _, _ in CASES:
        assert _ranked(r, ids, query, scope, hops) == _ranked(r, ids, query, scope, hops)


def test_eval_isolated_store(kb, tmp_path):
    """Eval-стор живёт в tmp, а не в прод-БД."""
    st, r, ids = kb
    assert str(tmp_path) in st._db_path
