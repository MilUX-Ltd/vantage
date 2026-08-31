# Starter brief

A template for `agent_context`. Everything in angle brackets is for the operator to replace;
everything else is a prompt for the kind of thing that belongs there.

Offer to fill the derivable parts yourself from `estate_health`, `server_detail`,
`list_actions` and `baseline_status` before asking anything. A draft to correct beats a form to
complete.

Keep the whole thing to a page or two. This is loaded every session, and a brief nobody
maintains is worse than a brief that is thin.

---

## What this estate is for

<One or two sentences. A permanent operations centre; a deployable kit that is switched off
between exercises; a demo rig; a training estate. This changes what "red" means more than
anything else here.>

<If the estate has a rhythm, say so: quiet most of the month and heavily used for a week; only
alive during exercises; always on.>

## The boxes

<One line each. Name, what it is, where it lives, and how much it matters.>

| Box | What it is | If it goes down |
|---|---|---|
| `<name>` | <the main server / the deployable kit / the admin console> | <incident, or a note> |

<Say which are expected to be offline routinely. An agent that reports a switched-off kit as a
fault every week trains you to ignore it.>

## What normal looks like

<The highest-value section, and the one most often left blank.>

<Which checks are expected to be amber here, and why. Anything that has been reported before
and is not a fault. Anything that looks wrong to a stranger and is deliberate.>

<Examples of the shape: "the kit's certificate is self-signed on purpose, it is never on a
public name"; "box X declares no TAK Server because it runs the other stack"; "the mesh box
shows no traffic outside exercises".>

## Standing orders

<Anything already decided, so it does not get re-asked.>

- **Never** <the things that are off limits, and why if the why is not obvious>
- **Always tell me before** <the things you want to hear about first>
- **Windows to avoid** <exercises, maintenance, anything where a restart is unwelcome>
- **Autonomy** <what you want done without asking, and what you always want to see first>

## Who I am, and how to tell me things

<How you want to be told. What you already know, so it does not get explained back. What you
want escalated rather than logged, and where.>

<If more than one person uses this estate, say who does what.>

---

## Notes on keeping this current

- Corrections belong here the day they happen. "You had that wrong, box X is the spare" is the
  most valuable line in any brief and the easiest to lose.
- Fixes belong in the estate's lessons, not here. What belongs here is the standing fact a fix
  left behind.
- Nothing transient. Today's incident is not a standing brief.
- Nothing a tool already reports. Versions, health and enrolment are read live; writing them
  here only creates a second copy to go stale.
