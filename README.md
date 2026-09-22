# hermes-unified-memory

Единый MCP-сервер памяти для Hermes Agent — полное слияние двух стеков:

- [hermes-lcm](https://github.com/stephenschoettler/hermes-lcm) (~65k строк) — lossless context management: SQLite message store, summary DAG, bounded recall после компакшна.
- [mnemosyne](https://github.com/AxDSan/mnemosyne) (~43k строк) — long-term memory: canonical facts, working/episodic memory, triples-граф, persona-слои.

Оба проекта MIT — слияние чистое юридически, attribution в `NOTICE`.

## Зачем

Сегодня оба сервиса персистят текст разговоров и оба отвечают «а что было раньше»
(`mnemosyne_recall` vs `lcm_recall`/`lcm_grep`), каждый со своей БД
(`mnemosyne.db` + `lcm.db`), своим embedding-провайдером и своим тул-неймспейсом.
Результат: двойное хранение, двойная семантика, двойной recall-путь.

Цель: **один пакет, одна БД, один тул-неймспейс `mem_*`, один embedding-слой.**

## Архитектура

```
┌─────────────────────────────────────────────────┐
│              MCP server (stdio)                 │
│  mem_remember / mem_recall / mem_expand /       │
│  mem_status / mem_doctor / mem_forget ...       │
├─────────────────────────────────────────────────┤
│  router.py — единый recall: FTS + vectors +     │
│  RRF fusion, scope/recency priors               │
├──────────────┬──────────────────┬───────────────┤
│ embeddings.py│ store.py (1 SQLite)│ ingest.py   │
│ 1 fastembed  │ um_messages      │ 1 пайплайн →  │
│ провайдер   │ um_summaries(DAG)│ DAG + графы   │
│ + реестр    │ um_facts/triples │ памяти        │
│ + dim-guard │ um_vectors       │               │
└──────────────┴──────────────────┴───────────────┘
```

Исходные модули маппятся в новое ядро без переписывания логики на этапе 1
(адаптеры), с постепенным переносом — см. `docs/`.

## Статус

- [x] Этап 0: скелет репо + `embeddings.py` (общий слой, работает)
- [ ] Этап 1: `store.py` — единая схема, миграция `mnemosyne.db` + `lcm.db`
- [ ] Этап 2: `ingest.py` — единый пайплайн (сообщения → DAG + память)
- [ ] Этап 3: `router.py` + MCP-тулы `mem_*`
- [ ] Этап 4: перенос логики из апстримов, удаление дублей

## Быстрый старт (этап 0)

```bash
pip install -e .
python -m unified_memory.embeddings  # smoke-test провайдера
```

## Доки

- `docs/MODULE_MAP.md` — какие модули апстримов куда переезжают
- `docs/TOOL_MAP.md` — `lcm_*` + `mnemosyne_*` → `mem_*`
- `docs/MIGRATION_PLAN.md` — этапы, риски, критерии готовности
