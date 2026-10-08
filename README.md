# Wearable → Health Connect → Claude

Ask Claude about **your own** sleep, heart rate, HRV, steps and workouts. This uses your wearable's data, on infrastructure you own, at roughly zero cost.

> "How did I sleep over the last 7 days?" · "Show my HRV trend for 30 days" · "Compare my sleep and resting heart rate over two weeks" · "How fresh is my data?"

It was built for a Fitbit Air and works with any wearable app that writes to Android **Health Connect**. It's a personal, single-user setup: you run your own copy.

## How it works

```
Wearable ─► its app ─► Health Connect ─► Health Sync (Android) ──HTTPS──► Backend (Cloud Run + Postgres) ◄──MCP/OAuth── Claude
```

1. **Android app** (`android/`):
   - Reads Health Connect and computes daily aggregates **on the phone**.
   - Uploads them about every 6 hours, plus a **Sync now** button.
   - Crash-safe: it keeps progress across crashes and network loss, and never uploads the same data twice.
2. **Backend** (`backend/`), a small FastAPI service, does three jobs:
   - stores the uploads in PostgreSQL;
   - runs a single-user OAuth server for Claude's sign-in;
   - exposes six **read-only** MCP tools.
3. **Claude** connects with a custom connector. You approve access once with your own owner password.

### MCP tools

| Tool | What Claude gets |
|---|---|
| `get_sleep` | Nights and naps with bed/wake times, time asleep and deep/REM/light/awake minutes |
| `get_sleep_summary` | Averages, min and max over recent nights |
| `get_heart_rate_trend` | Daily average/min/max and resting heart rate |
| `get_hrv_trend` | Daily heart rate variability (RMSSD) |
| `get_activity` | Daily steps, and workouts with distance, calories and heart rate |
| `get_data_status` | How fresh and complete the data is |

## Privacy and security

- **You own every piece.** It runs on your Google Cloud project, your Neon database, your phone and your Claude account. Nothing goes to third parties.
- **Minimal data:**
  - Only sleep, heart rate, resting HR, HRV, steps and workouts with their distance, calories and HR.
  - Raw heart-rate samples, workout titles and notes **never leave the phone**.
- **Locked down:**
  - **The phone:** reads only, never writes to Health Connect. It sends a secret over HTTPS to one fixed host. App data is excluded from backups and device transfer.
  - **Claude:** gets read-only access through OAuth 2.1 with PKCE, a confidential client, single-use codes, rotating refresh tokens and a rate-limited owner login.
- **No values in logs:** the logs contain no health values, tokens or secrets, and a test enforces this.

## Get started

Follow **[docs/SETUP.md](docs/SETUP.md)** to run your own copy. It takes 1–2 hours the first time, and you test everything with fake data before connecting your phone.

| Doc | For |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Step-by-step replication: accounts, deploy, Claude connector, phone app, troubleshooting |
| [backend/README.md](backend/README.md) | Backend internals: API, OAuth design, MCP tools, calculation rules, operations |
| [android/README.md](android/README.md) | App internals: sync state machine, Health Connect specifics, build and test |

## Repository layout

```
backend/   FastAPI + MCP server, 84 tests (real PostgreSQL), fake sample data, admin scripts
android/   sync-core (pure Kotlin sync engine, 40 tests) + app (Health Connect, Compose UI, WorkManager)
docs/      Setup guide
```

## Costs

At one user's volume (a few hundred requests a day, a few MB of data a year) it's expected to stay within the free tiers of Cloud Run and Neon. Cloud Run still needs a billing account. Set a budget alert and keep `--max-instances 1`, as the setup guide shows.

## Limitations

- **One user, one phone,** and one active data source per type.
- **Android only:** iOS and Apple Health aren't supported.
- **Values can differ slightly from your wearable's app.** HRV in particular is the average of the readings written to Health Connect, so it can differ by a few percent from the app's own nightly figure.
- **Older deletions:** deleting old data in Health Connect only reaches the server on a **Full resync** from the app. Recent deletions sync automatically.
- **No cloud API:** Google Health API (cloud) ingestion isn't implemented, because Google wasn't onboarding new projects when this was built. The schema has a `source` column ready for it.

## Disclaimer

A personal data tool, not a medical device, and not affiliated with Google, Fitbit or Anthropic. Don't use it for medical decisions.
