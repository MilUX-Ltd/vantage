# Spec 001: QR enrolment, box side

**Slice:** enrolment core (box side). **Review tier:** security-critical; pre-UAT diff read
against this spec before merge; crypto acceptance evidence from a run on the NUC.
**Design reference:** milux-vault-sync ADR-008 (conditions 1 to 5) and ADR-001 here.

## User story

An operator running a Vantage box mints an enrolment QR for a named device and holder. The
device holder scans it; the phone presents the token with its public key; the operator sees
the key fingerprint at the console and confirms; the device is on the roll and may sync. After
the deployment the operator revokes the device and the binding is gone.

## The QR payload

Compact JSON, versioned:

```json
{"v": 1, "t": "vd-enrol", "box": "<box name>", "url": "<sync base url>",
 "tok": "<single-use token>", "pin": "<channel fingerprint, lower hex>", "exp": <unix seconds>}
```

`pin` is the channel fingerprint the app will hold for this box (trust on first use, bounded
by a QR read in person, per ADR-008). `exp` mirrors the token's expiry so the app can refuse a
stale QR before any network call.

## The handshake

1. Console mints a token at the box: single-use, minutes-lived (default 600 s), pre-bound to a
   named device label, holder, deployment scope and clearance ceiling (condition 4).
2. The app presents `{token, public key (SPKI DER, base64)}`. The box consumes the token
   (refusing unknown, expired or already-used) and creates a **pending enrolment** carrying
   the bound fields, the key, and the key fingerprint. Pending devices are NOT on the roll.
3. The operator confirms the pending enrolment at the console after checking the fingerprint
   against the one the phone shows (condition 4's human check). Confirmation writes the
   record into the roll with provenance `qr:<box>:<date>` and bumps the roll generation
   (condition 2's anti-rollback holds). Rejection discards the pending enrolment.
4. Revocation is a tombstone: the row and key are retained, new requests are refused
   (condition 3).

## Acceptance criteria

Each criterion is one test in `boxside/tests/test_enrolment_acceptance.py`, committed failing.

- **AC1** A minted token enrols exactly once; a second presentation is refused as `used`.
- **AC2** A token presented after its expiry is refused as `expired`.
- **AC3** An unknown token is refused as `unknown`.
- **AC4** The QR payload round-trips (box, url, token, pin, expiry); a payload with the wrong
  type or version is refused.
- **AC5** A successful enrolment creates a pending record carrying the pre-bound label,
  holder, deployment scope, clearance ceiling, the presented key and its fingerprint, and the
  device is NOT on the roll while pending.
- **AC6** Confirmation writes the record into the roll (active, provenance `qr:...`), the roll
  generation bumps, and the previous generation is no longer accepted (anti-rollback).
- **AC7** Rejection discards the pending record and leaves the roll untouched.
- **AC8** Revoking an enrolled device tombstones it: `active()` refuses, `record()` retains
  the key.
- **AC9** The token store persists: minted tokens survive a reload from disk, and a consumed
  token stays consumed after reload.
- **AC10** (crypto, runs on a box with `cryptography`) With a real generated P-256 key, the
  recorded fingerprint equals `signing.device_id` of the SPKI DER, a signed roll document
  containing the enrolled record verifies, and a lower-generation roll is refused.

## Out of scope for this slice

The console QR page and confirm surface (next slice), the app's scan-and-enrol flow (app
slice, needs the S23), the signed sync transport (its own spec), the mesh bearer, and any
estate cutover.
