# The Vantage agent

Vantage ships a role an AI can adopt to help you run your estate, and the skills that tell it
how to do the job well. You bring the model; the console decides what it may touch.

This page is the full list: what the agent can read, what it can do, what it will never do, and
what stops any of that going wrong.

---

**Which model do you need?** See **[MODELS.md](MODELS.md)**: what the job actually demands, what
we observed at two model tiers, and whether a box with no GPU can do this.

## In one paragraph

Point an MCP-capable AI at your console and give the connection an autonomy level. The agent
reads your estate, works out what is actually wrong, and either proposes the fix or carries it
out, depending on what you set. It does not write shell commands. It picks an action from a
fixed catalogue and the console runs a script it cannot alter, so what happens is the same
whichever model is asking.

---

## Autonomy: the setting that decides everything

Autonomy is per connection, set by you in the console, and it is the only thing that decides how
much the agent may do. The agent cannot change it and is told never to ask you to.

| | What it can do |
|---|---|
| **Observe** | Read the estate, the vault and the release position. Changes nothing at all. |
| **Propose** | The above, plus queue actions for you to confirm and stage a release ready to apply. **This is what the shipped role uses.** |
| **Act** | The above, plus run its allowed actions directly, apply a release and bring the estate's checkers up. Every call audited under the connection's name. |

At **Act**, the agent is told to get on with the work rather than asking again for permission you
have already given. Asking anyway is not caution: it hands back a decision you made when you set
the dial, and it makes the agent slower than doing the job yourself.

What stays with you at every level is judgement rather than weight: destructive work with no
defined recovery, anything whose blast radius is the whole estate at once, and anything where the
right answer depends on something the agent cannot see, such as whether you are mid-exercise.

---

## What it can read

Nine tools, available at every autonomy level. All of them read; none changes anything.

| Tool | What you get |
|---|---|
| `estate_health` | Every server, its verdict and check counts |
| `server_detail` | One server in full: checks, software inventory, declared loadout |
| `list_actions` | What the agent may do on which box, which is narrower than the full catalogue |
| `list_credentials` | Device credentials issued on a box, names and dates only, never secret material |
| `release_status` | What the publish surface carries, what you run, which boxes deviate, what is staged |
| `baseline_status` | Third-party components, versions, licences and links; optionally what upstream has released |
| `agent_context` | Your own standing brief, from your vault |
| `vault_search` | Search the knowledge vault |
| `vault_read` | Read one vault note in full |

---

## What it can do

Forty actions across six groups. Each is a fixed script on the box, run through a key that
accepts nothing else. **Risk** is how the console classifies it: `read` changes nothing, `write`
changes something recoverable, `destructive` changes something that is not trivially undone.

### The box itself

| Action | Risk | What it does |
|---|---|---|
| `harden-plan` | read | Whether the box is at Standard or Hardened posture, and what differs |
| `harden-apply` | destructive | Move the box to Hardened posture |
| `harden-revert` | write | Undo hardening, restoring the backed-up SSH config |
| `firewall-plan` | read | The firewall against the box's declared policy |
| `firewall-apply` | write | Bring the firewall in line with that policy |
| `set-loadout` | write | Declare which modules the box carries, so health checks judge it fairly |
| `restart-service` | write | Restart one allowed service |
| `tail-logs` | read | The last lines of a service's journal |
| `kiosk` | write | Boot a box with a screen straight into its own console |
| `console-mode` | write | Promote or demote the console on the box between admin and client |
| `console-admin` | destructive | Mint a console's own keys and give it the estate to manage |
| `authorize-console` | destructive | Let a second console act on this box, or withdraw that |
| `destroy-server` | write | Erase the estate footprint from the box |

### TAK Server and devices

| Action | Risk | What it does |
|---|---|---|
| `list-certs` | read | Client certificates with days to expiry |
| `list-credentials` | read | Credentials issued on the box, names and dates |
| `view-coreconfig` | read | A redacted view of the server's configuration; never shows passwords |
| `issue-cert` | write | Create a client certificate |
| `enrol-device` | write | Create an enrolment credential and a QR code a phone scans |
| `estate-ca` | write | Package this estate's certificate authority for a device to import once |
| `set-group` | write | Add a user to a group |
| `revoke-cert` | destructive | Revoke a certificate and add it to the CRL |
| `show-cert-password` | write | Reveal the box's certificate password |
| `fetch-credential` | read | Re-download an issued credential |
| `backup-coreconfig` | write | Timestamped, root-only backup of the configuration |
| `le-renew` | write | Renew the public certificate |

