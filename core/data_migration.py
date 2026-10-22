"""SQLite snapshot copying for legacy data migration, including committed WAL."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time


def copy_database_snapshot(source: Path, destination: Path, *, timeout: float = 15) -> bool:
    """Close both connections before publishing; never install a partial snapshot."""
    deadline = time.monotonic() + timeout

    def check_progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("旧数据库仍被占用或快照超时，请稍后重试迁移")

    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".mw-migrate-", suffix=".db", delete=False) as scratch:
        backup_path = Path(scratch.name)
    try:
        with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as source_db:
            with contextlib.closing(sqlite3.connect(backup_path)) as target_db:
                source_db.backup(target_db, pages=256, progress=check_progress, sleep=.05)
        if destination.exists():
            return False
        os.replace(backup_path, destination)
        return True
    finally:
        backup_path.unlink(missing_ok=True)
