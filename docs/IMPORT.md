# IMPORT — переезд с hermes-lcm / mnemosyne

Unified store сознательно **не читает** `mnemosyne.db` / `lcm.db` напрямую:
схемы чужие и версионируются апстримами. Вместо этого — экспорт через их же тулы:

## Из mnemosyne

```python
# факты: mnemosyne_recall_canonical без фильтров (вывод большой — парсить из файла!)
# затем по каждой записи:
mem_fact(category=r["category"], name=r["name"], body=r["body"][:4000])
```

Что НЕ везти:
- `category='skill'` с телами SKILL.md — это bloat, а не память (см. причину
  canonical-bloat). Скиллы уже живут на диске + в skill-recall индексе.
- вектора (`memory_embeddings`) — модели/размерности не совпадут, будет
  `DimensionMismatchError`. Переэмбед происходит автоматически при `mem_fact`.

## Из hermes-lcm

```python
# постранично: lcm_load_session(session_id) -> mem_remember(...)
# саммари: lcm_describe(DAG) -> mem_compact() пересчитает свои
```

DAG-узлы 1:1 не переносятся (у LCM своя нумерация `covers_*`) — переносится
сырьё (`messages`), summaries пересчитываются `mem_compact`. Это дешевле и чище,
чем маппинг id.

## Проверка паритета

После импорта: 10–15 запросов-фрагментов самих воспоминаний → `mem_recall` должен
вернуть их в топ-3. Решение по дискордантным парам, не по «процентам сошлись».
