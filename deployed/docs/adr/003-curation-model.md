# ADR-003: One line of authority for what reaches a device

**Status:** Accepted (built to Matt's tasking of 30 Aug 2026: "where is this determined?").
**Owner:** Matt.

## Context

Three content kinds need to reach an EUD: the knowledge vault, mission packs, and map packs.
Before this decision the pieces were disjointed: the console's Store holds packs but nothing
says which device should have them; the sync surface serves notes by deployment folder
convention; packs reached devices only by the bootstrap page. Nobody could answer "what does
this EUD get, and who decided?" from one place.

## Decision: console decides, box records and enforces, EUD receives

- **The deployment is the unit of curation for every content kind.** Notes belong to a
  deployment by living in its folder (established); packs belong to a deployment by being
  assigned to it in the box's **deployment manifest**.
- **The operator determines sharing at the console.** Assigning a pack from the Store shelf
  to a deployment is the sharing decision, one deliberate act, recorded in the manifest.
  Until the console page lands (held for the parallel console build), `vd-ops pack` is the
  same act from the command line; the manifest file is the seam the console page will write.
- **The box records and enforces.** The manifest (`manifest.json` + a pack store in the
  box's state dir) says what each deployment carries. The signed sync surface serves exactly
  manifest ∩ device scope ∩ ceiling: a device sees its deployment's packs and nothing else.
  For notes the ceiling is read from the text; for binaries there is no text to read, so
  **operator curation IS the suitability decision** for a pack, stated plainly.
- **The EUD receives.** The same signed, hash-verified, one-request-per-file transport
  carries packs into the app's private pack store; the app lists them and hands them to ATAK
  through the platform share sheet (ATAK imports its own data packages).
- **Between servers**, the admin console's Store remains the library; packs reach a box by
  the console's existing gated push actions. A box serves its devices only from its own
  manifest, so a deployed kit stays self-sufficient offline.

## Why here and not the console

The decision could have lived console-side (a per-device list at the admin). It lives on the
box because the box is what a deployed EUD can actually reach: the kit must answer "what do
my devices get" with no reachback, and revocation, scope and ceiling already live in the
box's roll. The console is the curation surface, never the enforcement point.

## Consequences

Card 6170 (curate what reaches which device) gets its enforcement half from this product;
the console half becomes a page that writes the manifest and pushes the pack files. Pack
deletion propagation and per-device (rather than per-deployment) pack assignment are
deliberate later decisions; per-deployment matches how kits are actually issued.
