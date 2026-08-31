---
name: deploy-server
description: Take a bare machine to a working, managed TAK server. Covers provisioning, hardening, the firewall, declaring what the box carries, enrolling it into a console, kiosk mode, and tearing one down. Use when standing up a new server, adding a box to an estate, changing what a box is allowed to do, or working out why a build stopped where it did. Every step names the script that runs it and how that step fails.
audited: 2026-08-31
audit_verdict: pass
audited_with: skill-safety-audit v3
audit_sha: 6618dbea7a02d28a
origin: the development repository/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: Vantage Community Licence
category: operations
---

# From a bare box to a managed server

The whole path, in order, with the failure at each step. You will not run all of it: the operator
holds the keys and the licensed software. What you can do is know every step, verify the evidence
at each one, and tell them exactly where a build actually stopped rather than that it "failed".

---

## Before anything: which stack

Vantage builds two, and it is a choice rather than a default.

**`provision-server`** &rarr; `console/actions/tak-provision`, running
`console/provision/vantage-tak-provision.sh`

The MilUX TAK Server: hardening, PostgreSQL and PostGIS, a Java runtime, TAK Server itself, the
certificate authority, the connectors, and the public certificate where there is one. Nine
stages, and each prints `STAGE-OK` so you can say precisely which one stopped.

**`provision-infratak`** &rarr; the same action script, running
`console/provision/vantage-infratak-provision.sh`

infra-TAK, a third-party platform whose own marketplace deploys the TAK ecosystem above it.
Vantage stands it up, pinned to a tag, and then manages the box underneath it. A box running this
declares `infratak` in its loadout **instead of** `takserver`, because it has no TAK Server and
being judged against one is meaningless.

**TAK Server is never downloaded.** It is licensed to the operator. They supply the `.deb`,
online or on a stick, and no Vantage program will fetch it for them. Do not go looking for a
download; say what they need to provide.

---

## The build, stage by stage

Read `console/provision/vantage-tak-provision.sh` when you need the detail. The stages that go
wrong, and how:

**Harden.** apt up to date, `ufw` scoped to the TAK ports, unattended upgrades on. On a
disconnected box the upgrade step is skipped deliberately rather than waiting on sources it
cannot reach.

**Dependencies.** PostgreSQL 18 with PostGIS 3, and a 17 JRE. TAK 5.8's package depends on
PostgreSQL 18; an older build wanted 15, and a box that has been through both can end up with two
clusters. If a build has been upgraded through versions, check for an orphaned older cluster.

**Install TAK.** `dpkg` the package, let apt settle its dependencies. The guard refuses if TAK is
already there, which is deliberate: a second install over a working server is how a working
server stops working.

**Certificates.** The certificate authority, then the server certificate. The admin certificate
**may not be called `admin.p12`** &mdash; on at least one build it is `webadmin.p12`. Search for
it rather than assuming the name.

**Connectors.** 8089 for CoT, 8443 for the web and the API, **8446 for enrolment**. That last one
matters more than it looks: without it a device cannot enrol by QR, so an estate ends up with a
working server it cannot get anybody onto. On an offline build it is created against the box's
own keystore rather than a public certificate, so it still exists.

**The public certificate.** Skipped on an offline build, and on a connected build with
`--no-letsencrypt`. This is not a failure: proving control of a public name needs public DNS and
an inbound port, and a closed network has neither. The server is complete without it; what is
missing is only browser trust.

**Offline.** `--offline-repo` points the build at a bundle carried in on a stick, built by
`console/vantage-build-offline-bundle` on a box of the **same Ubuntu release**. A bundle made on
one release does not install on another, and the bundle stamps what it was built for so the far
end refuses a mismatch rather than half-installing.

If a build is claimed to work offline, the only honest test is an offline install. A developer
machine with a network cannot find the ways something secretly needs the internet.

---

## Making the box safe

**`harden-plan`** &rarr; `console/actions/tak-harden`

Reads whether the box is at Standard or Hardened posture and what differs. Changes nothing, so
run it freely.

**`harden-apply`** &rarr; the same script. Moves the box to Hardened: SSH hardened with root
staying key-only, kernel settings, the rest. It is classed `destructive` because it changes how
you reach the box.

