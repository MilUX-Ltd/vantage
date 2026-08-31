# Vantage Deployed

The Vantage product line's mobile app: a deployment knowledge vault on an Android device,
joined to a Vantage-managed box by scanning a QR code, kept synchronised over a signed
transport, and readable offline. Notes render with tappable wikilinks, the knowledge graph
draws the deployment's entities and orders, capture works in the field, and mission and map
packs arrive alongside the notes ready to hand to ATAK.

A MilUX product. Source-available under the Vantage Community Licence; free for
non-commercial use (see LICENSE at the repository root).

## How it fits together

- **The box** runs `boxside/bin/vd-sync-server.py` beside a Vantage console, serving one
  vault directory. Devices enrol by QR; every request after that is signed by a key that
  never leaves the device's hardware keystore.
- **The console** (Sync page) is the operator surface: mint enrolment QRs, confirm a device
  against the six-digit code its holder reads out, revoke at end of deployment, and see
  exactly what each device receives. Sharing decisions live where the content lives: a
  deployment's folder in the Knowledge Vault, and "Shared to deployments" on the File store
  for mission and map packs.
- **The app** (`android/`) scans, syncs, reads, edits and captures. A device may carry one
  deployment or several; it receives exactly its deployments' folders and packs, filtered
  by the content ceiling it was enrolled with.

## Quick start, box side

```
python3 boxside/bin/vd-sync-server.py --state-dir ~/vd-state \
    --bind <address> --port 8095 --box "<name>" \
    --base-url http://<address>:8095 --vault /path/to/vault
python3 boxside/bin/vd-ops.py --state-dir ~/vd-state config   # prints the admin token
```

Link the box on the console's Sync page (URL plus that admin token), then enrol devices
from the same page. `vd-ops` covers every operator act headless: mint, pending, confirm,
reject, revoke, pack add/list/remove, config. Add `--read-only` to serve pulls only.

## Building the app

Android Studio or a command-line SDK with JDK 17. The shared modules live in `eud-shared/`
beside `android/` in this repository; the Gradle composite build finds them there.

```
cd android && ./gradlew :app:assembleDebug
```

The APK installs on Android 8.0 or later. Landscape-first by design: the device is expected
to ride on the chest.

## The security model, briefly

Per-device ECDSA keys in the Android hardware keystore, never extractable. Enrolment tokens
are single-use and minutes-lived; a human confirms each enrolment against a six-digit code
derived identically on both sides. Every sync request is signed over a canonical string that
binds a per-device monotonic counter and the box's channel pin, so a captured request
replays nowhere. Devices are scoped to their deployments and a content ceiling, enforced
box-side and fail-closed. Revocation is a tombstone: refused thereafter, evidence retained.
The design decisions and their review conditions are in `docs/adr/`; the acceptance criteria
each build met are in `docs/specs/`.
