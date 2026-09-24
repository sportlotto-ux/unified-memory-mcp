# Changelog

Все значимые изменения. Формат близок к Keep a Changelog; версии — SemVer.
Ранние версии (0.1–0.3) сжаты: это была внутренняя сборка до публикации.

## [Unreleased] — 0.9.0 development
### Added
- P1.1–P1.5 parity additions: metadata inspection, lineage, archive recall,
  bounded importance ranking, and `mem_graph_query`.
- P1.6: read-only LCM and Mnemosyne migration adapters with dry-run
  reconciliation, atomic apply, source ordering/provenance metadata, owner/bank
  mapping, and explicit memory/summary policies.
- P2.1: working slot-fact TTL with lazy expiry on recall/assemble, vector/FTS
  lifecycle preservation, `UM_WORKING_TTL_S`, `UM_WORKING_LIMIT`, and opt-in
  `mem_assemble(include_working=true)`.
- P2.2: `um_annotations` metadata-only layer with `mem_annotate`, `mem_get`
  exposure, `mem_forget(kind=annotation)`, target cascade, and export/import
  remap; recall/assembly ranking unchanged.
- P2.3: `mem_validate` read-only collation (cite if claim + conflicts over
  target and direct supports/contradicts neighbours + live links +
  annotations); verdict-free, `needs_judgment` always true.
- P2.4: `mem_task` progress over `category="task"` slot-facts (open/doing/
  blocked/done machine, metadata-only status, `metadata_json` carried over
  supersede); no new tables, recall ranking unchanged.
- P2.5: `mem_persona` profile over `category="persona"` slots (set upsert +
  bounded ordered get); no new tables, upstream `memoria_persona` stays
  `not_in_scope`.
- P2.6: `mem_extract` endpoint preview of triples for one ref (strict JSON,
  caps, no writes); reuses `UM_SUMMARIZER_*`, explicit error without endpoint.
- P2.7: bank sharing over facts (`bank` scope + `um_grants` read-only grants,
  `mem_bank_share/unshare`); visibility enforced on recall/get/expand/evidence
  boundaries; legacy semantics preserved.
- Migration metadata columns for source references, confidence, veracity, and
  tool metadata; export/import and `mem_get` preserve them.

### Data integrity
- Archive moves, atomic export/import validation, schema migrations, embedding writes,
  fact updates, BFS session scope, and compaction/condensation are crash-safe and
  transactional.
- `mem_update` redaction is applied before persistence; `mem_evidence(pattern=...)`
  has bounded pattern length and per-scan timeout (`regex`, 256 chars / 50ms).

### Retrieval and embeddings
- Embedding writes stamp model/dimension on first use and reject model or dimension
  changes instead of silently mixing vector spaces.
- FTS5/LIKE session behavior is aligned; BFS cannot traverse into nodes from another
  session when `scope="session"`.

### Packaging and CI
- MCP Python SDK contract is explicit: `mcp>=2.0,<3`; the package uses the canonical
  `MCPServer` API.
- CI now builds a wheel, installs it into a clean venv, and imports the packaged server.

### Previous post-0.8 fixes
- Импорт пересобирает vecidx (`build_vec_index()` после транзакции — внутри
  нельзя: коммит разорвал бы контур; backend не нужен) — knn-плечо видит
  импортированные вектора сразу, без ручного `mem_reindex`.
- Импорт валидирует dim векторов: чужая размерность → skip + `dim_mismatch`
  в отчёте (раньше recall.len-фильтр молча не находил); битый blob — туда же.
  Эталон: активный индекс → первый вектор стора → первый валидный вектор дампа.
- Курсор `recent()` получил kind-тайбрейкер полного (created_at, id)-тия между
  messages/summaries (порядок rank DESC, `next` отдаёт `before_kind`); legacy-
  вызов без kind — бит-в-бит старое поведение (группа исключена).
- `export_store` и `_secret_scan` идут под публичным `Store.read_locked()`
  (RLock, реентерабелен): консистентный проход без гонки с писателями
  FastMCP-тредов; цена — сериализация писателей на время скана.
