# PARITY_HARDENING_PROTOCOL — регламент реализации unified-memory

Статус: **P0.1–P0.4, P1.1–P1.6 реализованы; изменения P1.6 не закоммичены**.

Документ фиксирует порядок работ после аудита переноса из `hermes-lcm` и
`mnemosyne`. Цель — не расширять API поверх известных correctness/lifecycle
проблем, а сначала довести unified-memory до безопасного компактного ядра,
затем добавлять выбранные функции.

## 0. Baseline

Точка отсчёта:

- branch: `main`;
- baseline commit: `43d547a` (`docs: document post-0.8 hardening`);
- upstream `hermes-lcm`: `v1.0.0-rc.1`, commit `8d1b1e6`;
- upstream `mnemosyne`: проверенный main snapshot `d738487`;
- последний полный suite: Python 3.14 — `328 passed, 4 skipped`;
  Python 3.12 — `312 passed, 7 skipped`;
- рабочее дерево до добавления этого документа было чистым.

Baseline не считается доказательством upstream parity. Он доказывает только
текущий unified-контракт.

## 1. Цель и границы

### Цель

1. Закрыть четыре подтверждённых P0-риска.
2. Сохранять существующие legacy-контракты.
3. Добавлять только те функции, которые дают измеримую ценность и не требуют
   молчаливого изменения API.
4. Не пытаться одновременно воспроизвести весь LCM/Mnemosyne ecosystem.

### Явные non-goals

- Не переносить все 40+ Mnemosyne tools автоматически.
- Не делать отдельный большой refactor `store.py`.
- Не менять `owner=""` legacy semantics: пустой owner по-прежнему видит всё.
- Не удалять глобальный `mem_doctor`.
- Не менять MCP dependency contract без отдельной story.
- Не добавлять `key.api` в Git, URL, commit, diff или историю.
- Не делать commit/push без отдельного явного запроса пользователя.
- Не переписывать старые данные redaction-ом автоматически.

## 2. Обязательные инженерные правила

### Последовательность

- Работы идут story-by-story.
- Сначала красный regression-тест, затем минимальный кодовый fix.
- Сначала P0, затем P1. P2 не начинается, пока P0 acceptance gate не пройден.
- Один story — один логический change set. Не смешивать P0 с новой feature
  в одном commit.

### API и совместимость

- Runtime API не меняется без явного раздела в story.
- Все новые параметры должны быть аддитивными и иметь безопасные defaults.
- Legacy owner/session поведение проверяется до и после изменения.
- Деривативные индексы не являются источником истины: SQLite parent rows —
  источник истины, FTS/vecidx восстанавливаются.

### Транзакционность

- Любая новая запись, update, archive, reindex или metadata mutation должна
  иметь определённую crash/retry semantics.
- При невозможности атомарности — backup-first либо staging/recovery path,
  документированный в коде и тестах.

### Проверки перед commit

Для каждой завершённой story:

```bash
/usr/bin/python3.14 -m pytest tests/ -q
/usr/bin/python3.12 -m pytest tests/ -q
git diff --check
```

Дополнительно для packaging/runtime изменений:

```bash
python3 -m pip wheel . --no-deps --wheel-dir /tmp/opencode/um-wheel
python3 -m venv /tmp/opencode/um-wheel-venv
/tmp/opencode/um-wheel-venv/bin/python -m pip install \
  /tmp/opencode/um-wheel/unified_memory_mcp-*.whl
/tmp/opencode/um-wheel-venv/bin/python -c \
  "import unified_memory; from unified_memory.server import mcp; print(unified_memory.__version__); print(type(mcp).__name__)"
```

### Коммиты и push

- Работа ведётся пачками: несколько последовательных story можно объединить
  в один commit/push после прохождения общей проверки.
- Commit и push выполняются только после явного разрешения пользователя на
  пакетную фиксацию; это разрешение не означает push после каждой story.
- До такой фиксации работа остаётся в рабочем дереве.
- Commit message должен начинаться с типа: `fix:`, `feat:`, `test:`,
  `docs:`, `ci:`.