**`harden-revert`** &rarr; the same script again. Restores the backed-up SSH configuration and
removes the hardening drop-ins. Worth telling the operator this exists before they decide on
`harden-apply`; a change you can undo is an easier decision.

**`firewall-plan`** and **`firewall-apply`** &rarr; `console/actions/tak-firewall`

Plan reads the firewall against the box's declared policy. Apply brings it into line. Always plan
first and show the diff: a firewall change on a box you are reaching over the network is one of
the few things that can lock everybody out, including you.

---

## Telling the box what it is

**`set-loadout`** &rarr; `console/actions/tak-set-loadout`

Declares which modules the box actually carries, written to `/etc/tak-health.d/loadout.conf`.
This is what stops a box being judged against software it was never meant to run.

Without a declaration, a box is checked against its profile, which may expect a great deal it
does not have. With one, undeclared components report as **skipped rather than silent**, so the
board says "not fitted" instead of either failing or quietly checking nothing.

The declaration is validated as a whole: **one unknown component rejects the entire loadout**, and
the box falls back to its profile. If a loadout is not taking effect, check the checker's reported
source, which will say `rejected` and name the token it did not recognise.

---

## Enrolling the box into a console

This is where the keys are, and it is the step you coach rather than perform.

`console/console-enrol-server.sh` installs a forced-command key per action. Each key runs one
program and ignores whatever else a caller asks of it, which is the second of the three
validation layers between an agent and a box.

Where it goes wrong, in the order it usually happens:

- **Interactive sudo.** Most boxes need the operator's password. `ssh -t` is required or sudo
  cannot prompt, and **never pipe an interactive sudo command**: the prompt goes to stdout, the
  pipe swallows it, and it reads as a hang while waiting for input nobody can see.
- **The wrong path.** When the server computes a destination, the server is the authority on it.
  A browser form holding a stale copy of that path produced a build that failed with the package
  "not found" while the package sat, hash-verified, somewhere else.
- **A partial authorisation.** If enrolment stops halfway, some keys are installed and some are
  not, and the console will offer actions the box will refuse. Finish it or undo it; do not leave
  it half-done.
- **Repeated failed logins.** These trip fail2ban and ban the source, turning a small problem into
  an operator locked out of their own box. One attempt, then check reachability at the TCP level
  only.

**`deploy-console`** installs a console on the box so it can manage itself. **`console-mode`**
&rarr; `console/actions/tak-console-mode` promotes or demotes that console between admin, which
manages the estate, and client, which manages only its own box.

**`console-admin`** &rarr; `console/actions/tak-console-admin` mints a console's own keys and
gives it the estate to manage. **`authorize-console`** &rarr;
`console/actions/tak-authorize-console` lets a second console act on a box, or withdraws that.
Both are `destructive` and both change who can control what, so they are decisions rather than
tasks: name exactly which console gains what, and let the operator confirm.

---

## A box with a screen

**`kiosk`** &rarr; `console/actions/tak-kiosk`

Boots a box straight into its own console, so powering it on shows Vantage rather than a login
prompt. Useful for a box on a desk in an operations room.

It can be installed after a build and turned on or off remotely afterwards. It also allows an
exit back to a terminal, because a box you cannot get a shell on is a box you cannot fix.

---

## Taking one down

**`destroy-server`** &rarr; `console/actions/tak-destroy`

Erases the estate footprint: TAK Server, its certificates, its data. Everything.

Hand this over, always, whatever your autonomy. It is the clearest case of destructive work with
no defined recovery, and the cost of being wrong is somebody's certificate authority. Say what
will be erased, ask whether a `backup-coreconfig` should be taken first, and let them run it.

---

## Confirming a build actually worked

A build that finished is not a build that works, and this is where most of the time gets lost.

Do not report a box as built because the provisioner exited zero. Check what it produces:

- the health checker (`common/tak-health.sh`) reports green against the declared loadout;
- 8089, 8443 and 8446 answer;
- a certificate can be issued and a device can enrol with it;
- the device, once enrolled, can actually see what its group should show it.

That last one is the only test that means anything for an estate. A server can accept a
connection, archive an event, climb every counter, and deliver to nobody.
