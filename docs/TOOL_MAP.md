# TOOL_MAP — `lcm_*` + `mnemosyne_*` → `mem_*`

## Прямые переименования (1:1)

| Новый тул | Было | Источник |
|---|---|---|
| `mem_remember` | `mnemosyne_remember` / `shared_remember` | M/mcp_tools |
| `mem_forget` | `mnemosyne_forget` / `shared_forget` | M |
| `mem_expand` | `lcm_expand` (+`lcm_load_session`, `lcm_describe`) | L |
| `mem_status` | `lcm_status` (+`mnemosyne_stats`) | L+M |
| `mem_doctor` | `lcm_doctor` (+`mnemosyne_diagnose`, hygiene_*) | L+M |

## Слияния (N:1 — здесь выигрыш)

| Новый тул | Поглощает | Решение |
|---|---|---|
| `mem_recall` | `lcm_recall` + `lcm_grep` + `mnemosyne_recall` | Один recall: FTS + vectors + RRF + recency-приор + scope-bias + MMR; `scope:` выбирает all/session/facts. Убивает главный дубль |
| `mem_recent` | `lcm_recent` + LCM rollup-периоды | Отдельный temporal-тул: UTC-окна как у LCM (`today`/`week`/`Nd`/`date:`/`last Nh`), поверх messages+summaries |
| `mem_evidence` | `lcm_query_state` + `lcm_compute` + `lcm_compile_evidence` + `lcm_evidence_pack` + `lcm_retrieve` | Evidence-семья LCM (5 тулов) — сжать до 1–2 с режимами |
| `mem_fact` | `remember_canonical` + `recall_canonical` + `triple_add/query` + `graph_*` | Факты/граф одной группой вместо 6 тулов (триплет — параметрами `subject/predicate/object`); canonical-слот как в mnemosyne |
| `mem_update` | `update` + `invalidate` (mnemosyne) + `triple_end` | Правка факта по id (supersede-цепочка) + истечение/reopen факта/ребра (`valid_until`) |
| `mem_assemble` | (новое, наследник LCM assembly) | Bounded активный контекст: summaries + fresh tail |
| `mem_compact` | ручной триггер LCM-compaction | + авто-компакшн в `mem_remember` по порогу давления |

> `mem_evidence` (сжатие evidence-семьи LCM: query_state/compute/compile/evidence_pack/retrieve в 1–2 тула) — **отложено**, в v0.3 нет. Не обещаем то, чего нет.

## Падающие (не переносим)

- `lcm_expand_query`, `lcm_inspect`, `lcm_compute` (отдельно) — покрыты `mem_recall`/`mem_evidence`/`mem_status`
- `scratchpad_*`, `sleep`, `import/export`, `validate/invalidate`, `get`, `update` — ops-хвосты mnemosyne; нужное впитывает `mem_doctor`/`mem_remember`
- `lcm_grep content_scope=externalized` — режим `mem_expand`, не отдельный тул

## Итого поверхность: ~40 тулов → ~8

`mem_remember mem_recall mem_expand mem_fact mem_update mem_compact mem_assemble mem_forget mem_status mem_doctor mem_reindex mem_recent`
(плюс отложенный `mem_evidence`)