- `archive.audit` URI-экранирует путь архива (`?`/`#` в имени больше не ломают
  `mode=ro`).

## [0.8.0] — golden-eval, дамп/импорт, bounded-движок, пагинация
Всё аддитивно, breaking-изменений нет. Порядок вех: E20 → A2 → D16+C13+B6 →
E21/E18/D15/D14/C12. E20 (hit@3=1.000, MRR=0.896) не сдвинулся за весь релиз.
### Added
- **E20 golden-eval** (`tests/test_eval.py`): базлайн качества recall до скоринговых
  правок — hit@3 + MRR, детерминизм (замороженные часы, фиксированные created_at),
  изоляция tmp-стором. Baseline v0.7.3 (n=24): hit@3 = 1.000, MRR = 0.896; пороги
  hit@3 ≥ 0.95, MRR ≥ 0.85. Вектор-плечо — синтетический `LexicalBackend`.
- **A2**: вектора на update/reopen. `Ingest.update_fact` переэмбеддивает новую версию
  (`superseded`) и reopened факт (вектор был удалён при expire, P4.8); batch-совместимо
  (`_commit=False`), `backend=None` — no-op.
- **D16**: экспорт — стриминговый JSONL (`um-export-jsonl`): header + `{table,row}` построчно;
  um_fts/um_vecidx исключены (производные), вектора base64; ридер понимает legacy single-JSON.
- **B6**: `python -m unified_memory.import_dump <file> [--owner] [--dry-run]` — fresh-id remap,
  аддитивность (skip-слот без перезаписи), owner-override, без re-redaction, отказ на
  неизвестную таблицу/версию; работает без backend; весь импорт транзакционен.
- **C13**: batch-op `remember` (сообщение) в `mem_batch`.
### Changed
- **D15**: `mem_assemble` читает только возможный хвост (`DESC/LIMIT`), а `mem_compact`
  ограничен `UM_COMPACT_MAX_MSGS` (дефолт 10000) вместо 1M-скана; выдача та же.
- **D14**: `mem_recent` — пагинация старых страниц (`before_id`+`before_ts` из `next`).
### Security
- **E21**: fuzz-свойства redaction (hypothesis, extra `dev`): нет совпадений каталога
  в выходе, идемпотентность, deadline против ReDoS.
### Internal
- **E18**: отдельный weekly CI-workflow на тяжёлых extras (+hypothesis), не required-check.
- **C12**: in-process smoke (in-memory MCP: 15 тулов + recall round-trip).
### Fixed
- Явный `weight=0.0` у линка больше не съедается дефолтом 1.0; `hops=None` в `Router`
  нормализуется в 1 (аудит v0.7.3, P4).

## [0.7.3] — быстрый набор по вердикту аудита-6
### Added
- **A1**: вес `um_links.weight` умножает графовый скор BFS (дефолт 1.0 — обратная совместимость).
- **A4**: FTS-плечо отдаёт bounded-сниппет (`Hit.snippet`), тело остаётся полным; показывается только для тел > 2000 символов, короткие — verbatim.
- **B7**: `mem_doctor(mode="archive_check")` — read-only сверка горячих заглушек с архивом (`stubs`/`archived_rows`/`orphans`).
- **B8**: `mem_doctor(mode="secret_scan")` — скан горячего стора каталогом redaction; отчёт `{pattern, kind, id}` **без значений**.
- **B9**: `PRAGMA wal_checkpoint(TRUNCATE)` в maintenance + `wal_bytes` в `mem_status`.
- **B10**: `UM_MAX_TEXT_CHARS` (дефолт 200000) — один гейт в `Ingest._clean`, громкий `ValueError` вместо тихой обрезки.
- **A5**: `hygiene()["duplicate_facts"]` — near-дубли живых фактов внутри owner (косинус ≥ 0.95, read-only, capped).
- **№22**: BFS graceful-skip для линков с удалёнными концами + счётчик `diagnostics.bfs.skipped_missing`.
- **C11**: поведенческие аннотации `ToolAnnotations` у всех 15 тулов (read-only/destructive/idempotent).
- **E19**: CI-матрица Python 3.11 / 3.12 / 3.13.
- `CHANGELOG.md`.

