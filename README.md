# unified-memory-mcp

Один MCP-сервер вместо двух: **хранение + поиск + сжатие** информации для любого MCP-клиента (Hermes Agent, Claude Code, ...).

Собрано из уроков двух боевых систем:
- [hermes-lcm](https://github.com/stephenschoettler/hermes-lcm) — lossless context management (message store, summary DAG, bounded recall)
- [mnemosyne](https://github.com/AxDSan/mnemosyne) — long-term memory (canonical facts, working/episodic memory, triples-граф)

Обе MIT — attribution в `NOTICE`. Здесь не форк: ядро написано с нуля по их картам
(`docs/MODULE_MAP.md`, `docs/TOOL_MAP.md`), без перетаскивания 108k строк.

## Установка

```bash
git clone https://github.com/sportlotto-ux/unified-memory-mcp
cd unified-memory-mcp
pip install -e .                    # база: FTS-поиск + extractive-сжатие, всё из коробки
pip install -e .[local-embed]       # + семантика: локальный fastembed, CPU, без облаков
pip install -e .[tokens]            # рекомендуется: точный tiktoken/cl100k для бюджета и компакшна
```

> Токен-оценщик общий для компакшна и `mem_assemble`. Без `.[tokens]` работает
> детерминированная RU-aware эвристика — пороги компакшна она держит, но на
> смешанном RU/EN/коде погрешность накапливается иначе, чем на однородном тексте;
> для жёсткого бюджетного счёта ставьте `.[tokens]`.

Требования: Python 3.11+, SQLite из коробки, MCP Python SDK 2.x
(`mcp>=2.0,<3`). Опционально для настоящего пересказа:

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
      "cwd": "/path/to/unified-memory-mcp",
      "env": { "UM_DATABASE_PATH": "~/.hermes/unified_memory.db" }
    }
  }
}
```

## Тулы (19)

| Тул | Что делает |
|---|---|
| `mem_remember` | Сохранить сообщение; авто-компакшн при превышении порога давления |
| `mem_fact` | Слот-факт (одно живое значение на `owner/category/name`) + опциональный триплет графа; то же тело — no-op, новое — supersede с историей |
| `mem_link` | Типизированная связь (`src`/`dst` как `fact:3`/`message:12`, `rel` ∈ `supports`/`contradicts`/`supersedes`/`derives_from`). Оба конца обязаны существовать и принадлежать `owner`; повтор живой связи — no-op с тем же id |
| `mem_graph_query` | Bounded graph traversal: exact `subject`/`predicate`/`object` для entity edges, `rel`/`min_weight` для typed links, `as_of`, `max_hops`, owner/session/liveness isolation; deterministic `edges`/`links` result |
| `mem_batch` | Атомарный батч записей (all-or-nothing): ops `remember_fact` \| `update` (fact/edge/link) \| `forget` (fact/edge/link). `dry_run=true` — валидация с откатом. Без кросс-ссылок; каждый op в savepoint; текст идёт через redaction-гейт |
| `mem_update` | Правка факта по id (новая версия, history живёт) или истечение/reopen факта/ребра/связи (`valid_until`) |
| `mem_recall` | Единый поиск: FTS + вектора + граф + RRF. `scope`: `all`/`session`/`facts`; `as_of` — срез графа на дату; `include_expired` — история; `source` — фильтр сообщений по source (facts/summaries/graph исключаются); `include_archived=true` — добавить bounded lexical search по cold archive (owner/session/source сохраняются, default hot-only); bounded importance component включается через `UM_IMPORTANCE_WEIGHT` (default `0`, legacy ranking); `hops>1` — BFS-обход типизированных связей и entity-графа, `rel` фильтрует связи (`supports`/`contradicts`/`supersedes`/`derives_from`; на рёбрах — `predicate`). `diagnostics=true` → `{hits, diagnostics}` (per-arm counts/вклад/timings/BFS/importance), `false` — прежний список |
| `mem_recent` | Temporal: что было в UTC-окне (`today`/`yesterday`/`week`/`month`/`Nd`/`date:`/`last Nh`); пагинация старых страниц через `before_ts`+`before_id`+`before_kind` из `next` (kind — тайбрейкер тия между messages/summaries) |
| `mem_expand` | Дословно по `kind`+`id`, единая схема `{kind,id,body}` |
| `mem_get` | Точечное чтение `message/fact/summary/edge`: body, metadata, vector status и прямые links |
| `mem_inspect` | Read-only диагностика store/session: integrity, hygiene, archive audit, pressure, frontier и summary DAG |
| `mem_load_session` | Cursor-paginated transcript recovery; live bodies или archive refs для cold rows |
| `mem_evidence` | Проверка опоры на refs: `cite` (дословно/почти → supported/partial/unsupported), `compute` (агрегация чисел над refs: count/sum/min/max/avg/median; pattern ≤256 символов, timeout 50ms), `conflicts` (кандидаты противоречий без вердикта, `needs_judgment`). Без LLM, только переданные refs |
| `mem_reindex` | Доложит недостающие вектора; полная смена embedding-модели — offline `python -m unified_memory.reembed` |
| `mem_compact` | Ручное сжатие старых сообщений (сырьё остаётся) |
| `mem_assemble` | Bounded активный контекст: summaries + свежий хвост в бюджет токенов (бюджет считается токен-оценщиком; для жёсткой арифметики — `.[tokens]`) |
| `mem_forget` | Удаление по `kind`: `fact`/`edge`/`link` (id) или `entity` (имя), каскадом |
| `mem_status` | Счётчики + флаги деградации (`vectors_enabled`, `summarizer`, `fts`) |
| `mem_doctor` | `integrity_check`, вектора по моделям, hygiene; read-only `export` (JSON-дамп в `<db>.export-<ts>.json`, вектора base64, архив не входит), `archive_check` (заглушки ↔ архив, orphans), `secret_scan` (каталог redaction, отчёт без значений); мутации `clean`/`repair` (backup-first), `archive`/`purge`/`retention` (только с `apply=true`; `retention` — age-based вынос горячего старше `UM_RETENTION_DAYS`) |

## Миграция P1.6

LCM snapshot importer работает через read-only SQLite URI и по умолчанию только
строит reconciliation report:

```bash
python -m unified_memory.migration --format lcm --input /path/to/lcm.db
# explicit atomic apply
python -m unified_memory.migration --format lcm --input /path/to/lcm.db --apply
```

Raw `messages` переносятся в порядке `store_id`; `conversation_id`, source ordering
и redaction-gated tool metadata сохраняются. LCM `summary_nodes` по умолчанию
не копируются — summaries пересчитываются Unified. Для явного сохранения source
summaries добавьте `--summary-strategy preserve --apply`. Отчёт не содержит source
content или tool payload.

Mnemosyne adapter импортирует canonical facts/history, `triples` и `graph_edges`,
а `working_memory`/`episodic_memory` по умолчанию пропускает:

```bash
python -m unified_memory.migration \
  --format mnemosyne --input /path/to/mnemosyne.db \
  --owner-map '{"bank-a":"tenant-a"}' --default-owner tenant-a
