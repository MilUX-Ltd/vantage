---
name: vantage-lessons
description: Diagnose a TAK estate the way someone who has been burned by it does. Use when a box, server, mesh radio, map, certificate or data package "should be working" but is not, when a check reports a fault before it has been proved on a known-good case, or when about to trust a signal that only proves something started. Covers what to distrust and why, with a symptom-to-cause table for the faults that have actually happened.
audited: 2026-08-31
audit_verdict: pass
audited_with: skill-safety-audit v3
audit_sha: abd68e58f6552268
audit_sha_source: 3d37283e82cb9aa3
origin: the development repository/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: Vantage Community Licence
category: engineering
---

# What this estate has already taught us

Every lesson below was paid for once, in hours, on a real box. You are reading them so
nobody pays twice.

This skill is not about what you are able to do. The console's action registry covers that,
and `list_actions` will tell you. This is about **doing the job well**, which mostly means
knowing which signals are worth believing.

## The one habit that matters most

**Almost every expensive mistake in this estate's history was believing a signal that proved
something weaker than it appeared to.**

- A service was `active`. That proves it started, not that it works. `mbtileserver` was
  active and serving an empty directory. Fifteen people would have had a grey screen.
- A server's input counters were climbing. That proves ingestion, not distribution. The
  events belonged to no group any client certificate was in, so no client ever saw one, and
  the fault sat there silently for a week.
- A CoT send returned success. That proves the socket accepted the bytes. Port 8089 drops
  clients that disconnect immediately, so the server never ingested it.
- A data package imported with no error. That proves it parsed, not that it drew anything.

So before you report that something works, or that something is broken, ask the same
question: **what would this signal look like if I were wrong?** If the answer is "exactly
the same", it is not evidence, and you need a different check.

The general form: **check the output, not the process.** For a tile server, count tilesets.
For a media server, push a stream and read it back. For TAK, complete a TLS handshake. For
an input, watch a marker arrive on a client that authenticated the normal way.

## Run a control before you report a fault

Four "faults" in one day turned out to be the checking code, not the system. Every port
read as firewalled, because `/dev/tcp` is a bash feature and the shell was zsh. One tileset
of five was reported, because `grep -oc` counts matching lines and the JSON is one line.

Before you tell an operator something is broken, **prove the check reports success on a
known-good case.** Port 22 obviously works: if your test says it does not, your test is
broken. This costs one extra command and it has saved this estate several wasted hours and,
twice, sent the operator to run something they did not need to run.

The same holds in reverse. A checker only ever observed printing PASS is not evidence of
anything. If you build or change a check, break something deliberately and confirm it says
FAIL.

## Read the primary source before theorising

Twice in two days, "go and read a working example" beat a morning of plausible reasoning.

- A WinTAK package that would not draw was solved by finding a package from 2024 that
  worked and diffing it. Hours had gone into theories about channels, groups, truststores
  and enrolment. All wrong, all reasonable-sounding.
- A mesh gateway's README said it forwarded to TAK clients by multicast. What the code
  actually did was unicast to one configured client on port 4242; the multicast constant was
  dead. Two hours went into symptoms. Grepping the source for `sendto` would have cost five
  minutes.

If a working example exists, find it before reasoning about why yours does not work. If a
third-party component is involved, read its code before you design around its description.

## When the operator contradicts you, they are usually right

The person with the device in their hand has evidence you do not. Twice a conclusion here
was contradicted by the operator and the operator was right both times, and both redirects
moved a stalled investigation forward.

Treat a contradiction as data, not as something to talk them out of. Ask what they are
seeing.

## What you should never do on your own

Some of these lessons are about restraint, not diagnosis.

- **Do not fix a hostname-verification failure by disabling hostname verification.** `pytak`
  will happily suggest `PYTAK_TLS_DONT_CHECK_HOSTNAME=1`. Taking it discards the guarantee
  that you are talking to the right server, on a system whose entire security model is
  mutual TLS. If you find yourself proposing it, propose the real fix instead.
- **Do not retry a failed SSH login.** Repeated failed auth trips fail2ban and bans the
  source, which turns a small problem into a locked-out operator. One attempt, then check
  reachability at the TCP level and tell the human what you found.
- **Do not treat a version bump as routine.** "Latest firmware" is a claim to test, not an
  upgrade to assume. Record the running version before replacing it, change one device,
  verify its core function on hardware, and only then roll to the fleet. Trackers here were
  flashed to latest stable and their GPS never fixed again.
- **Do not deploy to a live box that another build may also be deploying to.** Two chats
  shipped consoles over each other in one evening. The operator's symptom was a whole tab
  vanishing between page loads, twice, with no error anywhere, because a deploy is not a
  fault and nothing flags it.

## Offline is a different system

A kit verified with the network connected has not been verified. Three things quietly
depend on infrastructure that disappears when the cable comes out: DNS, time, and maps.
Clocks matter more than people expect, because TAK stamps CoT with staleness times, so
drift makes contacts vanish and presents as "TAK is broken".

If an install is claimed to work offline, its acceptance test is an offline install. A mesh
bundle here passed every suite and then failed on the box three times in a row, each time a
different way of secretly needing the internet. A developer machine with a network cannot
find those.

## Deleting is not revoking

This one is a security finding and it belongs in your head permanently. Certificate clients
authenticate by certificate, with password auth off, so the account is irrelevant to them.
**A guest whose account you deleted still connects.**

Revocation is: account, then the CRL, then the tailnet. The second step is the one that
works and the one most easily skipped, because the first feels like completion.

## Working the symptom table

`references/symptoms.md` lists the faults that have actually happened here, keyed by what
the operator sees rather than by what is wrong. Use it as a first pass, not an answer:
match the symptom, read the cause, then **confirm the cause on the box before acting on
it**. A table entry is a hypothesis worth testing first, and nothing more.

When a fault is not in it, say so plainly rather than reaching for the nearest entry. A
confidently wrong diagnosis costs more than an honest "I do not recognise this".

## Adding to this

If you and the operator solve something that was not in the table, that is a lesson and it
belongs in `the lessons log` with the symptom, the cause, and how it was found. Offer to write
it. This file exists because someone did that every time.
