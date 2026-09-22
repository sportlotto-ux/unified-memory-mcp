# MODULE_MAP — откуда что переезжает

Источники (только чтение, апстримы не патчим):
- `L = plugins/hermes-lcm` (v1.0.0-rc.1, ~65k строк)
- `M = venv/site-packages/mnemosyne` (mnemosyne-memory 3.15.1 + mnemosyne-hermes 0.5.0, ~43k строк)

## Ядро (shared, пишется с нуля)

| Новый модуль | Источники | Комментарий |
|---|---|---|
| `embeddings.py` ✅ | `L/embedding_provider.py` (1.7k) + `M/core/embeddings.py` | Этап 0 готов: fastembed + реестр + dim-guard. Cloud-бэкенды (voyage/ollama/OpenAI-compat) — этап 3 |
| `store.py` 🟡 | `L/store.py`+`db_bootstrap.py`+`vector_store.py` + `M/core/beam.py` (DDL-часть) | DDL-план готов; миграция данных — этап 1 |
| `ingest.py` ⬜ | `L/engine.py` (ingest) + `M/core/beam.py` (remember-путь) | Один пайплайн: сообщение → `um_messages` → DAG-нод + memory-записи |
| `router.py` ⬜ | `L/tools.py` (grep/retrieve-логика) + `M/core/polyphonic_recall.py`+`mmr.py` | RRF fusion FTS+vectors, scope_bias + recency prior |

## Движки (адаптеры этапа 1–2, затем перенос)

| Слой | LCM | Mnemosyne | Решение |
|---|---|---|---|
| Compaction/DAG | `L/compaction.py`+`dag.py`+`rollup_*` | — (нет) | Переезжает как есть |
| Temporal rollups | `L/rollup_*` | `M/memoria_timelines` | Оставить LCM-реализацию, timelines маппить в неё |
| Facts/canonical | `L/assertion_*` (V4 sidecar) | `M/core/canonical.py`+`facts` | Конфликтующая пара — решать на этапе 2 (кандидат: canonical-слоты + assertion-цитаты) |
| Triples/граф | — | `M/core/episodic_graph.py`+`triples` | Переезжает как есть |
| Conflict detect | `L/reconcile.py` | `M/core/llm_conflict_detector.py` | Две реализации — сравнить на своих данных, оставить одну |
| Hygiene | `L/maintenance.py` | `M/core/hygiene.py`+`doctor.py`+`repair.py` | `mem_doctor` вбирает обе |
| Sync/multi-tenant | `L/aux_session.py` | `M/core/sync*.py`+`banks.py`+`profiles.py` | Mnemosyne-блок переезжает (LCM одноместный) |

## Не переезжает

- `L/bench*/benchmarks/*`, `L/docs/banner.png` — мусор апстрима
- `M/core/importers/*` (cognee/honcho/mem0/...) — миграционный хлам, нужен один раз на этапе 1
- `M/core/local_llm.py`, `llm_backends.py` — за summarization отвечает хост (auxiliary model), не стор
