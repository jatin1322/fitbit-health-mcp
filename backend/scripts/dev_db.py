"""Start (or reuse) a local embedded PostgreSQL for development and print its URL.

Usage: python scripts/dev_db.py   ->   postgresql://postgres:@/postgres?host=...
Requires the dev extra (pgserver). Data lives in ~/.cache/fitbit-mcp-devdb
(pgserver cannot use a directory whose path contains spaces).
"""

from pathlib import Path

import pgserver

DATA = Path.home() / ".cache" / "fitbit-mcp-devdb"

if __name__ == "__main__":
    server = pgserver.get_server(DATA, cleanup_mode=None)
    print(server.get_uri())
