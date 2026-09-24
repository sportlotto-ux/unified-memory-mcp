# IMPORT — переезд с hermes-lcm / mnemosyne

## Общий контракт P1.6

Upstream SQLite snapshots открываются через read-only URI. Unified не создаёт и не
изменяет source DB. По умолчанию migration работает в dry-run и печатает только
counts, skipped field names и reconciliation metadata — без source content, tool
payloads и секретов. Apply запускается явно и выполняется в одной транзакции:
любая ошибка откатывает весь import.

LCM adapter уже поддержан в `src/unified_memory/migration.py`:

```bash
# plan/report; target DB берётся из UM_DATABASE_PATH
python -m unified_memory.migration \
  --format lcm --input /path/to/lcm.db

# explicit atomic apply
python -m unified_memory.migration \
  --format lcm --input /path/to/lcm.db --apply
```

Опции summary strategy:

```bash
# default: raw messages only; Unified пересчитает summaries по pressure
python -m unified_memory.migration --format lcm --input lcm.db

# explicit preservation of LCM summary_nodes
python -m unified_memory.migration \
  --format lcm --input lcm.db --summary-strategy preserve --apply
```

`messages` переносятся в порядке `store_id`. В unified message сохраняются
`conversation_id`, `source_order`, `source_ref` и redaction-gated metadata для
`tool_call_id`, `tool_name`, `tool_calls` и related LCM fields. Source payload не
попадает в migration report. Пустые content rows и отключённые source tables
явно отмечаются в `skipped_fields`; это не молчаливый partial import.

`summary_nodes` по умолчанию не копируются: сохраняется raw transcript, а Unified
summary pipeline может пересчитать summaries. `preserve` — только явное решение
оператора; при нём source IDs переводятся в `um_summary_sources`.

## Из mnemosyne

```bash
# plan/report; target DB берётся из UM_DATABASE_PATH
python -m unified_memory.migration \
  --format mnemosyne --input /path/to/mnemosyne.db \
  --owner-map '{"bank-a":"tenant-a"}' --default-owner tenant-a

# explicit atomic apply
python -m unified_memory.migration \
  --format mnemosyne --input /path/to/mnemosyne.db \
  --owner-map '{"bank-a":"tenant-a"}' --default-owner tenant-a --apply
```

Mnemosyne adapter импортирует:

- `canonical_facts` как versioned fact slots;
- `memoria_facts` как durable key/value facts;
- `triples` и `graph_edges` как entity graph;
- `consolidated_facts` как graph candidates, если они не дублируют `triples`;
- `metadata`, `confidence`, `veracity` и source references.

`facts` и `memoria_kg` — derived projections, поэтому они не импортируются
повторно и явно попадают в `skipped_fields`. `working_memory`, `episodic_memory`
и `memories` по умолчанию пропускаются. Message rows переносятся только через
явную policy:

```bash
python -m unified_memory.migration \
  --format mnemosyne --input mnemosyne.db --apply \
  --working-policy message --episodic-policy message --memory-policy message
```

`annotations`, persona, instructions, preferences, timelines, scratchpad,
validation/conflict tables и vector blobs не переносятся автоматически; их
counts и причины skip видны в report. Source content, metadata payloads и
credentials не попадают в stdout migration report.

P2.1 Working-memory TTL не меняет эту policy: upstream `working_memory` по-прежнему
не импортируется автоматически. TTL применяется только к фактам Unified с
`category=working`, записанным через `mem_fact`.

P2.2 Unified annotations — отдельный слой (`um_annotations`): upstream-таблица
`annotations` по-прежнему `not_in_scope` и не импортируется; пометки Unified
переживают собственный export/import с remap целей.

## Проверка паритета

После apply отчёт содержит `reconciliation.counts_match` и representative
recall checks. Дополнительно рекомендуется 10–15 фрагментов исходных воспоминаний:
`mem_recall` должен вернуть их в топ-3. Решение по дискордантным парам, не по
проценту «совпавших» строк.