- В commit не включаются generated `build/`, wheel, venv или временные файлы.
- Каждый пакет должен иметь понятный boundary и список входящих story.

## 3. P0 — закрыть до расширения функционала

### P0.1 — FTS scope starvation

**Проблема.** `Store.fts_search()` применяет `owner`, `session_id` и liveness
фильтры после глобального FTS `ORDER BY rank LIMIT`. При большом числе чужих
hits нужная сессия может вытесняться из кандидатов.

**Минимальный scope:**

- исправить FTS path без изменения `Router` public contract;
- фильтровать scope/owner/session/archived/expiry до или внутри bounded FTS
  plan;
- сохранить FTS5 ordering и snippet;
- проверить LIKE fallback parity;
- не менять ranking RRF/recency/MMR.

**Обязательные тесты:**

1. 100+ foreign messages и один current message с тем же термином;
2. те же проверки для owner A/owner B;
3. expired и archived rows не вытесняют live row;
4. FTS5 и LIKE fallback дают одинаковый scope contract;
5. short query fallback и snippet не ломаются.

**Acceptance:**

- current/owner/live hit присутствует при global candidate overflow;
- нет post-filter starvation;
- существующие `test_audit6.py` и recall tests остаются зелёными;
- `mem_recall` не возвращает чужую сессию/owner.

**Rollback.** Изменение локально для `fts_search`; при неудаче откатить только
FTS query path, не трогая schema.

---

### P0.2 — Compaction pressure runaway

**Проблема.** Текущий `pressure` включает raw messages и active summaries, но
после compaction токены покрытых raw messages не вычитаются. После первого
threshold новое сообщение может снова вызвать compaction только из-за старого
raw backlog.

**До coding decision.** Зафиксировать семантику:

- `raw_backlog_tokens` — сколько токенов ещё не покрыто compaction;
- `active_summary_tokens` — токены live summaries;
- `assembled_tokens` — фактический budget активного окна;
- `archive_tokens` — отдельная cold-storage метрика, не pressure.

**Минимальный scope:**

- исправить accounting так, чтобы covered raw messages не удерживали active
  pressure бесконечно;
- сохранить `frontier` и гарантию «каждое сообщение покрывается максимум один
  раз»;
- сохранить transactionality compaction;
- не менять публичный формат ответа без необходимости; поля можно добавить
  аддитивно.

**Обязательные тесты:**

1. серия сообщений после первого threshold;
2. отсутствие single-message summary storm;
3. каждое raw message покрывается не более одного раза;
4. rollback не оставляет summary при ошибке summarizer;
5. FTS/raw lossless recovery не меняется;
6. owner/session pressure раздельны.

**Acceptance:**

- pressure bounded agreed metric;
- compaction не срабатывает только из-за уже покрытых raw rows;
- `raw messages` остаются в store до явного archive/forget;
- legacy `mem_compact` и auto-compaction работают вместе.

**Rollback.** Вернуть старый counter только если новая метрика ломает
существующий контракт; при этом не принимать silent regression.

---

### P0.3 — Embedding model-change recovery

**Проблема.** `Store.__init__()` правильно запрещает смешивание vector spaces,
но при несовпадении model/dim MCP server не поднимается, поэтому
`mem_reindex` невозможно вызвать.

**Минимальный scope:**

- сохранить строгий `DimensionMismatchError` для обычного запуска;
- добавить отдельный recovery path для смены модели;
- recovery должен переэмбедить все поддерживаемые vector rows;
- обновлять `embedding_model`/`embedding_dim` только после успешной полной
  обработки;
- пересобирать FTS/vecidx только из успешного результата;
- не менять старые vectors наполовину.

**Предпочтительный API:** отдельный CLI/recovery command или явный
`reindex --replace-model`; не делать silent fallback на старую модель.

**Обязательные тесты:**

1. wrong model/dim не смешивается с обычными writes;
2. recovery re-embeds messages/facts/edges/entities;
3. crash/error оставляет старый stamp и не создаёт partial replacement;
4. повторный recovery идемпотентен;
5. `mem_reindex` продолжает работать для missing vectors в обычном режиме;
6. server smoke проходит с FTS-only backend.