```

Для явно выбранной message policy:

```bash
python -m unified_memory.migration \
  --format mnemosyne --input mnemosyne.db --apply \
  --working-policy message --episodic-policy message --memory-policy message
```

Derived/unsupported tables остаются в `skipped_fields`; source content и payloads
не попадают в отчёт.

## Как это работает

- **Хранение:** одна SQLite (WAL): `um_messages` + `um_summaries` (DAG) + `um_facts` + `um_entities`/`um_edges` (граф) + `um_links` (типизированные связи, traversal-only) + `um_vectors` + `um_fts` (FTS5) + `um_meta`.
- **Эмбеддинги:** два бэкенда. `local` (дефолт репо) — fastembed, модель `paraphrase-multilingual-mpnet-base-v2` (768, не дистиллят); для лёгких стендов MiniLM-L12 через `UM_EMBEDDING_MODEL`. `openai` — OpenAI-протокол `/v1/embeddings` поверх stdlib (ноль зависимостей): так подключается локальный model2vec-сервер Hermes (`UM_EMBEDDING_BASE_URL`, дефолт `http://127.0.0.1:8127`, potion = 256 dim, авто-детект). Держи сервер uncapped — static-модели молча режут после 512 токенов при выставленном `EMBED_MAX_TOKENS`. Основная запись и embedding атомарны; смена model/dim отвергается до запуска `python -m unified_memory.reembed`, который безопасно переэмбедит все строки и обновит stamp.
- **Поиск:** FTS5 (fallback LIKE с тем же session-scope) + cosine по векторам + RRF, поверх — recency-приор (`UM_RECENCY_HALFLIFE_DAYS`, дефолт 30, 0=off), scope-bias текущей сессии (`UM_SCOPE_BIAS`, дефолт 0.15), bounded importance multiplier для facts (`UM_IMPORTANCE_WEIGHT`, дефолт `0` = legacy ranking) и MMR-диверсификация по Жаккару (`UM_MMR_LAMBDA`, дефолт 0.7, 1=off). `mem_recall(include_archived=true)` добавляет отдельный bounded lexical scan холодного архива; owner/session/source фильтры применяются до scan cap, а exact body остаётся за `mem_expand`. BFS `scope="session"` не выходит через link в узел другой сессии. При `pip install -e .[local-vec]` + `mem_reindex` — vec0-индекс (KNN-кандидаты + точный косинусный перескоринг, паритет с brute force пробами); без индекса — честный фулскан. Без fastembed — честный FTS-режим, `mem_status` так и скажет (`vectors_enabled: false`), молчаливого «вроде ищет» нет.
- **Сжатие:** давление = токены сессии vs `UM_CONTEXT_TOKENS × UM_COMPACT_THRESHOLD` (дефолт 200k × 0.35, как LCM). Токены: при `pip install -e .[tokens]` — точный tiktoken/cl100k, иначе детерминированная RU-aware эвристика (`ASCII/4 + не-ASCII/2`; голый `len//4` занижал кириллицу ~2.3x). Накрыло → старые (всё кроме `UM_FRESH_TAIL_COUNT` свежих) в summary depth 0; каждые `UM_DAG_FANIN` нод уровня схлопываются в уровень выше. Frontier, leaf summary и condense-проходы пишутся одной транзакцией. `mem_assemble` собирает bounded контекст под бюджет тем же оценщиком.
- **Защита от старых болячек:** нет жёсткого `importance: 0.95` (причина canonical-bloat в mnemosyne) — кап `0..1`; смена embedding-модели блокирует обычный сервер, а offline `reembed` заменяет все vectors атомарно; `mem_evidence(pattern=...)` ограничен по длине и времени выполнения.
- **Redaction:** гейт на входе (`UM_REDACT_ENABLED`, дефолт ON): `api_key,bearer_token,password_assignment,private_key` — каталог и регулярки как у LCM. Режется до SQLite/FTS/vectors/summaries, включая `mem_update`; плейсхолдер `[UM redaction: name=...; chars=N]` необратим. Forward-only: что попало в стор раньше — чистить руками + reindex.
- **Retention/архив (lossless-холод):** `UM_RETENTION_DAYS` = **сколько держать ГОРЯЧЕЕ** (recall быстрый, БД маленькая), а не срок жизни данных. `0` (дефолт) = копим всё в горячей вечно. `>0` → раз в неделю (ленивый проход) горячее старше N дней уезжает в архив. Архив — **отдельный файл, lossless**, живёт вечно; **автоудаления нет** — физическое `purge` только вручную (`mem_doctor(mode=purge, apply=true)`). При пороге размера (`UM_ARCHIVE_SIZE_MB`, дефолт 1 ГБ) старейшее добивается до порога. В архив уезжают текст **и вектор** (вариант a2 — так порог реально держится), в горячей остаётся заглушка `[archived]`, `mem_expand` прозрачно достаёт текст из архива. Холодный recall включается только явным `mem_recall(include_archived=true)`, использует bounded scan с `UM_ARCHIVE_RECALL_SCAN_LIMIT` и не создаёт архив при чтении. Ручной `purge` режет архив по тому же `UM_RETENTION_DAYS` — то есть вычищает ровно строки старше N (при `retention_days>0` это почти весь холод, **осознанно**); при `retention_days=0` `purge` — no-op.
- **Факты = слоты (`mem_fact`), сообщения = лог (`mem_remember`).** Один живой факт на `(owner, category, name)` — гарантирует partial unique index, не код. Новое тело вытесняет старое (`valid_until`, `superseded_by`), история lossless; `valid_until=0` = живое (sentinel). `mem_recall`/`mem_expand` прячут истёкшее (`include_expired=True` — аудит). `mem_forget` — жёсткое удаление, истечение — только `mem_update`.
- **Проверка и арифметика (`mem_evidence`).** Детерминированно, без LLM, **только над переданными `refs`** (никакого авто-поиска — иначе инструмент превращается в мини-агента с его fallback-багами). `cite`: дословное/почти-дословное вхождение claim в тело ref → `supported/partial/unsupported` (RU-морфология через дешёвый prefix-stem). `compute`: агрегация чисел из тел тех же refs (`count/sum/min/max/avg/median`); интент парсит хост-агент. `conflicts`: высокоточные кандидаты противоречий (смена значения в слоте, точная негация) **без вердикта** — судью делает LLM-хост, тул не шумит.

