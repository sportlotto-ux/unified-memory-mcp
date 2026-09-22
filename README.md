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
export UM_SUMMARIZER_MODEL=qwen3:4b   # дешёвая локальная модель, НЕ фронтирная (см. «Стоимость»)
```

## Стоимость суммаризации (прочти до включения endpoint)

Кто суммирует — решаете вы, не агент:
- **По умолчанию — никто (0 ₽).** Extractive-конденсация: офлайн, детерминирована,
  без LLM вообще. Конденсат, не пересказ — но бесплатный.
- **С `UM_SUMMARIZER_*` — ваша endpoint-модель.** Сервер сам шлёт ей текст на
  каждом триггере давления: 1 вызов на leaf-summary + до 10 condense-проходов
  (`condense` ограничен, но при маленьком окне каждый чих = пачка вызовов).

Правила, чтобы не сжечь бюджет:
1. **Endpoint = дешёвая локальная модель** (`qwen3:4b`, `ministral`, аналоги через ollama).
   Никогда не направляйте сюда фронтирную чат-модель: авто-компакшн срабатывает
   регулярно, и дорогой токен × регулярность = резкий рост счёта.
2. **Прикиньте математику:** в триггере вход ≈ токены сжимаемого хвоста
   (кап POST — 12k символов). Частота триггеров ≈ 1 на `порог − хвост` новых токенов.
   Пример: окно 200k × 0.35 = 70k, в триггере ~50k символов входа на 4b-модели локально = 0 ₽;
   те же 50k на платной флагманской = дорого × десятки раз в день.
3. **Держите окно реалистичным.** Тестовые `UM_CONTEXT_TOKENS=400` — только для тестов:
   в проде крошечный порог = компакшн на каждом сообщении = пачка LLM-вызовов.
4. **Для Hermes-юзеров:** тот же принцип у LCM — auxiliary-модель для саммаризации
   должна быть дешёвой; дорогая модель — только в чат, не в инфраструктуру.
5. Сломанный/медленный endpoint не роняет запись (`status: degraded`), но висящие
   ретраи — ваши: держите `timeout` endpoint низким на своей стороне.

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

## Тулы (12)

| Тул | Что делает |
|---|---|
| `mem_remember` | Сохранить сообщение; авто-компакшн при превышении порога давления |
| `mem_fact` | Слот-факт (одно живое значение на `owner/category/name`) + опциональный триплет графа; то же тело — no-op, новое — supersede с историей |
| `mem_update` | Правка факта по id (новая версия, history живёт) или истечение/reopen факта/ребра (`valid_until`) |
| `mem_recall` | Единый поиск: FTS + вектора + граф (1-hop) + RRF. `scope`: `all`/`session`/`facts`; `as_of` — срез графа на дату; `include_expired` — история |
| `mem_expand` | Дословно по `kind`+`id`, единая схема `{kind,id,body}` |
| `mem_reindex` | Доложит недостающие вектора (лестница после смены модели) |
| `mem_recent` | Temporal: что было в UTC-окне (`today`/`week`/`Nd`/`date:`/`last Nh`) |
| `mem_compact` | Ручное сжатие старых сообщений (сырьё остаётся) |
| `mem_assemble` | Bounded активный контекст: summaries + свежий хвост в бюджет токенов |
| `mem_forget` | Удаление по `kind`: `fact`/`edge` (id) или `entity` (имя), каскадом |
| `mem_status` | Счётчики + флаги деградации (`vectors_enabled`, `summarizer`, `fts`) |
| `mem_doctor` | `integrity_check`, вектора по моделям, hygiene; режимы `clean`/`repair` (backup-first) и `archive`/`purge` (только с `apply=true`) |

## Как это работает

- **Хранение:** одна SQLite (WAL): `um_messages` + `um_summaries` (DAG) + `um_facts` + `um_entities`/`um_edges` (граф) + `um_vectors` + `um_meta`.
- **Эмбеддинги:** два бэкенда. `local` (дефолт репо) — fastembed, модель `paraphrase-multilingual-mpnet-base-v2` (768, не дистиллят); для лёгких стендов MiniLM-L12 через `UM_EMBEDDING_MODEL`. `openai` — OpenAI-протокол `/v1/embeddings` поверх stdlib (ноль зависимостей): так подключается локальный model2vec-сервер Hermes (`UM_EMBEDDING_BASE_URL`, дефолт `http://127.0.0.1:8127`, potion = 256 dim, авто-детект). Держи сервер uncapped — static-модели молча режут после 512 токенов при выставленном `EMBED_MAX_TOKENS`.
- **Поиск:** FTS5 (fallback LIKE) + cosine по векторам + RRF, поверх — recency-приор (`UM_RECENCY_HALFLIFE_DAYS`, дефолт 30, 0=off), scope-bias текущей сессии (`UM_SCOPE_BIAS`, дефолт 0.15) и MMR-диверсификация по Жаккару (`UM_MMR_LAMBDA`, дефолт 0.7, 1=off). При `pip install -e .[local-vec]` + `mem_reindex` — vec0-индекс (KNN-кандидаты + точный косинусный перескоринг, паритет с brute force пробами); без индекса — честный фулскан. Без fastembed — честный FTS-режим, `mem_status` так и скажет (`vectors_enabled: false`), молчаливого «вроде ищет» нет.
- **Сжатие:** давление = токены сессии vs `UM_CONTEXT_TOKENS × UM_COMPACT_THRESHOLD` (дефолт 200k × 0.35, как LCM). Накрыло → старые (всё кроме `UM_FRESH_TAIL_COUNT` свежих) в summary depth 0; каждые `UM_DAG_FANIN` нод уровня схлопываются в уровень выше. Frontier в `um_meta` — каждое сообщение жмётся один раз. `mem_assemble` собирает bounded контекст под бюджет.
- **Защита от старых болячек:** нет жёсткого `importance: 0.95` (причина canonical-bloat в mnemosyne) — кап `0..1`; смена embedding-модели без reindex — громкая ошибка, а не тихая деградация recall.
- **Redaction:** гейт на входе (`UM_REDACT_ENABLED`, дефолт ON): `api_key,bearer_token,password_assignment,private_key` — каталог и регулярки как у LCM. Режется до SQLite/FTS/vectors/summaries, плейсхолдер `[UM redaction: name=...; chars=N]` необратим. Forward-only: что попало в стор раньше — чистить руками + reindex.
- **Retention/архив (lossless-холод):** `UM_RETENTION_DAYS` = **сколько держать ГОРЯЧЕЕ** (recall быстрый, БД маленькая), а не срок жизни данных. `0` (дефолт) = копим всё в горячей вечно. `>0` → раз в неделю (ленивый проход) горячее старше N дней уезжает в архив. Архив — **отдельный файл, lossless**, живёт вечно; **автоудаления нет** — физическое `purge` только вручную (`mem_doctor(mode=purge, apply=true)`). При пороге размера (`UM_ARCHIVE_SIZE_MB`, дефолт 1 ГБ) старейшее добивается до порога. В архив уезжают текст **и вектор** (вариант a2 — так порог реально держится), в горячей остаётся заглушка `[archived]`, `mem_expand` прозрачно достаёт текст из архива.
- **Факты = слоты (`mem_fact`), сообщения = лог (`mem_remember`).** Один живой факт на `(owner, category, name)` — гарантирует partial unique index, не код. Новое тело вытесняет старое (`valid_until`, `superseded_by`), история lossless; `valid_until=0` = живое (sentinel). `mem_recall`/`mem_expand` прячут истёкшее (`include_expired=True` — аудит). `mem_forget` — жёсткое удаление, истечение — только `mem_update`.