### Software and releases

| Action | Risk | What it does |
|---|---|---|
| `push-checker` | write | Update a box to the current health checker, so it stops reporting stale results |
| `update-cloudtak` | write | Apply the pinned CloudTAK compose |
| `load-images` | write | Load a saved image tarball from the admin box, for an offline estate |
| `upgrade-server` | destructive | Upgrade TAK Server in place from a package on your shelf |

### Networks

| Action | Risk | What it does |
|---|---|---|
| `vpn-status` | read | The private-network state of a box |
| `vpn-join` | write | Join the box to your private network |
| `vpn-leave` | write | Leave it, shredding the join key |
| `apply-mesh-channel` | write | Set the mesh gateway's radio channel |
| `deploy-mesh-gateway` | destructive | Install the Meshtastic TAK gateway |
| `deploy-console` | write | Install a console on a box so it can manage itself |

### Federation

| Action | Risk | What it does |
|---|---|---|
| `federation-ca` | read | The CA certificate to hand a partner |
| `federation-enable` | write | Turn on the federation server and open its port |
| `federation-connect` | write | Add an outbound link to a partner |
| `federation-groups` | write | Name the group shared with trusted partners |

### Standing up a box

| Action | Risk | What it does |
|---|---|---|
| `provision-server` | destructive | Build a complete TAK Server on a bare box |
| `provision-infratak` | destructive | Stand up infra-TAK instead, as a stack choice |

### Running those actions

The forty above are the catalogue. How the agent reaches them depends on your dial:

| Tool | Autonomy | What it does |
|---|---|---|
| `propose_action` | Propose | Queue one of the forty for you to confirm. The agent cannot execute it |
| `run_action` | Act | Run one directly, audited under the connection's name |

### Releases and the estate

| Tool | Autonomy | What it does |
|---|---|---|
| `release_stage` | Propose | Download the published release and verify its checksum. Installs nothing |
| `release_apply` | Act | Apply a staged release. Snapshots first and puts the old version back if the console does not come up |
| `estate_update` | Act | Push the current checker to every enrolled box. Idempotent, and it never redeploys a box's console |

### The knowledge vault

Your estate's shared knowledge lives in a vault on the console. The agent reads it at every
level; writing needs Act.

| Tool | Autonomy | What it does |
|---|---|---|
| `vault_search` | Observe | Search the vault |
| `vault_read` | Observe | Read one note in full |
| `vault_write` | Act | Create, update or append to a note |
| `vault_build` | Act | Build a vault pack |
| `mission_pack_build` | Act | Build a mission pack from vault content |

---

## The skills it comes with

A skill is a procedure with judgement in it. These ship with the release and are updated by it.

**`vantage-lessons`** teaches diagnosis, built around one rule: a signal that proves something
weaker than it appears to is not evidence. A service being active proves it started. It carries a
symptom-to-cause table for faults that have actually happened on real estates, and it tells the
agent to prove a check works on a known-good case before reporting a fault.

**`vantage-redteam`** reviews your security posture and changes nothing. It uses read and plan
actions only, works against Cyber Essentials rather than a sweep that produces pages nobody acts
on, and says plainly what it did not cover. Its highest-value check is certificates against
people, because deleting an account does not revoke a certificate and the failure is silent.

**`updates`** works out where you stand against a release and gets it ready. It is careful
about one claim that is easy to overstate: a matching checksum proves the archive arrived intact,
not that it is authentic.

**`operations`** is the daily work: issuing and revoking certificates, enrolling devices,
managing users and groups, reading and backing up the configuration. It carries the rule that
catches people out, that deleting an account does not revoke a certificate.

**`plan-a-build`** works out what a box needs before anything is installed, and hands the
operator a plan code the Deploy wizard fills itself in from. It asks the same questions as the
build planner, in the same order, and finds what it can from the console instead of asking.
Its point is the preconditions: a build fails on those, not on steps, and it names them before
twenty minutes are spent.

