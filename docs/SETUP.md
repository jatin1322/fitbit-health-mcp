# Set up your own copy

This guide walks you through running your own instance: your wearable's data, your server, your Claude. It takes about 1–2 hours the first time, mostly waiting for installs and the first deploy.

You will end up with:
- **A small backend** on Google Cloud Run with a free Neon Postgres database. It is expected to cost nothing at this workload.
- **A Claude custom connector**, so Claude can answer questions like "how did I sleep this week?" from your own data.
- **An Android app** on your phone that syncs Health Connect to your backend about every 6 hours.

> **Not medical advice.** This is a personal data tool. Values are aggregates of what your wearable writes to Health Connect and can differ slightly from your wearable's own app.

---

## 0. Before you start

### What you need

| | Notes |
|---|---|
| **Android phone, Android 14 or newer** | Health Connect is built into the system from Android 14. Older phones need the Health Connect app, and background sync may not be available. |
| **A wearable whose app writes to Health Connect** | Built and tested with Fitbit Air and the Fitbit / Google Health app. Other sources such as Samsung Health or Garmin (via a bridge app) work if they write the same data types; you pick the source per data type in the app. |
| **A Claude plan with custom connectors** | The Free plan allows one custom connector; Pro and Max allow more. |
| **Google Cloud account with billing enabled** | Cloud Run requires a billing account even within the free tier. New accounts usually get trial credits. |
| **Neon account** | For the free Postgres database. |
| **A computer (macOS or Linux)** | Used for building and deploying. |

