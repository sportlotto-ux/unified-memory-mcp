"""Экспорт стора в JSON-файл (v0.7-п.7, read-only).

Пишем в `<db>.export-<ts>.json`, а НЕ инлайном в ответ тула: полный дамп —
контекстная бомба на большом сторе. Вектора — base64 (lossless). Архив не
включаем (отдельный файл, вечный холод). Стриминга в v1 нет — дамп целиком в память.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from .store import Store

# Контент-таблицы. um_vecidx — производный sqlite-vec индекс (пересборка
# build_vec_index), в дамп не входит.
_EXPORT_TABLES = ("um_messages", "um_summaries", "um_facts", "um_entities",
                  "um_edges", "um_links", "um_vectors", "um_fts", "um_meta")


def export_store(store: Store, path: str | Path | None = None) -> dict:
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = Path(path) if path else Path(
        f"{store._db_path}.export-{ts}.json")
    tables: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    for t in _EXPORT_TABLES:
        cols = [r[1] for r in store.conn.execute(f"PRAGMA table_info({t})")]
        rows = []
        for row in store.conn.execute(f"SELECT * FROM {t}"):
            d = dict(zip(cols, row))
            for k, v in d.items():
                if isinstance(v, (bytes, bytearray)):
                    d[k] = base64.b64encode(bytes(v)).decode("ascii")
            rows.append(d)
        tables[t] = rows
        counts[t] = len(rows)
    payload = {
        "schema_version": store.meta_get("schema_version"),
        "exported_at": time.time(),
        "source_db": store._db_path,
        "tables": tables,
    }
    text = json.dumps(payload, ensure_ascii=False)
    out_path.write_text(text, encoding="utf-8")
    return {"path": str(out_path), "bytes": len(text.encode("utf-8")),
            "counts": counts, "schema_version": payload["schema_version"],
            "archive_included": False, "streaming": False}
