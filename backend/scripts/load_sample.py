"""POST sample_data/*.json to an ingest endpoint as one full sync run.

Usage:
  PHONE_INGEST_SECRET=... python scripts/load_sample.py https://<host>
"""

import json
import os
import sys
import uuid
from pathlib import Path

import httpx

DATA = Path(__file__).resolve().parents[1] / "sample_data"
BATCH = 500


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: load_sample.py <base-url>")
    base = sys.argv[1].rstrip("/")
    secret = os.environ.get("PHONE_INGEST_SECRET")
    if not secret:
        sys.exit("PHONE_INGEST_SECRET is required")
    headers = {"Authorization": f"Bearer {secret}"}
    run_id = str(uuid.uuid4())
    parts = {name: json.loads((DATA / f"{name}.json").read_text()) for name in ("sleep", "daily_metrics", "exercise")}
    batches = [
        {name: rows[i : i + BATCH]} for name, rows in parts.items() for i in range(0, len(rows), BATCH)
    ]
    with httpx.Client(timeout=30) as client:
        for n, batch in enumerate(batches):
            body = {"run_id": run_id, **batch}
            if n == 0:
                body["full_resync_begin"] = True
            r = client.post(f"{base}/v1/ingest", json=body, headers=headers)
            r.raise_for_status()
            print(f"batch {n + 1}/{len(batches)}: {r.json()}")
        last_day = parts["daily_metrics"][-1]["date"]
        r = client.post(
            f"{base}/v1/ingest",
            json={
                "run_id": run_id,
                "run_complete": {"data_through": f"{last_day}T23:59:00+05:30", "full_resync": True},
                "run_report": {"result": "ok", "duration_ms": 1000},
            },
            headers=headers,
        )
        r.raise_for_status()
        print(f"run complete: {r.json()}")


if __name__ == "__main__":
    main()
