# Spec 005: pack sync (mission packs and map packs to the EUD)

**Slice:** the deployment manifest and pack transport, box side and app side. **Review
tier:** security-critical on the transport (signed, hash-verified); crypto acceptance
evidence from the NUC; the device proof is an agent-driven run on the S23 recorded on the
card (Matt out; his own run can follow). **Design reference:** ADR-003, Spec 004.

## The model

A deployment's manifest lists the packs assigned to it: name, kind (`mission` or `map`),
size, sha256, when it was assigned. Pack files live in the box's state-dir pack store, never
inside a vault. Assignment is the operator's sharing decision (ADR-003): `vd-ops pack add`
now, the console page later writing the same manifest.

The EUD's sync gains a pack phase after the note phases: list the deployment's packs over
the signed surface, pull what is missing or changed (hash-verified), delete nothing. The app
shows a Packs section; a tap offers the pack to ATAK through the platform share sheet.

## The wire

- `GET /sync/packs` (signed): `{packs: [{name, kind, size, sha256}...]}` for the device's
  deployment only.
- `GET /sync/pack?name=<name>` (signed): the raw bytes; refusals are honest (403 out of
  scope or not enrolled, 404 unknown).

## Acceptance criteria

`boxside/tests/test_pack_sync_acceptance.py`, committed failing. Signed operations need
`cryptography`; manifest logic runs anywhere.

- **AC1** Manifest: `pack add` records name, kind, size and sha256 and copies the file into
  the pack store; the manifest survives a reload; `pack remove` removes the entry; adding
  the same name again replaces it (a pack update).
- **AC2** Scope: a device in DEMO lists exactly DEMO's packs; packs assigned to another
  deployment do not appear and their bytes are refused as out of scope.
- **AC3** Transfer: pack bytes round-trip exactly (binary-safe, megabyte-scale), and the
  served sha256 matches the manifest entry so the device can verify what it pulled.
- **AC4** The signed matrix holds on the pack endpoints: a replayed counter is refused, a
  revoked device is refused, and the counter watermark is the same stream the rest of the
  device's requests use.

Device proof (agent-driven, screenshots on the card): after Sync, the packs appear in the
app with kind and size; the pulled files' hashes match the box's manifest; a tap opens the
share sheet for hand-off to ATAK.

## Out of scope

The console page (held for the parallel console build; it writes the manifest seam), pack
deletion propagation to devices, per-device pack assignment (per-deployment matches how kits
are issued, ADR-003), resumable transfer for very large map packs (single-request transfer
now; a range scheme is a later slice if field sizes demand it), and automatic ATAK import
(the share sheet is the honest hand-off until an ATAK plugin slice).
