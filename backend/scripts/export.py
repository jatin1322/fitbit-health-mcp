"""Dump all health tables and sync state to a local JSON file (mode 0600).

Usage: DATABASE_URL=... python scripts/export.py backup.json
"""

import datetime as dt
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import HEALTH_TABLES, normalize_url, sync_state  # noqa: E402


def _default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def export(database_url: str, path: str) -> dict[str, int]:
    engine = create_engine(normalize_url(database_url))
    data = {}
    with engine.connect() as conn:
        for table in (*HEALTH_TABLES, sync_state):
            data[table.name] = [dict(r) for r in conn.execute(select(table)).mappings()]
    engine.dispose()
    # Create with owner-only permissions before any data is written.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, default=_default)
    return {name: len(rows) for name, rows in data.items()}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: export.py <output.json>", file=sys.stderr)
        return 2
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    for name, n in export(url, argv[0]).items():
        print(f"{name}: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