## Переменные окружения

| Переменная | Дефолт | Назначение |
|---|---|---|
| `UM_DATABASE_PATH` | `~/.hermes/unified_memory.db` | Путь к БД |
| `HERMES_HOME` | `~/.hermes` | База для дефолтных путей (`~/.hermes/*`) |
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
| `UM_ARCHIVE_RECALL_SCAN_LIMIT` | `2000` | Максимум cold-сообщений, просматриваемых bounded archive recall |
| `UM_EVIDENCE_MAX_REFS` | `50` | `mem_evidence`: максимум refs за вызов (лишние — в `rejections`) |
| `UM_EVIDENCE_MAX_CHARS` | `8000` | `mem_evidence`: сколько символов тела брать из каждого ref |
| `UM_EVIDENCE_PARTIAL` | `0.5` | `mem_evidence(cite)`: порог покрытия токенов для `partial` |
| `UM_RECALL_MAX_HOPS` | `3` | `mem_recall(hops)`: потолок BFS-обхода связей (выше — ошибка) |
| `UM_LINK_FANOUT` | `20` | `mem_recall(hops>1)`: максимум связей с узла на направление |
| `UM_GRAPH_DECAY` | `0.5` | `mem_recall(hops>1)`: множитель score на глубину (`depth-1`) |
| `UM_IMPORTANCE_WEIGHT` | `0.0` | Bounded ranking component для facts; `0` сохраняет legacy ranking |
| `UM_BATCH_MAX_OPS` | `100` | `mem_batch`: потолок числа ops (проверка до открытия транзакции) |
| `UM_BATCH_MAX_CHARS` | `200000` | `mem_batch`: потолок суммарного payload ops |
| `UM_REDACT_ENABLED` | `true` | Гейт секретов на входе (дефолт ON — продукт публичный) |
| `UM_REDACT_PATTERNS` | `api_key,bearer_token,password_assignment,private_key` | Подмножество каталога через запятую |
| `UM_SUMMARIZER_URL` / `UM_SUMMARIZER_MODEL` | — | LLM-пересказ; без них extractive |
| `UM_SUMMARIZER_API_KEY` | — | Bearer для endpoint |
| `UM_CONTEXT_TOKENS` | `200000` | Эффективное окно хоста |
| `UM_COMPACT_THRESHOLD` | `0.35` | Доля окна — триггер компакшна |
| `UM_FRESH_TAIL_COUNT` | `20` | Свежих сообщений не жмём никогда |
| `UM_DAG_FANIN` | `5` | Нод уровня → одна выше |
| `UM_ASSEMBLY_BUDGET` | `8000` | Токенов в `mem_assemble` по дефолту |
| `UM_MAX_TEXT_CHARS` | `200000` | Кап входного текста (громкий `ValueError`, не тихая обрезка) |
| `UM_COMPACT_MAX_MSGS` | `10000` | Кап головы компакшна за проход (остаток досжимается следующим вызовом) |

