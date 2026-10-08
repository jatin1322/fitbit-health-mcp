# Backend: ingest API, OAuth server and MCP server

This is one small FastAPI service, deployed as a single container. It does three things:

1. **Ingest:** receives uploads from the Android app (`POST /v1/ingest`, authenticated with `PHONE_INGEST_SECRET`).
2. **Authorize Claude:** runs a minimal single-user OAuth 2.1 authorization server, so the Claude custom connector can sign in. You approve with `MCP_AUTH_SECRET`.
3. **Serve Claude:** exposes **six read-only MCP tools** at `/mcp` (MCP Python SDK v2, stateless Streamable HTTP).

Data is stored in PostgreSQL. Everything is stateless apart from the database, so it runs fine on scale-to-zero hosts.

## Layout

| Path | What it does |
|---|---|
| `app/main.py` | App factory. Mounts MCP last at `/`, and adds security headers and a values-free access log. |
| `app/config.py` | Environment settings, validated without echoing values. |
| `app/db.py` | Tables (SQLAlchemy Core) and the engine. |
| `app/schemas.py` | Strict ingest models (`extra="forbid"`, enums, bounds). |
| `app/ingest.py` | `/v1/ingest` and `/v1/status`: versioned upserts, deletions, the full-resync sweep. |
| `app/auth.py` | OAuth server (`/authorize`, `/login`, `/token`, `/revoke`) and the access-token verifier. |
| `app/metrics.py` | Every aggregation rule, plus the read queries behind the tools. |
| `app/mcp_server.py` | The six MCP tools, with output schemas. |
| `scripts/` | `generate_sample.py`, `load_sample.py`, `export.py`, `wipe.py`, `dev_db.py` |
| `sample_data/` | 60 days of obviously fake data with edge cases. |
| `tests/` | 86 tests, run against real PostgreSQL. |

## Local development

```bash
cd backend
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest        # starts an embedded PostgreSQL 16 (pgserver) automatically
```

**Tests:**
- They use a real Postgres, never SQLite, because single-use codes, atomic refresh rotation and concurrent ingests need real transactions.
- Set `TEST_DATABASE_URL` to use your own Postgres instead of the embedded one.

**Running the server locally:**

```bash
export DATABASE_URL="$(.venv/bin/python scripts/dev_db.py)"   # embedded Postgres in ~/.cache/fitbit-mcp-devdb
export PHONE_INGEST_SECRET=$(openssl rand -hex 24) MCP_AUTH_SECRET=$(openssl rand -hex 24) \
       OAUTH_CLIENT_ID=claude-connector OAUTH_CLIENT_SECRET=$(openssl rand -hex 24) \
       PUBLIC_BASE_URL=http://127.0.0.1:8765 USER_TZ=Asia/Kolkata
.venv/bin/uvicorn app.main:create_app_from_env --factory --port 8765 --no-access-log
.venv/bin/python scripts/load_sample.py http://127.0.0.1:8765      # load fake data
```

pgserver can't start in a directory whose path contains spaces, which is why its data lives under `~/.cache`.

## Configuration

| Variable | Notes |
|---|---|
| `DATABASE_URL` | A PostgreSQL URL. For Neon, use the **direct** (unpooled) URL with `sslmode=require`. |
| `PHONE_INGEST_SECRET` | 32+ characters. Only used by the phone, for `/v1/ingest` and `/v1/status`. |
| `MCP_AUTH_SECRET` | 32+ characters. The owner password you type on the `/login` approval page. |
| `OAUTH_CLIENT_ID` | Any identifier, such as `claude-connector`. Entered in Claude's connector dialog. |
| `OAUTH_CLIENT_SECRET` | 32+ characters. Entered in Claude's connector dialog. |
| `PUBLIC_BASE_URL` | The exact public origin, with no path, such as `https://my-service-123.europe-west1.run.app`. |
| `USER_TZ` | An IANA timezone. It is the **only** definition of "today" for MCP queries. |
| `TRUST_PROXY` | `1` behind Cloud Run. The client IP for the login rate limit is then the **last** `X-Forwarded-For` entry. |
| `ACTIVE_SOURCE` | Optional, default `health_connect`. |
| `EXTRA_ALLOWED_HOSTS` | Optional extra Host header values for the MCP DNS-rebinding check. |

The three secrets must all differ, or the app refuses to start. Generate each with `openssl rand -hex 24`.

## HTTP API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness: `{"ok": true}`. Note it's `/health`, not `/healthz`, because Cloud Run reserves some `…z` paths. |
| POST | `/v1/ingest` | `Bearer PHONE_INGEST_SECRET` | Batch upload. Max 2 MB and 2000 items. Strict schema; 422 responses never echo values. |
| GET | `/v1/status` | `Bearer PHONE_INGEST_SECRET` | Sync state, coverage, `max_compute_seq` and `resync_started_at`, for the phone. |
| * | `/mcp` | OAuth bearer | MCP. Without a token it returns 401 with `WWW-Authenticate: Bearer resource_metadata=…`. |
| GET | `/.well-known/oauth-protected-resource[/mcp]`, `/.well-known/oauth-authorization-server` | none | OAuth discovery |
| GET/POST | `/authorize`, `/login`, `/token`, `/revoke` | — | The OAuth flow |

## OAuth design (single user)

