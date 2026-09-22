# hermes-unified-memory

Один MCP-сервер вместо двух: **хранение + поиск + сжатие** информации для любого MCP-клиента (Hermes Agent, Claude Code, ...).

Собрано из уроков двух боевых систем:
- [hermes-lcm](https://github.com/stephenschoettler/hermes-lcm) — lossless context management (message store, summary DAG, bounded recall)
- [mnemosyne](https://github.com/AxDSan/mnemosyne) — long-term memory (canonical facts, working/episodic memory, triples-граф)

Обе MIT — attribution в `NOTICE`. Здесь не форк: ядро написано с нуля по их картам
(`docs/MODULE_MAP.md`, `docs/TOOL_MAP.md`), без перетаскивания 108k строк.

## Установка

```bash
git clone https://github.com/<you>/hermes-unified-memory
cd hermes-unified-memory
pip install -e .                    # база: FTS-поиск + extractive-сжатие, всё из коробки
pip install -e .[local-embed]       # + семантика: локальный fastembed, CPU, без облаков
```

Требования: Python 3.11+, SQLite из коробки. Опционально для настоящего пересказа:

```bash
export UM_SUMMARIZER_URL=http://localhost:11434/v1   # OpenAI-совместимый endpoint (ollama и др.)
export UM_SUMMARIZER_MODEL=qwen3:8b
```

## Подключение

```json
{
  "mcpServers": {
    "unified-memory": {
      "command": "python",
      "args": ["-m", "unified_memory.server"],
      "cwd": "/path/to/hermes-unified-memory",
      "env": { "UM_DATABASE_PATH": "~/.hermes/unified_memory.db" }
    }
  }
}
```

## Тулы (10)

| Тул | Что делает |
|---|---|
| `mem_remember` | Сохранить сообщение; авто-компакшн при превышении порога давления |
| `mem_fact` | Сохранить долгий факт + опциональный триплет графа (`subject`, `predicate`, `object`) |
| `mem_recall` | Единый поиск: FTS + вектора + граф (1-hop) + RRF. `scope`: `all`/`session`/`facts` |
| `mem_expand` | Дословно по `kind`+`id`, единая схема `{kind,id,body}` |
| `mem_reindex` | Доложит недостающие вектора (лестница после смены модели) |
| `mem_compact` | Ручное сжатие старых сообщений (сырьё остаётся) |
| `mem_assemble` | Bounded активный контекст: summaries + свежий хвост в бюджет токенов |
| `mem_forget` | Удаление по `kind`: `fact`/`edge` (id) или `entity` (имя), каскадом |
| `mem_status` | Счётчики + флаги деградации (`vectors_enabled`, `summarizer`, `fts`) |
| `mem_doctor` | `integrity_check`, вектора по моделям |

## Как это работает

- **Хранение:** одна SQLite (WAL): `um_messages` + `um_summaries` (DAG) + `um_facts` + `um_entities`/`um_edges` (граф) + `um_vectors` + `um_meta`.
- **Эмбеддинги:** дефолт репо — полная `paraphrase-multilingual-mpnet-base-v2` (768, не дистиллят). Для лёгких стендов — дистиллированная MiniLM-L12 через `UM_EMBEDDING_MODEL` (так стоит у автора в Hermes).
- **Поиск:** FTS5 (fallback LIKE) + cosine по векторам + RRF. Без fastembed — честный FTS-режим, `mem_status` так и скажет (`vectors_enabled: false`), молчаливого «вроде ищет» нет.
- **Сжатие:** давление = токены сессии vs `UM_CONTEXT_TOKENS × UM_COMPACT_THRESHOLD` (дефолт 200k × 0.35, как LCM). Накрыло → старые (всё кроме `UM_FRESH_TAIL_COUNT` свежих) в summary depth 0; каждые `UM_DAG_FANIN` нод уровня схлопываются в уровень выше. Frontier в `um_meta` — каждое сообщение жмётся один раз. `mem_assemble` собирает bounded контекст под бюджет.
- **Защита от старых болячек:** нет жёсткого `importance: 0.95` (причина canonical-bloat в mnemosyne) — кап `0..1`; смена embedding-модели без reindex — громкая ошибка, а не тихая деградация recall.

## Переменные окружения

| Переменная | Дефолт | Назначение |
|---|---|---|
| `UM_DATABASE_PATH` | `~/.hermes/unified_memory.db` | Путь к БД |
| `UM_EMBEDDING_MODEL` | `paraphrase-multilingual-mpnet-base-v2` (768) | Модель fastembed строго из реестра |
| `UM_SUMMARIZER_URL` / `UM_SUMMARIZER_MODEL` | — | LLM-пересказ; без них extractive |
| `UM_SUMMARIZER_API_KEY` | — | Bearer для endpoint |
| `UM_CONTEXT_TOKENS` | `200000` | Эффективное окно хоста |
| `UM_COMPACT_THRESHOLD` | `0.35` | Доля окна — триггер компакшна |
| `UM_FRESH_TAIL_COUNT` | `20` | Свежих сообщений не жмём никогда |
| `UM_DAG_FANIN` | `5` | Нод уровня → одна выше |
| `UM_ASSEMBLY_BUDGET` | `8000` | Токенов в `mem_assemble` по дефолту |

## Известные ограничения (v0.3)

- Нет user-isolation: один стор на инсталляцию, мультитенантность не предусмотрена.
- Нет redaction чувствительных данных (у LCM есть `SENSITIVE_PATTERNS`) — не кладите секреты или чистите руками.
- `UM_VEC_TYPE` удалён: вектора всегда float32, конфиг больше не врёт.
- Смена embedding-модели требует reindex (зато громко падает, а не молча врёт — см. `DimensionMismatchError`). Ранние сторa на MiniLM-384 с дефолтом mpnet-768 несовместимы: пересоздайте БД или задайте `UM_EMBEDDING_MODEL` явно.

## Разработка

```bash
python -m pytest tests/ -q   # 50 passed, 1 skipped без fastembed
```

Roadmap и разбор апстримов: `docs/MIGRATION_PLAN.md`. Переезд с hermes-lcm/mnemosyne: `docs/IMPORT.md`.
