---
name: vantage-internals
description: Where everything in Vantage lives and how the pieces fit. The action scripts, the provisioners, the health checker, the console, and the three validation layers between a request and a box. Use when working over SSH rather than the console, when you need to read what an action will actually do before proposing it, when something is not in the action catalogue, or when explaining to an operator why the design is safe.
audited: 2026-08-31
audit_verdict: pass
audited_with: skill-safety-audit v3
audit_sha: 0e5d98148a6851a6
origin: the development repository/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: Vantage Community Licence
category: engineering
---

# How Vantage is put together

Read this when the console is not the surface you are working through, or when you want to know
what an action will really do before you propose it. Everything here is readable: the product is
source-available, so "go and read the script" is always available to you and to the operator.

---

## The pieces

| What | Where | Job |
|---|---|---|
| The console | `console/vantage-console-serve.py` | One file, standard library only. Serves the interface, holds the action catalogue, and is the MCP server |
| Action scripts | `console/actions/tak-*` | One per action. What actually runs on a box |
| Privileged wrappers | `console/actions/tak-*-priv` | The half that runs as root, and validates again before it does |
| Provisioners | `console/provision/*.sh` | Build a server from bare: `vantage-tak-provision.sh`, `vantage-infratak-provision.sh`, `vantage-mesh-gateway-install.sh` |
| The health checker | `common/tak-health.sh` | Decides whether a box is healthy, and what "healthy" means for that box |
| Enrolment | `console/console-enrol-server.sh` | Installs the forced-command keys that let a console act on a box |
| The release helper | `console/vantage-apply-release` | Applies a release detached, and rolls back if the console does not come up |
| The baseline | `console/vantage-baseline` | What the estate is built on, read from the scripts that pin it |

The action catalogue in the console maps an action id to a key; `console-enrol-server.sh` maps
that key to the script it is allowed to run. Those two tables together are the answer to "what
does this action actually do".

Worked example. `harden-plan` and `harden-apply` both carry the key `id_action_harden`, which the
enrolment table maps to `tak-harden`. So both run the same script with different arguments, and
reading `console/actions/tak-harden` tells you exactly what each will do. The same holds for
`firewall-plan` and `firewall-apply` on `tak-firewall`, and for `vpn-status`, `vpn-join` and
`vpn-leave` on `tak-vpn`. One script, several actions, one place to read.

Use `list_actions` to see what a given box actually allows, which is narrower than the catalogue
and differs per box, and `estate_health` for the estate's own view of itself.

---

## The three layers

This is the part worth understanding properly, because it is what makes an agent safe to connect
to a live estate, and it is not the agent's own care.

A request to do something passes three independent checks, **each distrusting the one above it**:

1. **The console** validates the arguments against the action's declared input patterns. An
   argument that does not match the pattern never leaves the console.
2. **The forced-command key** runs one program and ignores whatever the caller actually asked
   for. Even a caller with the key cannot use it to run something else.
3. **The privileged wrapper** validates again before doing anything as root, on the box, with no
   trust in either layer above.

So the only variability anything upstream introduces is **which defined thing to do**, never what
that thing does. Two runs of the same action with the same arguments do the same work.

This is why "never route around the registry" is a real rule rather than a preference. A shell
command that achieves the same outcome defeats all three layers at once, and leaves no audit
trail. If no action exists for what is needed, that is the finding: say what is missing and let a
person decide whether it should exist.

---

## The health checker

`common/tak-health.sh` is what decides a box is green, so it is worth knowing how it makes that
decision.

It carries **profiles** (what a class of box is expected to run) and a **component map** (what
each declared component means in services and ports). A box's `/etc/tak-health.d/loadout.conf`
narrows the profile to what that box actually carries.

Three things follow:

- **An old checker reports old truths**, and nothing else would tell you. The console shows each
  box's checker version for this reason. A box behind on the checker may be green about the wrong
  things.
- **`--print-profile`** shows what the checker expects for a box without running anything against
  it. Useful for answering "why does it think this box should have that?"
- **`--self-test`** runs its own assertions. If you are about to report a fault, running it costs
  nothing and rules out the checker being the fault. Four "faults" here in one day turned out to
  be the checking code.

---

## Working over SSH

The role covers this: over SSH your agent has whatever the operator's key has, and none of the
console's gates apply. The product cannot protect you there and does not pretend to.

What that means in practice:

- **Read first.** The scripts are readable. `console/actions/tak-harden` tells you what
  hardening will do far better than any description of it.
- **Prefer the action.** If you find yourself doing over SSH what an action exists for, use the
  action. Not for safety theatre, but because the action is audited, has a defined result, and
  behaves the same next time.
- **`ssh -t` for anything needing sudo**, and never pipe an interactive sudo command: the prompt
  goes to stdout, the pipe swallows it, and it reads as a hang.
- **`pkill -f <name>` matches its own command line** and will kill the shell you launched it
  from. Separate the kill and the relaunch.
- **One authentication attempt.** Repeated failures trip fail2ban and lock the operator out of
  their own box.

---

## Reading the estate's own state

| File | What is in it |
|---|---|
| `/etc/tak-health.d/loadout.conf` | What the box declares it carries |
| `/var/lib/vantage-console/` | The console's state: targets, actions, history, keys |
| `/var/lib/vantage-console/last-apply.json` | The verdict from the last release applied, including whether it rolled back |
| `/root/infra-tak-admin-pass` | On an infra-TAK box only, root-readable |

The console's own store is where an estate's shape lives. It is not a place to write by hand
while the console is running.

---

## Where the product's own boundaries are written down

Not folklore, actual files:

- `AGENT.md` at the root of a release: everything an agent can do.
- `THIRD-PARTY.md`: what the estate is built on, generated from the scripts that pin the versions,
  so it cannot disagree with what is installed.
- `agents/` and `skills/`: the role and skills the release ships, so an operator can read what
  their agent has been told before connecting it.
- `LICENSE` and `NOTICE`: the Vantage Community Licence, and the third-party components that keep
  their own.

If you are asked something about the product's behaviour and you are guessing, one of these files
or one of the scripts above has the answer. Read it rather than reasoning about it. That habit
has beaten a morning of plausible theorising here more than once.