## Известные ограничения (v0.9)

Полный список отложенного — `docs/BACKLOG.md`.

- Архив выносит только **сообщения** (текст+вектор) — основной драйвер роста. Истёкшие факты/рёбра и `um_summaries` — TODO (`docs/BACKLOG.md`).
- Миграция upstream: P1.6 LCM/Mnemosyne adapters поддерживают dry-run/atomic apply и reconciliation report; working/episodic rows по умолчанию пропускаются, derived tables явно перечислены в `skipped_fields`.
- `mem_doctor(mode=export)` пишет **стриминговый JSONL** (`um-export-jsonl`: header + `{table,row}` построчно; вектора base64, um_fts/um_vecidx исключены). Импорт — `python -m unified_memory.import_dump <file> [--owner] [--dry-run]`, аддитивный (fresh-id remap, слот-конфликт → skip), без backend. **Чтение дампа — целиком в память** (стриминг только на записи).
- Isolation добровольная: `owner=""` (дефолт) — legacy без фильтра, видит всё; строгая изоляция — только при непустом `owner`. Старые БД мигрируют сами (`owner=''`), сущности пересобираются под `UNIQUE(name, owner)`.
- Поддерживается MCP Python SDK 2.x (`mcp>=2.0,<3`); MCP 1.x intentionally не входит в dependency contract.
- Redaction forward-only: сторa, созданные до v0.4, могут содержать секреты — чистить руками + reindex.
- Смена embedding-модели: обычный MCP-сервер падает громко (`DimensionMismatchError`); для перехода без MCP запустите `python -m unified_memory.reembed`, который переэмбедит messages/summaries/facts/edges/entities и обновит model stamp.
- Cron-режима нет (демона нет), но age-based проход (а) теперь есть вручную/по cron: `mem_doctor(mode=retention, apply=true)` выносит горячее старше `UM_RETENTION_DAYS` (dry-run без `apply` считает `would_move`, идемпотентен). Ленивый недельный проход на ingest остаётся.
- Пагинации **ранжированного** `mem_recall` нет и не будет: возвращаемый порядок — fused-релевантность, а не стабильный ключ; «следующие N» через offset даст недетерминированную выдачу. Сужайте запрос/увеличивайте `limit`. Пагинация есть только у хронологического `mem_recent` (`before_id`+`before_ts`).

## Разработка

```bash
python -m pytest tests/ -q   # текущий dev-прогон: 369 passed, 4 skipped (Python 3.14)
```

Полный suite также прогоняется на Python 3.12; CI дополнительно собирает wheel,
устанавливает его в чистый venv и импортирует `unified_memory.server`.
Прогон герметичен: `tests/conftest.py` снимает ambient `UM_*` (иначе шелл с
`UM_REDACT_ENABLED=off` или `UM_EMBEDDING_BACKEND=openai` молча ронял 12 тестов).
Тестам с env — только `monkeypatch.setenv`. `UM_LIVE_*` конфигом не считается.

Roadmap и разбор апстримов: `docs/MIGRATION_PLAN.md`. Переезд с hermes-lcm/mnemosyne: `docs/IMPORT.md`.