Tools to install on the computer:
- `git` and `openssl`.
- [`uv`](https://docs.astral.sh/uv/) for Python. It installs Python 3.12 for you.
- The [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) (`gcloud`). Sign in with `gcloud auth login`.
- [Android Studio](https://developer.android.com/studio). Run its Setup Wizard with the **Standard** option, which installs the SDK and `adb`.
- JDK 17, for example `brew install openjdk@17`.

### Check that your data reaches Health Connect (2 minutes)

On the phone, open **Settings → search "Health Connect" → Data and access** and open each category:
- **Sleep:** open a night and check that it shows **stages** (light, deep, REM).
- **Vitals:** heart rate, resting heart rate, heart rate variability.
- **Activity:** steps, exercise, distance, calories.

Also open **App permissions** and confirm your wearable's app is allowed to **write** these categories. Anything missing here can't be synced.

### Choices to make now

Decide these once; later steps use them.

- **`REGION`:** a Google Cloud region near you, such as `europe-west1`, `us-central1` or `asia-south1`. Pick the Neon region closest to it.
- **`USER_TZ`:** your home timezone as an IANA name, such as `Europe/London` or `Asia/Kolkata`. Claude uses it for "today" and "last 7 days".
- **`SERVICE`:** the Cloud Run service name. These docs use `fitbit-mcp`.

---

## 1. Get the code

```bash
git clone <this repository> health-mcp && cd health-mcp
```

Optionally, run the backend tests (about 10 seconds; they start an embedded Postgres automatically):

```bash
cd backend
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
cd ..
```

## 2. Create the database (Neon)

1. Sign up at neon.com and create a project in the region nearest your `REGION`. Postgres 16 or newer is fine.
2. Click **Connect**, turn **off** "Connection pooling" (the host must **not** contain `-pooler`), and copy the connection string. It looks like `postgresql://user:password@ep-xxx.region.aws.neon.tech/neondb?sslmode=require`.

Don't save it to a file or paste it anywhere; you'll put it straight into Secret Manager in step 4. The app creates its tables on first start.

## 3. Create the Google Cloud project

```bash
SERVICE=fitbit-mcp; REGION=<REGION>             # the choices from step 0; keep this terminal open
gcloud projects create <PROJECT_ID>            # or create it in the console
gcloud billing projects link <PROJECT_ID> --billing-account <BILLING_ACCOUNT_ID>
gcloud config set project <PROJECT_ID>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
PROJECT_NUMBER=$(gcloud projects describe <PROJECT_ID> --format='value(projectNumber)')
echo "Your service URL will be: https://$SERVICE-$PROJECT_NUMBER.$REGION.run.app"
```

Two things to set up now:
- **Budget alert:** add one in the console (**Billing → Budgets & alerts**), for example $1 or ₹50 a month, with **"exclude credits"** so trial credits don't hide spending. A budget only notifies; it doesn't stop spending. The real limits are `--max-instances 1` and Neon's free plan, which suspends rather than bills.
- **Service URL:** note the URL printed above. That exact URL becomes `PUBLIC_BASE_URL`. Cloud Run also serves a second legacy hostname, but only this one will work, because the app checks the host exactly.

## 4. Create the secrets

Three random secrets are generated straight into Secret Manager, so they never touch disk or your screen:

```bash
for s in PHONE_INGEST_SECRET MCP_AUTH_SECRET OAUTH_CLIENT_SECRET; do
  openssl rand -hex 24 | tr -d '\n' | gcloud secrets create $s --replication-policy=automatic --data-file=-
done
```

Then add the database URL. Run this in your own terminal, with your Neon string in the quotes:

```bash
printf '%s' 'postgresql://…?sslmode=require' | gcloud secrets create DATABASE_URL --data-file=-
```

Let Cloud Run read them:

```bash
SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
for s in DATABASE_URL PHONE_INGEST_SECRET MCP_AUTH_SECRET OAUTH_CLIENT_SECRET; do
  gcloud secrets add-iam-policy-binding $s --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done
```

| Secret | Used for |
|---|---|
| `PHONE_INGEST_SECRET` | The phone app's uploads. Entered once in the app. |
| `MCP_AUTH_SECRET` | Your owner password on the approval page when Claude connects. **Save it in your password manager.** |
| `OAUTH_CLIENT_SECRET` | Entered once in Claude's connector dialog. |
| `DATABASE_URL` | The backend's database connection. |

To read a secret later: `gcloud secrets versions access latest --secret=NAME`.

## 5. Deploy the backend

From the `backend/` folder:

```bash
gcloud run deploy $SERVICE --source . --region $REGION \
  --min-instances 0 --max-instances 1 --concurrency 10 --timeout 60 --memory 512Mi \
  --allow-unauthenticated \
  --set-secrets DATABASE_URL=DATABASE_URL:latest,PHONE_INGEST_SECRET=PHONE_INGEST_SECRET:latest,MCP_AUTH_SECRET=MCP_AUTH_SECRET:latest,OAUTH_CLIENT_SECRET=OAUTH_CLIENT_SECRET:latest \
  --set-env-vars OAUTH_CLIENT_ID=claude-connector,USER_TZ=<USER_TZ>,TRUST_PROXY=1,PUBLIC_BASE_URL=https://$SERVICE-$PROJECT_NUMBER.$REGION.run.app
```

The first deploy builds the container on Cloud Build and takes a few minutes. `--allow-unauthenticated` only means Google doesn't put its own IAM login in front of the service; the app authenticates every data endpoint itself.

**Check it** (set `URL=https://$SERVICE-$PROJECT_NUMBER.$REGION.run.app`):

```bash
curl -s $URL/health                                            # {"ok":true}
curl -s -o /dev/null -w "%{http_code}\n" -X POST $URL/mcp      # 401  (don't add -L)
curl -s $URL/.well-known/oauth-protected-resource/mcp          # "resource" must equal $URL/mcp
```

**Load fake data,** so you can test Claude before connecting your phone:

```bash
cd backend
PHONE_INGEST_SECRET="$(gcloud secrets versions access latest --secret=PHONE_INGEST_SECRET)" \
  .venv/bin/python scripts/load_sample.py $URL
```

## 6. Connect Claude

1. Get the two values you'll need, in your own terminal:
   ```bash
   gcloud secrets versions access latest --secret=OAUTH_CLIENT_SECRET; echo
   gcloud secrets versions access latest --secret=MCP_AUTH_SECRET; echo
   ```
2. In Claude, go to **Customize → Connectors → Add custom connector**:
   - **URL:** `https://$SERVICE-$PROJECT_NUMBER.$REGION.run.app/mcp`. Use exactly this hostname.
   - **Authentication:** **Sign in now**.
   - **OAuth client:** **Use your own OAuth client**, with client ID `claude-connector` and the `OAUTH_CLIENT_SECRET`.
3. An approval page opens. Check it says you'll be sent to **claude.ai**, then enter `MCP_AUTH_SECRET`.
4. In a chat with the connector enabled, try "How did I sleep over the last 7 days?" You should get answers from the fake data, which includes a nap and an unstaged night.

## 7. Build the Android app

1. Point the app at **your** backend. In `android/local.properties` (git-ignored; Android Studio may already have created it):
   ```properties
   sdk.dir=/Users/<you>/Library/Android/sdk
   healthsync.baseUrl=https://<SERVICE>-<PROJECT_NUMBER>.<REGION>.run.app
   ```
   The release build bakes this URL in, and the phone secret is only ever sent there.
2. Build and test:
   ```bash
   cd android
   export JAVA_HOME=<path to JDK 17>      # e.g. /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
   ./gradlew test assembleRelease          # → app/build/outputs/apk/release/app-release.apk
   ```

**Optional: emulator rehearsal.**
1. Create an emulator in Android Studio (Device Manager, any Android 14+ Google image). Start it, then run `./gradlew assembleDebug` and `adb install -r app/build/outputs/apk/debug/app-debug.apk`.
2. In the app, tap **Generate 21 days of fake data**. It only works on an emulator.
3. Then do step 9 below on the emulator.
4. **Uninstall the debug app afterwards** (`adb uninstall dev.jatin.healthsync.debug`). Otherwise its background job keeps sending fake data whenever the emulator runs.

## 8. Clear the fake data

Do this right before your first real sync. It deletes server data only:

```bash
cd backend
DATABASE_URL="$(gcloud secrets versions access latest --secret=DATABASE_URL)" \
  .venv/bin/python scripts/wipe.py --yes-delete-all-health-data
```

## 9. Install on your phone and sync

1. **Enable USB debugging** on the phone:
   - Settings → About phone → tap **Build number** 7 times. On Samsung it's under Software information.
   - Then Settings → Developer options → **USB debugging**.
   - Connect with a data-capable USB cable and tap **Allow**.
2. **Install:**
   ```bash
   adb install -r android/app/build/outputs/apk/release/app-release.apk
   ```
3. **Enter the phone secret.** In the app, tap the **Ingest secret** field, then from your computer run:
   ```bash
   adb shell input text "$(gcloud secrets versions access latest --secret=PHONE_INGEST_SECRET)"
   ```
   Tap **Save**, then **Test connection**. It should say "Connection OK".
4. **Grant permissions:** tap **Grant permissions** and allow every data type, plus **Access data in the background** and **Access past data**.
5. **Battery:** set Settings → Apps → **Health Sync** → Battery → **Unrestricted**. On Samsung, Realme, Xiaomi and similar phones, aggressive battery saving otherwise stops background sync.
6. **Pick your data sources:** tap **Discover sources** and review the list.
   - **Steps:** pick **only** your wearable's app. Many phones also write their own step count, under a package like `com.android.healthconnect.phone.*`. Selecting both double-counts.
   - **Every other type:** pick your wearable's app.
   - Tap **Confirm sources**. Changing them later triggers a full resync automatically.
7. **First sync:** tap **Sync now**. It reads your full history, which may take a minute.
8. **Verify** by asking Claude: *"Show my sleep last night with stages, yesterday's steps, last night's HRV, yesterday's resting heart rate and my most recent workout."* Compare with your wearable's app:
   - **Sleep, steps and resting heart rate** should match closely.
   - **HRV** is the average of the readings your wearable writes, and can differ by a few percent from the app's own nightly figure.

From now on the phone syncs about every 6 hours in the background. Ask Claude "how fresh is my data?" at any time.

---

## Maintenance

- **Update the backend:** pull changes, then rerun the deploy command from `backend/`.
- **Update the app:** rebuild, then `adb install -r …/app-release.apk`. Settings and the secret are kept, as long as you build with the same signing key (the debug key in `~/.android`).
- **Rotate a secret:** add a new version in Secret Manager (`gcloud secrets versions add NAME --data-file=-`) and redeploy. Then:
  - `OAUTH_CLIENT_SECRET`: re-add the connector in Claude.
  - `PHONE_INGEST_SECRET`: re-enter it in the app.
  - `MCP_AUTH_SECRET`: existing sign-ins stay valid until their tokens are revoked.
- **Back up your data:** `DATABASE_URL=… python scripts/export.py backup.json` writes a private (mode 0600) JSON file.
- **Delete everything:**
  1. Remove the connector in Claude.
  2. Uninstall the app.
  3. Delete the Neon project and the Google Cloud project.
  4. In Health Connect, revoke Health Sync's access (it never wrote any data).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/health` (or `/healthz`) returns a Google 404 page | Use `/health`. Cloud Run reserves some paths ending in `z`. |
| Claude: "Couldn't reach the MCP server" | Use the exact `PUBLIC_BASE_URL` hostname plus `/mcp`. `curl -X POST …/mcp` must return 401 without redirects. |
| Sign-in fails after the approval page | Check the client ID is `claude-connector` and the client secret is current. Remove and re-add the connector after rotating. |
| "Too many attempts" on the approval page | Wait 15 minutes. The lockout applies only to the network that made the failed attempts. |
| Database connection errors | Use Neon's **direct** connection string (no `-pooler`), with `sslmode=require`. |
| App: "Secret rejected (401)" | Re-enter `PHONE_INGEST_SECRET` (step 9.3). |
| App: "Background sync unavailable" | Grant **Access data in the background** in Health Connect. Until then, open the app and tap Sync now. |
| Data stops updating | Set battery to **Unrestricted**, then ask Claude "how fresh is my data?". |
| Steps look doubled or too low | Re-run **Discover sources** and select only your wearable for steps. |
| A deleted workout or nap still appears for an old date | Run **Full resync** in the app. Older deletions are applied on full resync. |
| `pgserver` won't start locally | Its data directory can't contain spaces. `scripts/dev_db.py` uses `~/.cache/fitbit-mcp-devdb`. |

## Security notes

- **Your service URL is public, but every data path is authenticated.**
  - The phone uses its secret.
  - Claude uses OAuth with a confidential client, PKCE, single-use codes and rotating refresh tokens, plus your owner password.
- **Anyone with your `MCP_AUTH_SECRET` and `OAUTH_CLIENT_SECRET` could read your health data.** Keep them in a password manager.
- **Nothing secret belongs in the repository.** `local.properties`, `.env` files and exports are git-ignored, and the backend refuses to start with weak or reused secrets.
- **The server keeps only daily aggregates and session summaries.** Raw heart-rate samples, workout titles and notes never leave the phone, and logs never contain health values.
