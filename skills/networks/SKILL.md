---
name: networks
description: Private networks. Join or remove a box from a VPN, check its membership, and install a console on a box. Use when connecting a box to a private network or giving a box a console of its own. The Meshtastic mesh is Mesh Manager's, reached from a box's tile; this skill only points there. Names the script behind each action.
audited: 2026-09-03
audit_verdict: pass
audited_with: skill-safety-audit v3
audit_sha: b0926af517b7f965
audit_sha_source: 03205b60595be42e
origin: the development repository/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: Vantage Community Licence
category: operations
---

# Networks: private networks, and where the mesh went

The Networks action group is the bearer layer under the estate: the private network a box
joins so the console can reach it, and the console a box can carry for itself. It is no
longer a tab. ADR-001 made Networks a tab with Meshtastic as its first network type;
ADR-005 superseded it on 3 September 2026, and the tab, the channel store, the mesh map and
the kit guide left the console in console 2.56.0.

## The actions in this group

| Action | Script on the box | Risk | What it does |
|---|---|---|---|
| `vpn-status` | `console/actions/tak-vpn` | read | The box's private-network state |
| `vpn-join` | `console/actions/tak-vpn` | write | Join the box to the private network; the join key is used once and shredded |
| `vpn-leave` | `console/actions/tak-vpn` | write | Leave the private network |
| `deploy-console` | `console/actions/tak-install-console` | write | Install a console on the box so it can manage itself (Spec 003) |

All four ride two scoped keys (`id_action_vpn`, `id_action_install_console`), mapped to their scripts at enrolment by `console/console-enrol-server.sh`.

Each runs over its own scoped key, named on the action in `ACTIONS` in
`console/vantage-console-serve.py`; read the action's `desc` and `confirm` there before
proposing it. Joining or leaving a private network changes how the console reaches the box:
say so in the proposal, and never leave a box you cannot otherwise reach.

## The Meshtastic mesh

The mesh, its devices, channels, firmware and map belong to **Mesh Manager**, a standalone
application on the box that carries the radio (repository `MilUX-Ltd/mesh-manager`,
GPL-3.0-or-later). A box running it shows a Mesh Manager link on its tile and its server
page, on port 8093, beside CloudTAK's. The checker records its two units
(`mesh-manager-bridge`, `mesh-manager-screen`) in the software inventory, and the console
can restart them and read their journals through the ordinary service actions.

Do not propose mesh work through this skill. Point the operator at the Mesh Manager link,
or at its own installer for a box that does not have it yet. Installing Mesh Manager as a
Vantage module through the provisioner is the join still to come (an internal card's follow-up).

## When the mesh is quiet

A quiet mesh is not a broken gateway. The Overview tile's mesh line comes from
`/var/lib/vantage-mesh/heartbeat.json`, which Mesh Manager's bridge writes only when it has
forwarded something to TAK. A running bridge with nothing to forward reads quiet, and that
is the truth.
