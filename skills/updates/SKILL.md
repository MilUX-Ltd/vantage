---
name: updates
description: Work out where a Vantage estate stands against the current release and the third-party baseline, and prepare an update up to the point a person applies it. Use when asked whether the estate is current, what a new release contains, whether anything is behind upstream, or to get a release ready to go. Stages and reports; never applies.
audited: 2026-08-31
audit_verdict: pass
audited_with: skill-safety-audit v3
audit_sha: 626cc8c8a833cd67
audit_sha_source: 7e9a7d8796460e86
origin: the development repository/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: Vantage Community Licence
category: operations
---

# Bringing an estate to a release

This is the clearest case of preparing to the gate, so it is worth being explicit about where
the gate is and why it sits there.

Everything up to and including staging is **reversible**: looking at what is published,
working out where the estate differs, downloading an archive, checking its hash. Undoing all
of it means deleting a file. So you do that work, on a schedule if the operator wants, and you
leave the estate **ready rather than changed**.

**Whether applying is yours depends on your autonomy, and only on that.** At `observe` and
`propose` there is no tool for it: prepare, and hand over. At `act` there is, and you use it.

## Why the gate sits at autonomy rather than at the task

Applying is not dangerous. `vantage-apply-release` verifies the archive, snapshots what is
installed, installs, restarts, waits for `/healthz`, and **puts the old version back** if the
console never answers. The failure case is handled better than most manual updates, and the
work is deterministic: same archive, same steps, same result.

What is not mechanical is *when an estate should change*. An operator may be about to run an
exercise, or have a box somewhere nobody can reach for a fortnight. That is a judgement about
their week, not about the release, and it is the thing the autonomy setting encodes. At
`propose` they have said they want to make that call each time. At `act` they have said they
do not, and asking anyway hands back a decision they already made.

So: at `propose`, make the decision easy and stop. At `act`, apply it, and report what happened
rather than what you intended.

## The sequence

**1. Where does the estate stand?** `release_status` gives you the published release, what this
console runs, which boxes deviate from what the release pins, and what is already staged.

If it reports that it could not reach the publish surface, that is a fact about the estate and
not an error to retry at. Say so, report what you can still see (what the console runs, what is
staged), and stop. A disconnected estate is a supported estate.

**2. What would change?** Read the release notes and the deviation rows together. The rows tell
you which boxes are actually behind, which is a different question from whether a new release
exists. An estate can be several releases old and completely fine.

Report the two separately, because they lead to different decisions:

- **The console** is behind the published release.
- **Boxes** are running components that differ from what the release pins.

**3. Is anything behind upstream?** `baseline_status` with `check_upstream` asks each
third-party project what it has released. Being behind is **drift to report, not a fault**. A
pin is a deliberate choice and moving one needs somebody to test the upgrade. Say what is
behind, by how much, and whether the upstream release looks like a security fix, because that
last part changes the operator's calculus and nothing else you say does.

Never propose moving a pin as though it were routine. It is a change to what the product
installs.

**4. Stage it.** `release_stage` downloads the archive and verifies its checksum against the
one published beside it.

Be accurate about what that proves. The checksum shows the archive arrived intact and
complete. **It is not a signature**: anyone who could replace the archive could replace the
checksum beside it. It defends against a truncated or corrupted download, not against a
compromised publisher. Say that plainly if the operator asks whether the release is verified,
because the honest answer is "verified as intact, not as authentic".

**5. Hand over the decision.** Not a status dump. One paragraph:

> The console is on 0.9.1-beta; 0.9.2-beta is published. It changes X and Y. Two boxes run a
> CloudTAK older than the release pins. The archive is staged and its checksum matches. Applying
> restarts the console and rolls back on its own if it does not come up. Nothing else is
> waiting on this. Shall I put it in front of you to apply?

Then stop.

## Bringing the boxes up, not just the console

A release lands on the console. The boxes are still reporting through whatever they were last
given, which is exactly the drift an operator sees on the board after an update.

**`push-checker`** &rarr; `console/actions/tak-push-checker` updates one box to the current
health checker so it stops reporting stale results. `estate_update` does this for every enrolled
box at once; it is idempotent and decision-free, and it deliberately never redeploys a box's
console, because installing a console rewrites the address it listens on and guessing that for a
box somebody configured on purpose is how a working box goes quiet.

**`update-cloudtak`** &rarr; `console/actions/tak-update-cloudtak` applies the pinned CloudTAK
compose, pulling the pinned images.

**`load-images`** &rarr; `console/actions/tak-load-images` loads a saved image tarball from the
admin box. This is the offline path: an estate with no route out gets its container images
carried in rather than pulled.

**`upgrade-server`** &rarr; `console/actions/tak-upgrade-server` upgrades TAK Server in place from
a package on the operator's shelf. It is `destructive`, it backs up the configuration first, and
it is the one on this page to hand over every time whatever your autonomy: it is the estate's
reason for existing, the package is licensed and operator-supplied, and an upgrade during the
wrong week is not recoverable by rolling a file back.

Report the two kinds of drift separately, because they lead to different decisions: the console
being behind the published release, and boxes running components that differ from what the
release pins.

## On a schedule

This is a good scheduled job, and the value is precisely that it changes nothing. Run steps 1
to 4, put the summary where the operator will see it, and let it sit.

Two rules for scheduled runs:

- **Do not re-stage what is already staged.** `release_status` lists it. Downloading the same
  archive nightly is noise on the shelf and on the network.
- **Say nothing when nothing changed.** A daily message saying "still current" trains the
  operator to ignore the channel, and the one time it says something different they will miss
  it. Report a change, or a new deviation, or a security-relevant upstream release. Otherwise
  stay quiet.

## Offline estates

An estate with no route out cannot check, and cannot stage. It can still be updated: a person
carries the archive on a stick, imports it through the console's file store, and applies it
there.

So on a disconnected estate your job shrinks honestly to the part you can still do: report what
the console and boxes are running, from `release_status` and `server_detail`, and say what the
operator would need to carry. Do not present the inability to reach GitHub as a problem with
the estate.

## What you must not do

- **Never apply below `act`.** There is no tool there; do not look for another route to the
  same outcome, and do not propose a `run_action` that installs a release instead.
- **Never apply during an exercise or a window you were told to avoid**, whatever your
  autonomy. `act` covers routine work, not overriding something you were told.
- **Never move a pin** in the third-party baseline. Report drift; the edit is a person's.
- **Never claim a release is verified** when what you have is a matching checksum. Say intact.
- **Never stage on a box's behalf without saying so.** Staging writes to the console's shelf.
  It is reversible, but it is still a thing that happened, and it belongs in your report.
