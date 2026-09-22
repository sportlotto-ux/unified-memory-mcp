# MIGRATION_PLAN — статус v0.3.2

## Этап 0 ✅ — скелет + embeddings
## Этап 1 ✅ — единый стор
`open_db`, WAL + RLock single-writer, `um_meta` stamp, dim-guard. VIEW-совместимость
отложена (предрелиз, внешних потребителей нет).
## Этап 2 ✅ — единый ingest
Одно сообщение → `um_messages` + вектора; факты + триплеты → `um_facts`/`um_entities`/`um_edges`.
Пара `assertion_* vs canonical` решена в пользу canonical-слотов (assertion-семья — в mem_evidence, отложено).
## Этап 3 ✅ — роутер + MCP
`recall.py` (FTS + vectors + граф + RRF), 9 тулов `mem_*` на FastMCP/MCPServer-шиме.
Cloud embedding-бэкенды не тянули (только fastembed + FTS-only деградация).
## Этап 4 🟡 — чистка и остаток
Адаптеров не возникло (ядро писалось с нуля по картам — MODULE_MAP/TOOL_MAP актуальны
как разбор апстримов). Остаток: `mem_evidence`, temporal rollups, sqlite-vec,
redaction гейт, user-isolation, `UM_VEC_TYPE`. См. «Известные ограничения» в README.

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
