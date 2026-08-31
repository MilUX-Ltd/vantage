# Context and domain language

## What Vantage Deployed is

The Vantage product line's mobile app: the deployment knowledge vault on an Android device,
joined to a Vantage-managed estate by scanning a QR code, synchronised over IP when there is a
path and over a Meshtastic LoRa mesh when there is not, queryable offline by an on-device model.

Naming (Matt, 30 Aug 2026): **Vantage Deployed is the thing that goes on the mobile device.**
The Vantage console is one product installed on any server box and marked as an admin or a
client device; there is no separate "deployed console" product.

## Tier vocabulary

EUD (the handset) / EDGE (a forward box) / FIRMBASE (the rear box). Inherited from
milux-vault-sync ADR-010. Operator-facing strings keep the operator's words ("the box").

## The enrolment model (Matt, 30 Aug 2026)

- A QR is minted **per box** from the Vantage console. Scanning binds the phone to that box.
- A phone may bind to more than one box (rear and kit) so sync survives one box being down.
  Fully deployed means binding to the kit box alone. Expected use is one or two scans.
- After a deployment the synchronisation is removed (app forgets the box, console revokes the
  device); re-enrolment is part of the next deployment's setup.

## Orientation

Landscape is the preferred orientation (Matt, 30 Aug 2026): the EUD rides on the chest in
landscape. Every screen works in both orientations and is designed landscape-first; nothing
locks portrait.

## Lineage

Derived from the proven Deployed builds: `MilUX-Ltd/milux-vault-sync` (the estate instance:
Android app, box-side python, ADRs 001-010) and `MilUX-Ltd/milux-eud-shared` (the shared
Kotlin modules: mesh wire format, estate resolution, canonical signing). Design authorities:
ADR-007 (app owns the IP bearer), ADR-008 (per-device keypair identity, no CA, five binding
review conditions), ADR-009 (one app on the EUD), plus the local ADRs in `docs/adr/`.

## Layout

| Path | Purpose |
|---|---|
| `docs/adr/` | Decision records local to this product. |
| `docs/specs/` | Build specs, one per slice, with acceptance criteria. |
| `boxside/` | The box-side enrolment and sync surface (python, stdlib plus lazy `cryptography`). |
| `android/` | The Vantage Deployed app (arrives with the app slice). |
| `build-log.md` | Chronological build record, written as the work happens. |

## Coordination

Board 51 (Agentic Engineering), card 6188. Progress comments after every unit of work.