## Переменные окружения

| Переменная | Дефолт | Назначение |
|---|---|---|
| `UM_DATABASE_PATH` | `~/.hermes/unified_memory.db` | Путь к БД |
| `UM_EMBEDDING_MODEL` | `paraphrase-multilingual-mpnet-base-v2` (768) | local: модель fastembed строго из реестра; openai: passthrough-имя |
| `UM_EMBEDDING_BACKEND` | `local` | `local` (fastembed) \| `openai` (8127/любой OpenAI-совместимый) |
| `UM_EMBEDDING_BASE_URL` | `http://127.0.0.1:8127` | База для backend=openai |
| `UM_EMBEDDING_TIMEOUT` | `30.0` | Таймаут HTTP, сек |
| `UM_EMBEDDING_DIM` | — | Пропустить probe dim (openai), полезно оффлайн |
| `UM_VEC_INDEX` | `auto` | `auto` (строить в reindex, KNN при совпадении dim) \| `off` (всегда brute force) |
| `UM_RETENTION_DAYS` | `0` | `0` = копим вечно. `>0` = горячее старше N дней уезжает в архив раз в неделю (lossless; удаление — только вручную `purge`) |
| `UM_ARCHIVE_SIZE_MB` | `1024` | Порог горячей БД: старейшие сообщения уезжают в архив |
| `UM_ARCHIVE_PATH` | `~/.hermes/unified_memory.archive.db` | Отдельный файл холода |
| `UM_ARCHIVE_BATCH` | `500` | Сколько сообщений за один проход архивации |
| `UM_REDACT_ENABLED` | `true` | Гейт секретов на входе (дефолт ON — продукт публичный) |
| `UM_REDACT_PATTERNS` | `api_key,bearer_token,password_assignment,private_key` | Подмножество каталога через запятую |
| `UM_SUMMARIZER_URL` / `UM_SUMMARIZER_MODEL` | — | LLM-пересказ; без них extractive |
| `UM_SUMMARIZER_API_KEY` | — | Bearer для endpoint |
| `UM_CONTEXT_TOKENS` | `200000` | Эффективное окно хоста |
| `UM_COMPACT_THRESHOLD` | `0.35` | Доля окна — триггер компакшна |
| `UM_FRESH_TAIL_COUNT` | `20` | Свежих сообщений не жмём никогда |
| `UM_DAG_FANIN` | `5` | Нод уровня → одна выше |
| `UM_ASSEMBLY_BUDGET` | `8000` | Токенов в `mem_assemble` по дефолту |

