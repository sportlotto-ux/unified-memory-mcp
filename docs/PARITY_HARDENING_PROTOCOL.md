# PARITY_HARDENING_PROTOCOL — регламент реализации unified-memory

Статус: **P0.1–P0.3 реализованы локально; изменения ещё не закоммичены**.

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
- export/import archive behavior определить отдельной строкой.

### P1.4 — Importance-aware ranking

- добавить конфигурируемый bounded importance component;
- default сохраняет текущий ranking, если отдельно не согласован иной default;
- `importance=0.95` не означает unconditional top;
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
| P0.4 secure artifacts | pending | — | single-user policy explicit |
| P1.1 mem_get/inspect | pending | — | after all P0 |
| P1.2 lineage | pending | — | after P1.1 |
| P1.3 archive recall | pending | — | after lineage decision |
| P1.4 importance | pending | — | default-compatible |
| P1.5 graph query | pending | — | exact graph contract |
| P1.6 migration | pending | — | dry-run first |

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