**Acceptance:**

- после успешного recovery vectors принадлежат новой модели;
- `mem_recall` не смешивает old/new vector spaces;
- recovery доступен без доступа к уже поднятому MCP server;
- ошибки loudly возвращаются и не оставляют store в промежуточном состоянии.

---

### P0.4 — Secure local DB/archive permissions

**Проблема.** При `umask 022` DB и archive могут получить `0644`, а каталоги —
`0755`. Для single-user local mode это допустимо, для shared/multi-user — нет.

**Минимальный scope:**

- DB directory: `0700` при создании;
- main DB и archive DB: `0600`;
- SQLite auxiliary files (`-wal`, `-shm`) также должны быть restricted;
- уже существующие файлы не переписываются молча без явной policy;
- `mem_status` может сообщать permission diagnostics без содержимого.

**Обязательные тесты:**

1. newly created DB/archive modes;
2. existing DB не меняется неожиданно;
3. WAL/SHM permissions;
4. archive path отдельно;
5. Windows/no-op режим, если применимо, не падает.

**Acceptance:**

- новый single-user store создаётся private;
- `owner`-изоляция не считается заменой filesystem permissions;
- функциональность и tests не требуют root.

## 4. P1 — функции после P0

### P1.1 — `mem_get` и `mem_inspect`

`mem_get` — точечное чтение metadata/body по `kind:id` без полного recall.

`mem_inspect` — read-only диагностика:

- summary DAG и frontier;
- covered/uncovered ranges;
- source lineage, когда появится;
- archive status;
- vector/fts/index status;
- owner/session dimensions;
- orphan/dangling diagnostics.

`mem_expand` остаётся для body/recovery. Не смешивать inspect с mutation.

### P1.2 — Source lineage и transcript recovery

- хранить source message IDs для summary nodes;
- добавить summary child/source manifest;
- `mem_expand(summary)` возвращает lineage manifest;
- `mem_load_session` с cursor pagination;
- source filter в recall;
- сохранять LCM source/tool metadata при миграции.

Не переносить весь LCM `query_view/evidence controller` автоматически.

### P1.3 — Archive-aware recall

- отдельный archive FTS или bounded archive scan;
- `include_archived`/явный archive query;
- сохранение owner/session/scope filters;
- `mem_expand` по-прежнему быстрый exact path;
- archive остаётся отдельным cold-файлом: hot `export` не включает archive rows/bytes, а `import` не создаёт и не восстанавливает архив; archive recovery идёт через `mem_expand`.

### P1.4 — Importance-aware ranking

- добавить конфигурируемый bounded importance component;
- default сохраняет текущий ranking, если отдельно не согласован иной default;
- `importance=0.95` не означает unconditional top;
- component — bounded centered multiplier `1 + weight × (importance − 0.5)`, weight ∈ `[0,1]`; non-fact rows используют neutral `0.5`;
- diagnostics показывает вклад importance;
- eval fixture расширяется отдельным ranking case.

### P1.5 — `mem_graph_query`

- subject/predicate/object filters;
- `as_of`;
- max hops;
- edge relation/weight;
- deterministic order;
- owner/session/liveness semantics;
- no implicit cross-owner traversal.

Контракт P1.5: `mem_graph_query` возвращает bounded deterministic `{edges, links, truncated}`;
subject/predicate/object — exact case-insensitive filters для `um_edges`, `rel`/`min_weight` — для
`um_links`; `as_of` использует `[created_at, valid_until)`, `include_expired` включает историю,
`max_hops` ограничен `UM_RECALL_MAX_HOPS`, а непустой `owner`/`session_id` не обходятся.

### P1.6 — Upstream migration adapters

Сначала поддержать dry-run и reconciliation report.

LCM adapter:

- raw messages;
- session/source/conversation;
- tool call metadata;
- source ordering;
- summary strategy: recompute by default, preserve only after explicit decision;
- archive/externalized payload policy.

Mnemosyne adapter:

- durable memory rows;
- canonical facts and history;
- triples/edges;
- bank/owner mapping;
- metadata/confidence/veracity where present;
- working/episodic rows with explicit policy.

