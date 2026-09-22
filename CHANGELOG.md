# Changelog

Все значимые изменения. Формат близок к Keep a Changelog; версии — SemVer.
Ранние версии (0.1–0.3) сжаты: это была внутренняя сборка до публикации.

## [Unreleased] — 0.7.3 (быстрый набор по вердикту аудита-6)
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
