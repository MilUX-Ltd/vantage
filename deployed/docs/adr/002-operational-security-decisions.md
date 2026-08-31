# ADR-002: Operational security decisions for enrolment (Matt, 30 Aug 2026)

**Status:** Accepted. **Owner:** Matt. Closes the three decisions ADR-008's convened security
review reserved for the owner, and states the compliance posture.

## 1. The staleness bound is operator-configurable (condition 3)

The roll staleness bound is set by the operator, from a fixed menu: **24 hours, 48 hours,
7 days, 30 days, 90 days**. Default **7 days**. Configured at the box (surfaced in the console
later; the server refuses values outside the menu). When a box's roll replica is older than
the bound, the box degrades to **read-only**: it serves nothing new for write and refuses
push, rather than trusting a roll it cannot confirm is current. Revocation convergence is
therefore bounded by the operator's own number.

## 2. A box certificate change means a re-scan; the brick is accepted (condition 4)

No backup pin, no in-band re-pin. If a box's channel identity changes, enrolled phones go
deaf to it until re-enrolled by QR in person. This is stated plainly and accepted: the
product's own lifecycle removes sync after every deployment and re-enrols at the next, so the
re-scan path is the normal path, not an emergency. Revisit only if a long-running standing
deployment emerges.

## 3. The stolen-unlocked-handset residual is accepted for the demo posture (condition 5)

The phone's own lock (biometrics or PIN) is the control, the same exposure class as ATAK on
the same handset: an unlocked phone yields the tactical picture regardless of what this app
does. Keys are hardware-backed and non-extractable (StrongBox where present), which defends
against impersonation and key cloning; user-authentication-bound keys with a short validity
window are the named hardening step if the product moves past proof-of-concept and demo use.

## 4. The read-back is a 6-digit pairing code, never the raw fingerprint (Matt, 30 Aug 2026)

The first live ceremony asked the holder to read a 64-hex fingerprint aloud and Matt
rejected it flat: not a human ceremony. The read-back is a **six-digit code** derived from
the device fingerprint identically on both sides (pinned cross-language vector, the same
discipline as the canonical signing string); the pattern is Bluetooth numeric comparison and
Signal safety numbers. The full fingerprint stays on the roll for the record. The code's job
is only to distinguish which device consumed the single-use, minutes-lived token; a raced
token announces itself because the real holder's own enrolment is then refused as used,
which tells the operator to reject the pending impostor. The residual (an insider who
already knows a target device's fingerprint grinding a matching key) is accepted at the demo
posture and noted on the compliance watch.

## Compliance posture (replaces the separate Compliance card)

This build is part of the development environment, running as a proof of concept and demo; it
is not a production credential system, so no standalone Compliance card is raised. The
obligation kept is **tracking**: `docs/compliance-watch.md` lists everything in this design
that would need compliance attention if it moves beyond demo use, and the watch list is
reviewed whenever the product's status changes or at the Cyber Essentials Plus renewal, whichever comes first.
