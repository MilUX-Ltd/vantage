---
name: plan-a-build
description: Work out what a Vantage box needs before anything is installed, and hand the operator a plan code the console's Deploy wizard fills itself in from. Asks the same questions as the pre-install planner, in the same order, and discovers what it can from the console rather than asking. Use when someone is about to build a box, has been sent to Deploy without a plan, or is stuck on which address, profile or certificate route applies. Covers the preconditions that fail a build 20 minutes in.
audited: 2026-09-02
audit_verdict: pass
audited_with: skill-safety-audit v3 (author self-audit, not independent)
audit_sha: 57a0fca810fc91da
audit_sha_source: d9ca3f7b9f369ba2
origin: the development repository/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: Vantage Community Licence
category: operations
---

# Plan a build before you start one

A Vantage build fails on preconditions, not on steps. Every failure recorded against the
Deploy wizard so far surfaced five to twenty-five minutes in, on something knowable before
the first byte was written: the console could not reach the box, the admin user could not
elevate, the address was the kit LAN one when a VPN one was needed. This skill asks the
questions in the order that stops those, and ends with a code the wizard reads.

It is the same set of questions as `PRE-INSTALL-PLANNER.html`, which ships in every release
and is served by the console at `/planner`. Use the page when the operator has a browser;
use this when they are in a terminal, on a call, or already talking to you.

## Discover before you ask

Ask nothing you can find out. On a console box:

| Question | Where the answer already is |
|---|---|
| What is already enrolled | `/etc/vantage-console/actions.json`, `targets` |
| What this console runs | `VANTAGE_RELEASE` in `/usr/local/lib/vantage-console/vantage-console-serve.py` |
| Whether a box is reachable | `ssh -o BatchMode=yes -o ConnectTimeout=5 <user>@<addr> true` |
| Whether the admin user can elevate | `ssh <user>@<addr> 'sudo -n true'` |
| Which addresses a box actually has | `ip -4 -o addr` on the box; `tailscale ip -4` for the VPN one |

State what you found and ask the operator to confirm it. Do not present a discovered value
as a decision they made.

## The questions, in the order that matters

**1. Is this the first console, or a box joining an estate?** This decides everything after
it. If a console already runs, the box is built *from* that console's Deploy wizard and
nothing is hand-installed on it. Getting this wrong means walking someone through a
hand-install the README forbids.

**2. What is the address the console can reach?** Not "the box's address", but the one the
*console* can open a connection to. A kit LAN address behind its own router is reachable
from nowhere else. If the two are on a tailnet, this is the tailnet address, and it is the
single most common wrong value in the form.

**3. Can the admin user elevate without a password?** Enrolment needs root or passwordless
sudo. An ordinary sudo account that prompts is refused, and the refusal arrives at phase 1
of 5 after the whole form is filled in.

**4. What does the box carry?** The health profile is judged on what the box *runs*, not
where it lives. `firmbase` expects TAK Server and PostgreSQL. `cloud` expects the full
twelve services and six tilesets, so choosing it for a box that carries only TAK reports a
dozen absent things as unhealthy.

**5. What certificate?** Three routes, and one of them is "none". A box on a private network
that publishes nothing needs no public DNS record and no Let's Encrypt: pass
`--no-letsencrypt`. Say plainly that this means a browser warning on first connect, and that
it is a real answer rather than a compromise.

**6. Kiosk?** If yes, the console install on the box is not optional: the kiosk points at it.
The kiosk itself is set later by the `tak-kiosk` action, from the box's own page.

## Emit the plan code

Spec 007. One line, pasted into step 1 of Deploy:

```
VANTAGE-PLAN-1.<base64url of compact JSON, no padding>
```

Keys: `v` (always `1`), `name`, `label`, `addr`, `user`, `profile`, `fqdn`, `comp` (array),
`cert` (`none` | `laptop` | `token`), `kiosk` (boolean).

```bash
python3 -c '
import base64, json, sys
plan = json.loads(sys.argv[1])
raw = json.dumps(plan, separators=(",", ":")).encode()
print("VANTAGE-PLAN-1." + base64.urlsafe_b64encode(raw).decode().rstrip("="))
' "$(cat <<JSON
{"v":1,"name":"field-laptop","label":"Field Laptop","addr":"100.64.0.10",
 "user":"admin","profile":"firmbase","fqdn":"field-laptop.example.com",
 "comp":["takserver"],"cert":"none","kiosk":true}
JSON
)"
```

**No secrets, ever.** No password, no key, no token, no certificate material. The code is
meant to survive a chat window, a phone call or a sheet of paper, and that is only safe
while there is nothing in it worth stealing. If you are tempted to add a credential, you
have found the boundary, not a gap.

Always show the operator what the code says in plain English before they paste it. The code
saves retyping; it never substitutes for them looking.

## Before they press Deploy

Read these back as a checklist, because the wizard cannot test any of them for you:

- the console can open a connection to that exact address
- the admin user is root, or has passwordless sudo
- the TAK release is on the shelf already
- on a private build with `cert: none`, the run **stops at stage 7 of 9** on the certificate
  stage. Everything before it is done. Say so in advance or it reads as a failure.

## Where this stops

You plan; the operator builds. Do not run Deploy, do not enrol a box, and do not touch a
console's `actions.json` on your own initiative. If a build has already failed, the useful
work is reading the phase it stopped at and the line above the error, not starting another.
