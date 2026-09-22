# ADR-001: Типизированные связи памяти (`um_links`)

- **Статус:** Accepted (v0.7)
- **Дата:** 2026-09-22
- **Контекст кода:** `store.py:87-97` (`um_edges`), `recall.py:169-185` (граф-рука),
  `store.py:1248+` (`neighbors`)

## Контекст

- Наш граф `um_edges(subject_id, predicate, object_id)` ссылается **строго** на
  `um_entities`. Создаётся только сайд-эффектом `mem_fact(subject, predicate, obj)`;
  читается `neighbors()`/граф-рукой — **строго 1-hop**.
- Связать «сообщение подтверждает факт», «факт противоречит факту», «факт вытекает
  из факта» сейчас **невозможно в принципе**.
- Апстрим hermes-lcm моделирует это боевым образом (`assertion_store.py:47-55`,
  `db_bootstrap.py:1763-1839`): `lcm_assertion_sources` (assertion ← сообщения-
  источники через `source_content_sha256`) и `lcm_assertion_relations`
  (`from_assertion_id`, `relation_type`, 8 типов). Потребность — не гипотетическая.
- Сделать `um_edges` полиморфной = тронуть entity-индексы, FTS-синк, `neighbors` и
  `as_of`-фильтры ради концов, которым ни FTS, ни вектора не нужны.

## Решение

### D1. Отдельная traversal-only таблица `um_links`
Endpoints generic: `(src_table, src_id) → (dst_table, dst_id)`. Ни FTS, ни векторов.
Новая `CREATE TABLE` — **ноль изменений горячего пути** `um_edges`. Идентичность узла
обхода = кортеж `(table, id)`.

### D2. Словарь `rel` — 4 типа, закрытый `CHECK`
`supports | contradicts | supersedes | derives_from`.
- `supports` — message→fact (порт `assertion_sources`);
- `contradicts` — вердикт хоста поверх verdict-free кандидатов v0.6 (спеллинг LCM
  ради будущей совместимости);
- `supersedes` — fact→fact, дополняет слот-цепочку;
- `derives_from` — lineage.

Отвергнуто: полный LCM-набор из 8 (часть — почти синонимы: `narrows`/`weakens`,
`reverses`/`contradicts`; тянет 8 чужих семантик и тестов). Отвергнуто `mentions`
(слабая семантика → свалка «всё упоминает всё», нетестируемо). Расширение — миграцией.

### D3. Дедуп живых связей
`UNIQUE(src_table, src_id, dst_table, dst_id, rel, owner) WHERE valid_until = 0` —
та же семантика «одно живое значение», что `ux_um_facts_live` (v0.5).

### D4. Валидация концов — в write-path
SQLite не даёт FK на generic-концы ⇒ `mem_link` обязан проверить существование
`src`/`dst` и совпадение `owner`; иначе — отказ. Висячие ссылки запрещены.
Тест на каждый мусорный конец.

### D5. Семантика BFS
- Узел = `(table, id)`; visited-set по узлам; depth-cap = `hops`.
- Обход по объединению `um_edges` (**роль `rel` играет `predicate`**) и `um_links`
  (**роль `rel` — `rel`**). Зафиксировано здесь.
- `hops=1` (дефолт) = текущее поведение ⇒ **ноль регрессий**.
- `as_of` и `include_expired` режут **обе** руки; `owner` — тоже (тест на срез сквозь линк).

### D6. Жизненный цикл
- Удаление: `mem_forget(kind=link)` — **жёсткое** (симметрично GDPR-hatch фактов/рёбер).
- Истечение/reopen: `mem_update(kind=link, valid_until=...)` (`0` = reopen).
- `rel` не редактируется — замена связи = новая связь.
- Без этого таблица только растёт.

### D7. `mem_batch` — атомарный, `dry_run` opt-in
- Ops строго `{remember_fact | update | forget}` (закрытый контракт, не «любой op»).
- Один `BEGIN`; любая ошибка → rollback, ответ с per-op статусом.
- `dry_run=true` — savepoint-предпросмотр тем же кодом; **default = apply** (batch —
  мутационный тул, как `mem_fact`/`mem_update`, пишет безусловно).
- Cap на количество ops и суммарный размер.

### D8. Экспорт — `mem_doctor(mode=export)`
Read-only JSON-дамп всех таблиц + `schema_version`; семейство maintenance, счётчик
тулов **не растёт**. Импорта нет: источника-донора не существует (второго инстанса
нет; Mem0/Hindsight в пайплайне не водятся) — YAGNI.

## Последствия
- **+** Связи message↔fact и fact↔fact без регрессий горячего пути.
- **+** `contradicts` закрывает конфликт-петлю v0.6: кандидат → суд хоста →
  **записанный** линк. Без эвристики, без шума (доверие не подрывается).
- **−** Новая поверхность write-path (валидация концов) → +~20 тестов.
- **−** 4 типа могут потребовать расширения — осознанный YAGNI, миграция дешёвая.