- **Client:** one pre-registered **confidential** client. Dynamic client registration is off, the redirect URI must exactly match `https://claude.ai/api/mcp/auth_callback`, and PKCE S256 is required. The authorization response includes `iss` (RFC 9207).
- **Codes:** single-use, enforced with an atomic `UPDATE … WHERE used_at IS NULL`. Reusing a code revokes every token it produced.
- **Refresh tokens:** rotated on every use. Reusing an old refresh token after a 30-second race window revokes the whole token family.
- **Storage:** tokens and codes are stored only as SHA-256 hashes.
- **Login:** `/login` compares the owner secret in constant time and allows 5 failures per 15 minutes per client IP, tracked in the database.

## MCP tools (all read-only)

| Tool | Returns |
|---|---|
| `get_sleep(start_date, end_date)` | Sessions by wake-up date: start/end in the session's own offset, minutes in bed and asleep, `nap`, and stage minutes (null if the session has no stages). |
| `get_sleep_summary(days=7)` | Averages, min and max over main nights (naps excluded), plus the night count. |
| `get_heart_rate_trend(days=30)` | Daily avg/min/max/resting bpm. |
| `get_hrv_trend(days=30)` | Daily RMSSD in ms. |
| `get_activity(start_date, end_date)` | Daily steps, plus workouts with type, duration, distance, calories, avg/max HR and steps. |
| `get_data_status()` | Freshness (`current`), `history_complete`, known gaps, coverage per dataset and the last phone run. |

**Limits:** dates are `YYYY-MM-DD`, ranges cover at most 365 days, and `days` is 1–365.

**Output:** compact JSON plus a structured output schema. Missing data is omitted or `null`, never 0.

**Personal baselines:** so Claude compares against *your* normal instead of working it out from rows, every tool returns:
- **`<metric>_30d` next to each value:** the mean of the **previous** 30 days, excluding that day. For example, `resting_30d`, `hrv_30d`, `steps_30d`, and `asleep_30d` (main nights only, never naps).
- **A `baseline` block:** the 30-day normal ending today (`USER_TZ`). It contains the `mean`, the typical range (`typical_low` and `typical_high`, the 10th and 90th percentiles), `days_with_data` and the `window`.
- **Covered metrics:** resting and average HR, HRV, asleep/deep/REM minutes, and steps.
- **Thin history:** both are `null` when fewer than 7 days have data, so a thin baseline never misleads the model.

## Semantics (`app/metrics.py`)

- **Stages:**
  - LIGHT/DEEP/REM map to light/deep/rem.
  - AWAKE, AWAKE_IN_BED and OUT_OF_BED map to awake.
  - SLEEPING (unstaged) counts as asleep.
  - UNKNOWN maps to unknown.
- **Stage cleanup:**
  - Stages are clipped to the session and sorted by start.
  - When stages overlap, the earlier one keeps the overlap.
  - Time not covered by any stage counts as unknown.
- **Asleep time:** light + deep + rem + unstaged. A session with no stages counts as entirely asleep, with null stage fields.
- **Main sleep:** for each wake date, the session with the most time asleep is the night's main sleep. Ties go to the earlier start; every other session is a nap.
- **Dates:**
  - A sleep belongs to the local date of its end, using the record's own offset. An exercise belongs to the local date of its start.
  - "Today" and `days=N` windows use `USER_TZ`.
  - Daily rows use the phone's calendar day.
- **HRV** is computed on the phone: the mean of the RMSSD readings per day. Readings taken during sleep count toward that sleep's wake-up date.
- **Rounding:** summaries are arithmetic means over nights with data, rounded half up; HRV is rounded to 0.1 ms.
- **Versioning:**
  - A row is replaced only if `(source_last_modified, compute_seq)` is ≥ the stored pair.
  - `compute_seq` is a per-run counter from the phone, kept in step with the server through `/v1/status`.
  - Clocks are never used for ordering.

## Full resync (server-owned boundary)

1. The first batch carries `full_resync_begin`. The server records `resync_started_at` with **its own** clock and returns it, for display only.
2. Every row written afterwards gets a server-side `seen_at` at or after that boundary.
3. The final batch's `run_complete.full_resync` deletes this source's rows with `seen_at` before the boundary, which are rows not re-sent because they no longer exist on the phone. It also clears known gaps.
4. An interrupted resync deletes nothing. A final batch with no recorded begin gets a 409, and the phone restarts the resync.

## Operations

- **Wipe** (server data only): `DATABASE_URL=… python scripts/wipe.py --yes-delete-all-health-data`. Then run a Full resync from the phone.
- **Export:** `DATABASE_URL=… python scripts/export.py backup.json`. The file is mode 0600 and contains real health data, so keep it private.
- **Rotating secrets:**
  1. Update the secret in Secret Manager.
  2. Redeploy, or deploy a new revision.
  3. Then, depending on which secret changed:
     - `OAUTH_CLIENT_SECRET`: remove and re-add the connector in Claude.
     - `PHONE_INGEST_SECRET`: re-enter it in the app.
     - `MCP_AUTH_SECRET`: existing tokens stay valid; delete the rows in `oauth_tokens` to force a new sign-in.
- **Logs:** they contain method, path, status, latency and row counts only, never bodies, query strings, tokens, secrets or values. `tests/test_ops.py` enforces this.

Deployment steps are in [../docs/SETUP.md](../docs/SETUP.md).
