"""Private filesystem defaults for local SQLite stores."""

from __future__ import annotations

import os
from pathlib import Path


def ensure_private_parent(path: str | Path) -> None:
    """Create a missing parent directory as 0700 without rewriting an existing one."""
    parent = Path(path).parent
    existed = parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        os.chmod(parent, 0o700)


def restrict_new_sqlite_files(db_path: str | Path,
                              seen: set[Path]) -> None:
    """Chmod newly created SQLite files to 0600; leave pre-existing files alone."""
    base = str(db_path)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{base}{suffix}")
        if not candidate.exists():
            seen.discard(candidate)
            continue
        if candidate in seen:
            continue
        os.chmod(candidate, 0o600)
        seen.add(candidate)
