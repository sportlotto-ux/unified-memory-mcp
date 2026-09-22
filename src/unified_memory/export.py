"""Экспорт стора в JSONL-файл (v0.8 D16, read-only, стриминговый).

Формат (строка = один JSON-объект, \n-разделённые):
  1-я строка — header: {"format":"um-export-jsonl", "schema_version", "exported_at",
                        "source_db", "counts"}
  далее      — {"table": <имя>, "row": {...}} по объекту на строку.

Что внутри: контент + um_links, вектора base64 (lossless). um_fts — исключён
(производный; перестраивается _fts_index при импорте, иначе пришлось бы ремаппить
его id). um_vecidx — исключён (пересборка через reindex). um_meta — дампится,
но импортёр читает из него только schema_version. Архив — вне дампа (отдельный
файл, вечный холод). Пишем курсором/чанками, не собирая дамп в память.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from .store import Store

FORMAT = "um-export-jsonl"

# Контент-таблицы в стабильном порядке. um_fts/um_vecidx — производные
# (см. docstring), в дамп не входят.
CONTENT_TABLES = ("um_messages", "um_summaries", "um_facts", "um_entities",
                  "um_edges", "um_links", "um_vectors", "um_meta")


def _encode_row(cols: list[str], row: tuple) -> dict:
    d = dict(zip(cols, row))
    for k, v in d.items():
        if isinstance(v, (bytes, bytearray, memoryview)):
            d[k] = base64.b64encode(bytes(v)).decode("ascii")
    return d


def export_store(store: Store, path: str | Path | None = None) -> dict:
    """JSONL-дамп. Имя по умолчанию: <db>.export-<ts>.jsonl."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = Path(path) if path else Path(f"{store._db_path}.export-{ts}.jsonl")
    counts = {t: store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in CONTENT_TABLES}
    header = {"format": FORMAT,
              "schema_version": store.meta_get("schema_version") or "1",
              "exported_at": time.time(), "source_db": store._db_path,
              "counts": counts}
    written = 0
    with out_path.open("w", encoding="utf-8") as f:
        line = json.dumps(header, ensure_ascii=False) + "\n"
        f.write(line)
        written += len(line.encode("utf-8"))
        for t in CONTENT_TABLES:
            cols = [r[1] for r in store.conn.execute(f"PRAGMA table_info({t})")]
            for row in store.conn.execute(f"SELECT * FROM {t}"):
                line = json.dumps({"table": t, "row": _encode_row(cols, row)},
                                  ensure_ascii=False) + "\n"
                f.write(line)
                written += len(line.encode("utf-8"))
    return {"path": str(out_path), "bytes": written, "counts": counts,
            "schema_version": header["schema_version"], "format": FORMAT,
            "archive_included": False, "streaming": True}