## Известные ограничения (v0.5)

- Архив пока выносит **сообщения** (текст+вектор) — основной драйвер роста. Вынос истёкших фактов/рёбер и `um_summaries` — TODO; в плане.

- Isolation: `owner=""` (дефолт) — legacy без фильтра, видит всё; непустой owner — строгая изоляция во всех тулах. Старые БД мигрируют сами (owner=''), сущности пересобираются под UNIQUE(name, owner).
- Redaction forward-only: сторa, созданные до v0.4, могут содержать секреты — чистить руками + reindex.
- `UM_VEC_TYPE` удалён: вектора всегда float32, конфиг больше не врёт.
- Тесты герметичны: ambient `UM_*` из шелла не влияет на прогон (`tests/conftest.py` чистит env; `UM_LIVE_*` — opt-in гейты, их не трогает).
- Смена embedding-модели требует reindex (зато громко падает, а не молча врёт — см. `DimensionMismatchError`). Ранние сторa на MiniLM-384 с дефолтом mpnet-768 несовместимы: пересоздайте БД или задайте `UM_EMBEDDING_MODEL` явно.

## Разработка

```bash
python -m pytest tests/ -q   # 142 passed, 4 skipped без fastembed/vec; UM_LIVE_OPENAI=1 — live против 8127
```

Прогон герметичен: `tests/conftest.py` снимает ambient `UM_*` (иначе шелл с
`UM_REDACT_ENABLED=off` или `UM_EMBEDDING_BACKEND=openai` молча ронял 12 тестов).
Тестам с env — только `monkeypatch.setenv`. `UM_LIVE_*` конфигом не считается.

Roadmap и разбор апстримов: `docs/MIGRATION_PLAN.md`. Переезд с hermes-lcm/mnemosyne: `docs/IMPORT.md`.
