"""Offline embedding-model recovery.

Run with ``python -m unified_memory.reembed`` when the configured embedding
model changed and the normal MCP server refuses to open a mixed vector store.
"""

from __future__ import annotations

import argparse
import json

from .config import Config, load
from .embeddings import make_backend
from .ingest import Ingest
from .store import Store


def reembed(cfg: Config | None = None, batch: int = 64) -> dict:
    """Re-embed every source row and atomically update the model stamp."""
    cfg = cfg or load()
    if batch <= 0:
        raise ValueError("batch must be > 0")
    backend = make_backend(cfg)
    backend.warm()
    # Do not pass the active dimension here: the point of this command is to
    # recover a store whose existing model stamp intentionally mismatches.
    store = Store(cfg)
    try:
        return Ingest(store, backend, cfg=cfg).reembed(batch=batch)
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m unified_memory.reembed",
        description="Atomically re-embed all source rows after a model change.",
    )
    parser.add_argument("--batch", type=int, default=64,
                        help="documents per embedding batch (default: 64)")
    args = parser.parse_args(argv)
    if args.batch <= 0:
        parser.error("--batch must be > 0")
    print(json.dumps(reembed(batch=args.batch), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