Acceptance migration:

- no partial import;
- row counts and representative recall checks;
- no source secrets copied into logs;
- explicit list of skipped fields.

P1.6 contract: оба source SQLite открываются только в read-only URI mode; apply выполняется
в одной транзакции, а отчёт содержит counts/field names/digests, но не source content, tool payload
или credentials. LCM сохраняет source ordering/conversation/tool metadata и по умолчанию
пересчитывает summaries; preserve требует явного флага. Mnemosyne переносит canonical fact history,
triples/edges и owner/bank mapping; working/episodic rows требуют явной policy, derived/unmapped
таблицы перечисляются в skipped report.

## 5. P2 — только после P1 parity gate

Не начинать без отдельного use case:

- working-memory TTL и automatic context injection;
- banks/shared/private memory;
- collaborative validate/attest;
- persona;
- scratchpad/task progress;
- annotations;
- media;
- sync/encryption;
- LLM entity/triple extraction;
- adaptive retrieval;
- full LCM evidence controller.

## 5.1 P2.1 — Working-memory TTL

**Use case:** агент в рамках сессии кладёт временное (параметры задачи, промежуточные
решения), и оно само исчезает из `mem_recall`/`mem_assemble` по истечении, без
ручного `mem_forget`/`mem_update`.

**Проблема:** временные рабочие факты остаются в recall и assembly до явного
ручного удаления и создают лишнее давление/шум.

**Пользовательский эффект:** temporary working fact имеет bounded lifetime;
до `valid_until` он участвует в обычном recall, после дедлайна скрывается из
обычного recall/assembly, но остаётся доступен через `include_expired=true`.
В `mem_assemble` bounded working-срез включается только opt-in через
`include_working=true`; default/false сохраняет прежнее поведение.

**Scope:**

- без новых таблиц: working — обычный `um_facts` slot-fact;
- `valid_until = created_at + ttl_s`; `ttl_s <= 0` означает бессрочный fact;
- `UM_WORKING_TTL_S` и `UM_WORKING_LIMIT` через strict integer config;
- `mem_fact`/`upsert_fact` принимает TTL для `category=working`;
- переиспользовать существующий expire lifecycle: vector удаляется, FTS остаётся;
- lazy expiry на recall/assemble read-path, без daemon/cron;
- opt-in bounded working assembly;
- сохранить legacy `owner=""` semantics.

Ограничение текущей схемы: `um_facts` не имеет `session_id`, а новые таблицы запрещены;
поэтому working assembly имеет owner-scope, а не отдельный session partition.

**Non-goals:**

- server push, background worker и scheduler;
- изменение owner isolation;
- banks/shared/private, persona, scratchpad, annotations;
- изменение Mnemosyne migration policy для `working_memory`;
- новая схема/entity type или новый lifecycle storage.

**Красные тесты:**

- working fact жив до deadline, скрыт из обычного recall после deadline и виден
  с `include_expired=true`;
- `mem_assemble(include_working=true)` возвращает bounded working-срез в пределах
  budget; `include_working=false`/default совпадает с legacy;
- expired working rows не раздувают `pressure()` и не вызывают summary storm;
- `owner=""` behavior не меняется.

**Acceptance criteria:**

- TTL bounded и валиден для category `working`; нерабочие категории не получают
  скрытый TTL side effect;
- expiration не удаляет FTS-историю и удаляет vector через существующую
  expire-ветку;
- обычный recall/assemble не возвращает expired working rows;
- explicit include-expired retrieval сохраняет lineage/history semantics;
- default ranking, legacy owner semantics и archive behavior не меняются;
- migration `working_memory → skip` остаётся без изменений.

**Rollback/failure behavior:** ошибка config validation или невалидный TTL
отклоняется до записи; lazy expiry не требует фоновой миграции и безопасно
обрабатывает malformed/legacy rows; transaction/vector/FTS paths используют
существующие atomic операции.

**Docs/config impact:** `README.md`, `docs/IMPORT.md` не меняют migration policy;
документируются новые config knobs и opt-in assembly behavior.