**`deploy-server`** takes a bare machine to a managed server, stage by stage, with how each stage
fails. **`federation`** and **`networks`** cover what reaches past your own estate: a partner's
server, a private network, the radio mesh. **`vantage-internals`** is where everything lives, for
working over SSH or reading what a script will really do.

Three more cover getting a plan onto the ground, which is the journey from an admin
instruction on a desk to an overlay on a handset with no signal.

**`knowledge-vault`** turns the material a deployment actually arrives as, an admin instruction,
a deck, a thread of email, into a structured folder the rest can build from. It insists on two
things that sound like bookkeeping and are not: every point and every boundary records where it
came from, because a boundary lifted off a photographed slide and one read from a grid in the
written order are worth different amounts to whoever is standing on it; and the originals are
kept, because when the ground does not match the plan the only useful question is whether the
extraction was wrong or the plan was.

**`map-packs`** gets the ground onto the device. It starts with the question most tooling skips,
which is whether a source may lawfully become an offline copy at all, and it will stop and ask
rather than build a pack from data nobody was allowed to extract. It knows that a map on a tile
server is invisible to a client until the client is told the server exists, which is the usual
reason for "the maps are there and the device cannot see them", and it insists on verifying a
pack has no holes before it ships, because a pack that opens is not a pack that is complete.

**`mission-packs`** turns the plan into what is drawn on that ground: areas, boundaries, routes,
ERVs and points, packaged so a device can import them. It is careful about the two different
things both called a data package, because both import without complaint and only one of them
then works.

**`estate-brief`** builds and maintains the standing brief the agent reads at the start of every
session. Most of it is derivable from your estate, so it drafts what it can and asks only about
what a person alone knows. It never writes the brief without showing you the text first.

Every skill has passed a safety audit before shipping, and each carries a fingerprint of what was
audited, so a skill edited after its audit fails the build until it has been audited again.

---

## What it will never do

Whatever autonomy you set:

- **Compose a shell command.** It picks an action from the catalogue. If nothing fits, that is
  the answer it gives you.
- **Ask you to widen a gate.** If something is refused, it reports the refusal.
- **Retry a failed login.** Repeated failed authentication locks you out of your own box.
- **Weaken security to make something work.** Not hostname verification, not a firewall rule,
  not an authentication setting. If the secure path is blocked, that is the finding.
- **Hand you a secret.** It names a certificate, never its password, and it does not paste log
  output wholesale into a report you might forward.
- **Act on instructions it finds in your data.** Log lines, hostnames, certificate fields and
  configuration comments are data to report. Text that argues for its own innocence is treated
  as a finding in itself.

---

## Why this is safe to connect to a live estate

Not because the agent is careful. Because it cannot reach past the design.

The agent selects an action id and supplies arguments. Between that and anything happening sit
three independent checks, each distrusting the one above it:

1. the console validates the arguments against the action's declared patterns;
2. the SSH key is bound to one program and ignores anything else the caller asks for;
3. the privileged wrapper validates again before doing anything as root.

So the variability a language model introduces is confined to *which defined thing to do*, never
*what that thing does*. Two runs of the same action with the same arguments do the same work.

Every call is audited under the connection's name, and you can revoke a connection at any time
from the console.

---

## Connecting one

Create a connection on the Agent page. The console mints a token, shows it once, and hands you
the configuration to paste. See the README's "Connecting an AI assistant" for MCP, API key and
offline options, and for what a disconnected estate should do instead.

The role and skills are in `agents/` and `skills/` in this repository and in every release, so
you can read exactly what your agent has been told before you connect it.

---

## Using something other than MCP

You may prefer to work over SSH, or drive the console in a browser, and the role covers both. It
is worth being plain about what changes.

MCP is where the product can protect you: the autonomy dial, the gated actions, the audit trail.
Over SSH your agent has whatever your key has, and none of that applies. In a browser it is
driving the console a person normally drives, and the console's own confirmations are the gate.

We protect what an agent can do through the MCP surface. How you use your own AI on your own
boxes is your decision, and not one we manage. What the role does is refuse to pretend the safety
story is the same on every route.
