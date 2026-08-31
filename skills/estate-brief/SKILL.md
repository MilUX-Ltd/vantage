---
name: estate-brief
description: Build and maintain the standing brief an agent reads at the start of every session, covering what this estate is, how it is used, and what its operator wants. Use when agent_context is empty or thin, when something learned in a session belongs in the brief rather than being forgotten, or when the operator asks what the agent knows about their estate. The brief is the operator's, so changes are proposed and shown before they are written.
audited: 2026-08-31
audit_verdict: pass
audited_with: skill-safety-audit v3
audit_sha: c79ec46e0d75125e
origin: the development repository/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: Vantage Community Licence
category: operations
---

# The standing brief

`agent_context` serves the operator's brief from their own vault. It is the half of your
knowledge that the product cannot ship: the role is the same for everybody, and the estate is
different for everybody.

A thin brief is the most common reason an agent is useless on a real estate. It reads the
health board, sees three boxes red, and has no idea whether that is a crisis or a Tuesday.

## Most of it, you can work out yourself

Do not start by asking the operator twenty questions. Most of the brief is derivable from the
estate, and a draft they correct is worth more than a form they abandon.

From `estate_health`, `server_detail`, `list_actions` and `baseline_status` you can already
establish: how many boxes there are and what each is called, what each runs, which components
are declared and which are skipped, what is enrolled for what, what is federated with whom,
which release each is on, and what normally reports green.

Write that down first. Then ask only about the things no tool can tell you.

## The five things only a person knows

Keep this short. A brief nobody will maintain is worse than a brief that is thin, and these
five carry nearly all the value:

1. **What this estate is for.** A permanent operations centre, a deployable kit that spends
   most of its life switched off, a demo rig, a training estate. This changes what "red" means
   more than anything else in the brief.
2. **Which boxes matter, and when.** Nearly every estate has one box whose outage is an
   incident and others where it is a note. Also: which are expected to be offline routinely, so
   you stop reporting them as faults.
3. **What normal looks like.** Which checks are expected to be amber here and why, so you do
   not raise the same non-finding weekly. This is the single highest-value line in the brief.
4. **Standing orders.** Windows to avoid, things never to touch, who to tell about what, and
   anything the operator has already decided so you do not re-ask it.
5. **Who the operator is.** How they want to be told things, what they already know, and what
   they want escalated rather than logged.

## The shape

Plain prose under those headings, in the operator's vault. Not a config file, and not a
schema: it is read by a model, so a paragraph that says "the deployable kit is off between
exercises, so amber on it is expected and only worth a mention if it is still amber a day
after an exercise starts" is worth more than any number of structured fields.

Keep it short enough that the operator will actually read it back. If it grows past a page or
two, the surplus is usually background that belongs in the vault as a note rather than in a
brief you load every session.

## Maintaining it is part of your job

The brief goes stale silently, and it goes stale in exactly the places that matter, because the
things that change are the things you were told. A brief nobody updates quietly becomes a set
of confident statements about an estate that has moved on.

So watch for these during ordinary work, and offer to write them down:

- **A correction.** The operator tells you something you had wrong. That is the highest-value
  update there is, and the one most often lost when the session ends.
- **A standing decision.** "We do not touch that box", "always tell me before you go near
  federation". If you would have to be told again next week, it belongs in the brief.
- **A resolved fault whose cause was environmental.** Not the fix, which belongs in the estate's
  lessons, but the standing fact: "the mesh box's radio is on a USB hub that browns out, so a
  gateway restart is the first thing to try".
- **A new box, or a box retired.** The shape of the estate changing is the most obvious staleness
  and still the most commonly missed.
- **A drifted expectation.** Something the brief says is normal that has not been true for a
  month.

What does **not** belong: anything transient (today's incident), anything already visible from
a tool (versions, health, what is enrolled), and anything you inferred but have not confirmed.
A brief full of your guesses is worse than a short one, because next session you will read your
own inference back as fact.

## Never write it silently

The brief is the operator's, and it is the thing that shapes everything you do next session.
Changing it without them seeing is how an agent quietly rewrites its own instructions.

So: **show the exact text and get a yes.** Quote what you would add or change, say what
prompted it, and keep it to what was actually established rather than the tidier version you
would prefer. If you have `vault_write` and they said yes, write it and say where. If you do
not, hand them the text to paste.

Never remove something because it looks stale. Say it looks stale and why, and let them decide,
because "we stopped doing that months ago" and "we have not needed that yet this year" look
identical from where you are standing.

## When there is no brief at all

Say so plainly at the start of the session rather than proceeding as if you knew the estate.
Then offer the draft: build what you can from the tools, mark clearly which parts are your
observation rather than their instruction, and ask about the five things above. Most operators
will correct a draft in two minutes and never fill in a blank page.