**Validation:** полный suite на Python 3.14 и 3.12, `git diff --check`, wheel
build/install/import smoke из clean venv.

## 6. Story template

Каждая story должна иметь этот блок до начала кода:

```text
ID:
Проблема:
Пользовательский эффект:
Scope:
Non-goals:
Красные тесты:
Acceptance criteria:
Rollback/failure behavior:
Docs/config impact:
Validation:
```

## 7. Stop conditions

Остановить story и не кодить дальше, если:

- непонятно, меняем ли мы legacy behavior;
- acceptance criterion требует silently выбрать модель, budget или security policy;
- тест зависит от unavailable upstream/network без явного skip;
- изменение затрагивает `key.api`, secrets или remote URLs;
- полный suite уже был красным до начала story;
- требуется массовый refactor вместо локального fix.

## 8. Status log

| Story | Статус | Commit | Notes |
|---|---|---|---|
| Baseline audit | done | `43d547a` | 328/4 and 312/7 recorded |
| P0.1 FTS scope starvation | done (uncommitted) | — | parent-table scope join before FTS LIMIT; session/owner regressions; full suite 330/4 (3.14), 314/7 (3.12) |
| P0.2 compaction pressure | done (uncommitted) | — | raw/live-summary counters, compactable backlog trigger, min-batch guard; archive/import invalidation; full suite 335/4 (3.14), 319/7 (3.12); wheel smoke OK |
| P0.3 model recovery | done (uncommitted) | — | offline `python -m unified_memory.reembed`; atomic full replacement, rollback, summaries/owner coverage; full suite 339/4 (3.14), 323/7 (3.12); wheel smoke OK |
| P0.4 secure artifacts | done (uncommitted) | — | new private DB/archive parents and SQLite artifacts; existing permissions preserved; full suite 341/4 (3.14), 325/7 (3.12); wheel smoke OK |
| P1.1 mem_get/inspect | done (uncommitted) | — | exact metadata/vector/links lookup; store/session diagnostics; 17-tool MCP smoke; full suite 342/4 (3.14), 326/7 (3.12); wheel smoke OK |
| P1.2 lineage | done (uncommitted) | — | lineage table, mem_expand, mem_load_session, export/import, source-filter recall; full suite 346/4 (3.14), 330/7 (3.12); wheel smoke OK |
| P1.3 archive recall | done (uncommitted) | — | explicit include_archived bounded lexical scan; owner/session/source/scope filters; no archive creation on read; full suite 351/4 (3.14), 335/7 (3.12); wheel smoke OK |
| P1.4 importance | done (uncommitted) | — | optional bounded centered multiplier for facts; UM_IMPORTANCE_WEIGHT default 0; diagnostics + eval ranking case; full suite 356/4 (3.14), 340/7 (3.12); wheel smoke OK |
| P1.5 graph query | done (uncommitted) | — | mem_graph_query with exact edge filters, typed-link rel/min_weight, as_of/liveness, bounded max_hops, owner/session isolation, deterministic edges/links; full suite 360/4 (3.14), 344/7 (3.12); wheel smoke OK |
| P1.6 migration | done | `5dbf1c6` | LCM + Mnemosyne read-only SQLite adapters: dry-run default, atomic apply, reconciliation + recall checks, LCM source ordering/tool metadata, Mnemosyne fact history/graph/owner mapping, explicit working/episodic policies, skipped-field report; full suite 369/4 (3.14), 353/7 (3.12), wheel smoke OK |
| P2.1 working TTL | done (uncommitted) | — | working slot-fact TTL, lazy expiry through existing vector/FTS lifecycle, opt-in bounded assembly, owner semantics preserved; full suite 375/4 (3.14), 359/7 (3.12), wheel smoke OK |

## 9. Final gate

A release/migration decision is allowed only when:

1. all P0 stories pass their acceptance criteria;
2. full suite is green on Python 3.14 and 3.12;
3. wheel builds and imports from a clean venv;
4. `git diff --check` is clean;
5. migration adapters have dry-run reconciliation;
6. operational docs and README match the runtime surface;
7. rollback procedure is documented and tested;
8. user explicitly requests commit and push.
