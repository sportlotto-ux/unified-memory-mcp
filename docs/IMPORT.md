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

Mnemosyne adapter — следующий atomic story P1.6b. До его появления migration
команда намеренно принимает только `--format lcm`; прямой импорт
`mnemosyne.db` не выполняется.

Политика будущего adapter:

- canonical facts + version history;
- triples/edges с owner/bank mapping;
- confidence/veracity/metadata, когда присутствуют;
- working/episodic rows — только через явную policy;
- annotations/persona/scratchpad/vector blobs — отдельный skip report;
- тот же read-only dry-run, atomic apply, row-count reconciliation и recall checks.

## Проверка паритета

После apply отчёт содержит `reconciliation.counts_match` и representative
recall checks. Дополнительно рекомендуется 10–15 фрагментов исходных воспоминаний:
`mem_recall` должен вернуть их в топ-3. Решение по дискордантным парам, не по
проценту «совпавших» строк.
