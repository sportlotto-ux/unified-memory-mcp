# MIGRATION_PLAN

## Этап 0 ✅ — скелет + embeddings
Готово: структура, `embeddings.py` (fastembed + реестр + dim-guard),
`store.py` (DDL-план), `server.py` (поверхность API).

## Этап 1 — единый стор
1. `open_db` боевое: WAL, sqlite-vec опционально, `um_meta` stamp.
2. Миграция `mnemosyne.db` → `um_*` (маппинг 30 таблиц, VIEW-совместимость).
3. Миграция `lcm.db` → `um_*`.
4. Критерий: `mem_status` на объединённой БД, recall-паритет пробами
   (запросы-фрагменты самих воспоминаний, дискордантные пары, биномиальный тест —
   стенд как в `embed_memory_bench.py`, не «проценты»).

## Этап 2 — единый ingest
Один пайплайн на сообщение: `um_messages` → DAG-нода (LCM-compaction) +
memory-записи (canonical/episodic по правилам). Убрать двойную запись.
Отдельно решить пару `assertion_* vs canonical` (см. MODULE_MAP).

## Этап 3 — роутер + MCP
`router.py` (RRF FTS+vectors), `server.py` на `mcp.server.Server` (stdio),
8 тулов из TOOL_MAP. Cloud embedding-бэкенды дотянуть здесь.

## Этап 4 — чистка
Удалить адаптеры, выкинуть непереехавшее (см. MODULE_MAP «Не переезжает»),
заморозить VIEW-совместимость → дропнуть.

## Риски
1. **Форк-расхождение**: апстримы живые. Митигация — этапы 1–3 адаптерами
   поверх вендоренных снапшотов с фиксацией версий; перенос логики только на 4.
2. **Creds в памяти**: mnemosyne хранит credentials-категорию; unified recall
   расширяет поверхность. Нужен `sensitive-patterns` гейт как в LCM
   (`LCM_SENSITIVE_PATTERNS_ENABLED` → `UM_SENSITIVE_PATTERNS_ENABLED`).
3. **Bloat-инжекция**: canonical-bloat (полные SKILL.md в каждом ходе) не должен
   переехать в новый стор — кап `importance`/размера на уровне `um_facts`
   (уже в DDL: нет жёстких 0.95).
4. **Write-lock**: два писателя (gateway + cron sleep) уже дают
   `database is locked`. Один стор = один lock-домен: busy-timeout + WAL +
   single-writer очередь в `store.py` обязательны до продакшна.
