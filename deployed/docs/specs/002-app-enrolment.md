# Spec 002: the box's enrolment surface over HTTP, and the app that joins

**Slice:** app enrolment (box HTTP surface + the Vantage Deployed app's join flow).
**Review tier:** security-critical; pre-UAT diff read before merge; crypto acceptance evidence
from the NUC; the app gate is a live proof on the S23. **Design reference:** Spec 001,
ADR-008 conditions 1 to 5, ADR-002.

## User story

The holder of an Android device is handed an enrolment code (a QR, or the payload typed when
there is no screen to scan). They open Vantage Deployed, join the box, read the fingerprint
on their screen to the operator, and when the operator confirms they see the box as enrolled
and can prove the link works. A phone may join more than one box; each binding stands alone.

## Box side: `vd-sync-server`

A stdlib HTTP server (the `vault-ask-server` pattern) over the Spec 001 core, bound to a
stated address and port, state under a stated directory, never inside a vault.

- `POST /enrol` `{tok, key}` (key = SPKI DER, base64). Refusals are honest and specific:
  bad key 400 before the token is judged; unknown, expired or used token 403 with the reason.
  Success 202 `{status: "pending", fingerprint}`.
- `GET /enrol/status/<fingerprint>`: `pending`, or `enrolled` with the deployment scope and
  roll version, or 404.
- `POST /sync/ping`: the first signed request. Canonical string per ADR-008 condition 1
  (counter mode), verified against the confirmed roll entry. Pending device 403, revoked 403,
  bad signature 401, counter at or below the seen watermark 401, wrong channel pin 401.
  Success 200 `{ok: true, label, deployment_scope}` and the counter watermark commits only
  after a good signature.
- Channel pin, interim: a per-box channel id minted at first start and carried in the QR;
  bound into every signature. It becomes the TLS certificate fingerprint in the transport
  slice (ADR-008; tailnet legs are already inside WireGuard; `usesCleartextTraffic` stays
  until then and is on the compliance watch).
- Staleness config per ADR-002: `staleness_bound_s` accepted only from the menu
  {24 h, 48 h, 7 d, 30 d, 90 d}, default 7 d, reported by `GET /sync/config`.

CLI for the operator until the console page lands (kept afterwards for headless boxes):
`vd-ops` with subcommands `mint` (prints the QR payload), `pending`, `confirm`, `reject`,
`revoke` and `config` (the staleness menu), plus `vd-sync-server` to run the surface.

## App side: Vantage Deployed (new app, `uk.co.milux.vantagedeployed`)

- Join a box: paste or scan the payload (manual entry is a first-class path, not a fallback;
  the camera scan is proven when the console page renders a real QR). The app refuses a
  stale payload (`exp` passed) before any network call.
- On join: ensure the Keystore identity (ADR-008: P-256, non-extractable, StrongBox where
  present), present token and key, store the binding (box, url, pin, own fingerprint), show
  the fingerprint large for the operator read-back.
- Poll status; when enrolled show the deployment scope. Test link sends the signed ping with
  the per-binding monotonic counter. Forget deletes the binding locally (the operator's
  revoke is the box-side half).
- Bindings are a list; each carries its own counter and pin. No addresses are hardcoded.

## Acceptance criteria

Box side, in `boxside/tests/test_sync_server_acceptance.py`, committed failing. AC4/AC5 need
`cryptography` and run on the NUC for the gate.

- **AC1** POST /enrol with a minted token and a key returns 202 pending with the fingerprint,
  and the device appears in pending.
- **AC2** Refusals: bad key 400 (token unburnt, a retry with a good key succeeds); unknown,
  expired and used tokens 403 with the honest reason.
- **AC3** Status: pending before confirmation; enrolled with deployment scope and roll
  version after; 404 for a stranger.
- **AC4** Signed ping: a confirmed device's correctly-signed ping returns 200; a replayed
  counter 401; a wrong channel pin 401; a tampered body 401; a pending device 403; a revoked
  device 403; the watermark holds across a server restart.
- **AC5** Config: the staleness menu is enforced (default 7 d, off-menu values refused) and
  reported by /sync/config.

Device gate (the slice's named artefact, not an automated test): on the S23, Vantage
Deployed joins the NUC's server from a minted payload, shows pending with the fingerprint,
shows enrolled after `vd-confirm` on the NUC, and Test link returns green; a second press
also green (counter advanced); after `vd-revoke` the ping is refused. Screenshots and the
server's log are the evidence on an internal card.

## Out of scope

The console pages (mint, pending list, confirm); camera scan; TLS and the pin-set swap; the
vault sync transport (index, pull, push); mesh; multi-box failover UI polish.