## [0.7.2] — внешний аудит: зелёный CI
### Fixed
- 🟥 Тесты зависели от **необъявленного `tiktoken`** (`test_auto_compact_on_pressure` падал на чистой инсталляции, CI красный). `tiktoken` объявлен extra `.[tokens]`; эвристика стала RU-aware (`ASCII/4 + не-ASCII/2` вместо занижающего `len//4`); suite герметичен.
- FTS-плечо: `ORDER BY rank` (bm25) + pushdown `owner_table` в SQL.
- Висячие `um_links` после удаления концов: `hygiene()["dangling_links"]` + чистка в `repair()`.
- Гонка ленивой `_ingest()` (FastMCP гоняет sync-тулы в тредах): `threading.Lock` + double-checked init.

## [0.7.1] — hygiene
### Changed
- P3.4(B): удалён мёртвый холодный поиск (`archive.search`, `include_archived`); холод — только `mem_expand`.
- P4.8: supersede/expire удаляют вектор старой версии факта (FTS остаётся для `include_expired`).
- P4.5: `_dedupe_live_slots` пропускается при наличии partial unique `ux_um_facts_live`.
### Added
- P3.5: `mem_doctor(mode="retention", apply)` — age-based вынос горячего старше `UM_RETENTION_DAYS`.

## [0.7.0] — типизированные связи, BFS, атомарный batch
### Added
- `um_links` + `mem_link` (ADR-001): типизированные связи `supports|contradicts|supersedes|derives_from`, lifecycle (`mem_forget`/`mem_update` kind=link).
- `mem_recall(hops, rel, diagnostics)`: BFS по `um_links ∪ um_edges` (hops>1) с guardrails распада/фан-аута.
- `mem_batch`: атомарный all-or-nothing (savepoint), dry-run, капы.
- `mem_doctor(mode="export")`; `schema_version` пишется в `um_meta`.
- 15 тулов.

## [0.6.0] — evidence
### Added
- `mem_evidence`: `cite` (supported/partial/unsupported) + `compute` (count/sum/min/max/avg/median) + `conflicts` (verdict-free кандидаты). Детерминированно, без LLM.

## [0.5.0] — слоты фактов, темпоральность, lossless-архив
### Added
- Слот-модель фактов (partial unique на живой слот), `valid_until` sentinel (0 = живой), `mem_update` (supersede/expire/reopen), `include_expired`.
- Temporaльный граф: `as_of`-срез (`valid_from=created_at`), `expand` с окном валидности.
- Retention + холодный архив: отдельный SQLite, lossless, заглушка `[archived]` + `mem_expand`; автоудаления нет (purge только вручную). Вариант «горячее окно».

## [0.4.0] — качество recall, изоляция, безопасность
### Added
- OpenAI-протокол backend (локальный model2vec-сервер Hermes, авто-детект dim).
- Redaction-гейт на входе (каталог LCM, default ON, forward-only).
- Ререйтинг: recency-приор, scope-bias, MMR-диверсификация.
- Изоляция пользователей (`owner` + фильтры, миграция legacy).
- `mem_recent` (LCM UTC-семантика периодов).
- sqlite-vec KNN-путь; `mem_doctor` hygiene/repair (backup-first).

## 0.1–0.3 — внутренняя сборка
### Added
- MCP-сервер (stdio), единый стор (SQLite WAL), FTS5 + fallback LIKE, embeddings.
- Граф сущностей/рёбер (`um_entities`/`um_edges`), 1-hop graph arm.
- ActiveWindow: давление в токенах, frontier, многоуровневый DAG-конденсат, `mem_assemble`.
- Аудиты 0.3.x: каскад `mem_forget`, session-leak саммари, trigram-fallback, строгий env, кэш токенизатора и др.
