# Health Sync (Android)

A sideloaded personal app that reads Fitbit data from Health Connect and uploads daily aggregates and session summaries to the backend's `POST /v1/ingest`.

- **`sync-core/`:** pure Kotlin, so it is unit-tested on the JVM. It contains:
  - the sync state machine (`SyncEngine`);
  - the `SyncCoordinator` mutex;
  - the wire models (`Payload.kt`, which mirrors `backend/app/schemas.py`);
  - the day windows, batching and retry policy.
- **`app/`:** the Health Connect adapter, DataStore and Keystore storage, the OkHttp transport, the WorkManager worker and the Compose UI.

## Build and test

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home   # any JDK 17
./gradlew test               # 40 sync-core + 6 app tests
./gradlew assembleRelease    # read-only APK for the phone
./gradlew assembleDebug      # emulator build: URL override + fake-data generator (write permissions)
```

`local.properties` (git-ignored) must contain:

```properties
sdk.dir=/path/to/Android/sdk
healthsync.baseUrl=https://<your-service>.run.app   # your backend; baked into the release build
```

The release APK is signed with the debug key, since it's a personal sideload. Keep the same key, or updates will fail to install over the old app. Full setup steps are in [../docs/SETUP.md](../docs/SETUP.md).

**Contract test:**
- `PayloadTest` in sync-core compares a canonical payload with `backend/tests/fixtures/android_ingest_payload.json`.
- The backend's `test_contract.py` checks that the same file validates and ingests.
- If the wire format changes, run `UPDATE_CONTRACT=1 ./gradlew :sync-core:test`, then run the backend tests.

## Install

- **Phone:** `adb install -r app/build/outputs/apk/release/app-release.apk`. This is the release build (`dev.jatin.healthsync`) with **no** write permissions.
- **Emulator:** use the debug build (`dev.jatin.healthsync.debug`). Its "Generate 21 days of fake data" button refuses to run on a physical device.

On first launch, work through the app's sections in order:
1. Enter the phone secret.
2. Grant permissions.
3. Discover sources and confirm them, per data type.
4. Tap **Sync now**. The first sync is a full resync.

## Deviations from the written plan (the real SDK and platform win)

- **Sync now / Full resync run in the foreground app process,** not as WorkManager one-off work. Without the background permission, Health Connect only allows reads while the app is in the foreground. All runs, periodic and manual, go through the same `SyncCoordinator` mutex, so they never overlap. WorkManager unique work is used only for the 6-hourly periodic job, and that job is scheduled only when background reads are enabled.
- **Token expiry** is reported by `ChangesResponse.changesTokenExpired`, a flag, rather than by an exception. The adapter maps it to the engine's expiry path.
- **Changes tokens** are created with the confirmed origins as `dataOriginFilters`, and upserts are filtered by origin again in the adapter.
- **Overlapping sleep stages** are rejected by Health Connect on write, so they can't occur in real data. The fake-data generator therefore omits them; the backend still handles them.
- **Full-history floor:** with "past data" access granted, a full sync reads back to 2020-01-01.
- **Resync reconciliation:** if the final acknowledgement of a full resync is lost, the next run compares `/v1/status`'s `resync_started_at` and `last_full_sync_at` with the boundary it was given. It either marks the resync done, or restarts it if the server no longer knows about it, without forcing a needless second resync.
- **HRV:** the daily value is the mean of the RMSSD readings. Readings taken during a sleep session count toward that session's wake-up date (`HrvAttribution`).
- **Restore guard:** state is reset when an encrypted secret is present but can't be decrypted. Keystore keys are never backed up or transferred, so this identifies a restored copy. A fresh setup without a secret is not reset.

## Privacy

- **Read-only on the phone:** the release build has read permissions only, and backup and device transfer are excluded for every domain.
- **Network:** cleartext traffic is off and redirects are refused, so the bearer secret can only go to the built-in HTTPS host. The secret is encrypted with a Keystore AES-GCM key.
- **Logs** contain only outcomes, counts and error categories.
- **Never read or sent:** exercise titles and notes. Raw heart-rate samples are also never sent.
