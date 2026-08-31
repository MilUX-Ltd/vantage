# Spec 004: the sync transport (index, pull, push over the enrolled identity)

**Slice:** box side first, then the app client. **Review tier:** security-critical; crypto
acceptance evidence from the NUC; the device gate is a live run on the S23. **Design
reference:** ADR-001, ADR-002, Specs 001-003; milux-vault-sync ADR-007 and its slice 5.

## What synchronises, and the shape of it

**The unit of sync is the deployment, not the vault.** A device's roll entry names its
deployment scope and its content ceiling; `index` lists exactly the markdown inside that
deployment folder, at or below the ceiling, each with its sha256. The device never learns
what else the box holds. Attachments and binaries come later; notes are the product today.

**State-based, hash-driven, both directions.** The device holds its own copy of its
deployment (app-private storage, per the estate's ADR-007/009) plus a per-file
"last-received" hash map. Sync is: fetch the signed index, pull what is new or changed, push
local edits. Nothing streams; every exchange is one signed request, which is what lets the
same model ride the mesh bearer later (one file at a time was the mesh's shape all along).

**The no-second-copy rule is enforced on both sides.** Matt's standing rule from the estate
build: the vault holds exactly one version of any note, and conflicts are resolved OUTSIDE
the vault, only the resolution ever enters it. The proven `vaultapply` decision table
(created / identical / updated / conflict, quarantine outside the vault) is the shared
statement of the rule. On push the device states the hash its edit was based on; the box
fast-forwards when its copy matches, and otherwise stages the incoming version in its
quarantine and answers 409 so the device knows. Every box-side overwrite first copies the
previous version into a history directory outside the vault, so no push destroys evidence.

## Brought across from the estate build, verbatim (ADR-001 vendoring, at 42a89de)

- `syncserver.py` `SyncCore`: authorise (roll, channel, counter, signature, commit-after-
  good-signature), deployment scoping, path-traversal guard, ceiling filter, and the three
  operations. Its 10 security unit tests come with it.
- `deployment.py` (+ `graph.py` it reads with): a deployment IS its label, a folder is its
  home. 4 unit tests.
- `classification.py`: the content-suitability guard, fail-closed on an unreadable marking.
  9 unit tests.
- `vaultapply.py`: the conflict decision table. 5 unit tests.
- Already vendored in earlier slices: `roll.py`, `signing.py` (their 16 unit tests join now).

**New in the product:** the HTTP wiring of SyncCore onto the enrolment surface (slice 5 left
SyncCore unwired by design); the conflict-safe push path (the estate's SyncCore.push
overwrote, because its conflict handling lived on the mesh path; the product routes push
through the vaultapply decision); the persisted counter watermark shared with ping; and the
whole app-side client, which the estate never built (its IP transport was the un-built
remainder; vault content moved by mesh).

**Deliberately not this slice:** the mesh bearer (arrives capability-gated with its proven
stack), attachments, the on-device model and graph (later ports from `:brain`), the console
pages.

## The wire

All served by `vd-sync-server`, all signed with the Spec 002 headers; `path_with_query` is
inside the signature, so the file path and claimed base hash are tamper-evident.

- `GET /sync/index` -> `{files: [{path, sha256}...]}` scoped and ceiling-filtered.
- `GET /sync/file?path=<rel>` -> raw bytes; refusals are 403/404 with honest reasons.
- `POST /sync/push?path=<rel>&base=<sha256|new>` -> 200 applied (`created`/`updated`),
  409 conflict (staged outside the vault, local version stands), 403 refusals (scope,
  ceiling, not enrolled).

## Acceptance criteria

Box side, `boxside/tests/test_sync_transport_acceptance.py`, committed failing. All need
`cryptography` (every operation is signed), so the suite's evidence run is on the NUC; the
vendored unit tests cover the pre-crypto logic everywhere.

- **AC1** Index is scoped and ceiling-filtered: a note in another deployment and a note
  marked above the device's ceiling do not appear; entries carry correct sha256s.
- **AC2** Pull: an in-scope file is returned byte-exact; out-of-scope, traversal and
  above-ceiling requests are refused; an unknown path is refused honestly.
- **AC3** Push, fast-forward: a new file is created; an edit based on the current box hash
  is applied; the previous version is retained in the history directory outside the vault.
- **AC4** Push, conflict: an edit based on a stale hash gets 409; the vault still holds
  exactly one version (the box's); the incoming version sits in the quarantine outside the
  vault.
- **AC5** A push whose content is marked above the device's ceiling is refused.
- **AC6** The signed-request matrix holds on the transport: replayed counter 401, tampered
  path or base 401, revoked device 403, and the watermark is shared with ping (a ping and an
  index cannot reuse one counter).

Device gate (with Matt, after the app client lands): notes appear on the S23 after Sync; a
landscape edit on the phone lands in the box's demo vault; a deliberate two-sided edit ends
with one version in each vault and the other staged, never two copies side by side.

## Out of scope

Everything named "deliberately not this slice" above, plus deletion propagation (a removed
note is a later, careful design: tombstones versus absence) and multi-box reconciliation
(each binding syncs independently; the boxes reconcile over their own link).

## Committed follow-up: mission packs and map packs (Matt, 30 Aug 2026)

Pack sync joins the design as its own spec: the Store shelf's mission packs and map packs
are the information a device actually needs in the field, so the transport must carry
binaries, not only notes. The design questions it owns: binary transfer over the same signed
surface (hash-verified, resumable for large map packs), suitability for a binary (no text
marking to read, so the decision is the operator's curation per deployment, which is card
6170's content-management thread), and the hand-off into ATAK on the device (its own data
package import). The deployment-scoped index model extends naturally: packs live under the
deployment like notes do, listed with size and sha256.
