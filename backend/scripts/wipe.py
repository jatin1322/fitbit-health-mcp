"""Backend wipe: delete all health rows and sync state from the database.

This touches neither the phone nor Health Connect. Afterwards, run "Full resync"
in the phone app to rebuild the backend. OAuth tokens are left alone, so the
Claude connector stays signed in.

Usage: DATABASE_URL=... python scripts/wipe.py --yes-delete-all-health-data
"""

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, delete

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import HEALTH_TABLES, normalize_url, sync_state  # noqa: E402

FLAG = "--yes-delete-all-health-data"


def wipe(database_url: str) -> dict[str, int]:
    engine = create_engine(normalize_url(database_url))
    counts = {}
    with engine.begin() as conn:
        for table in (*HEALTH_TABLES, sync_state):
            counts[table.name] = conn.execute(delete(table)).rowcount
    engine.dispose()
    return counts


def main(argv: list[str]) -> int:
    if argv != [FLAG]:
        print(f"Refusing to run. This deletes ALL health data. Pass exactly {FLAG} to confirm.", file=sys.stderr)
        return 2
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    for name, n in wipe(url).items():
        print(f"{name}: deleted {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
