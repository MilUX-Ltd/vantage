---
name: networks
description: Private networks and the Meshtastic mesh. Join or remove a box from a VPN, check its membership, deploy the mesh gateway, and change a mesh channel. Use when connecting a box to a private network, when a handset is not receiving over the mesh, or before taking kit into another regulatory domain. Names the script behind each action.
audited: 2026-08-31
audit_verdict: pass
audited_with: skill-safety-audit v3
audit_sha: 72a23abd9133dd59
origin: the development repository/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: Vantage Community Licence
category: operations
---

# Networks: private networks and the mesh

How a box reaches the rest of the estate when there is no ordinary infrastructure, and how
a radio mesh gets into TAK. Both can remove your own access to a box, so read the last
section before proposing anything here.

## Private networks

**`vpn-status`** &rarr; `console/actions/tak-vpn`

The private-network state of a box for one provider: interface, address, and whether it is up.
Read-only. Run it before and after any change here.

**`vpn-join`** &rarr; the same script. Installs the provider if needed and joins the box to the
private network.

**`vpn-leave`** &rarr; the same script again. Brings the interface down, disables it at boot, and
**shreds the join key**. That last part means this is not trivially reversible: rejoining needs a
fresh key from whoever administers the network.

### What goes wrong

**Bare hostnames stop working when the network does.** Tailscale's MagicDNS intercepts them,
which works beautifully until the kit is offline and then does not. Use the fully qualified name
everywhere, and pin the box's own name in its `/etc/hosts` so services on the box do not depend
on a router booting first.

**Leaving a private network can be how you lose the box.** If the private network is the route
you reach it by, `vpn-leave` removes your own access. Check how you are connected before you
propose it, and say so.

---

## The mesh

The Meshtastic gateway bridges a radio mesh into TAK, for kit operating with no infrastructure at
all.

**`deploy-mesh-gateway`** &rarr; `console/actions/tak-mesh-deploy`, installing the vendored bundle
from `console/provision/vantage-mesh-gateway-install.sh`

Installs the gateway into its own virtual environment from a bundle on the console's shelf. It is
`destructive` because it replaces whatever gateway was there.

**`apply-mesh-channel`** &rarr; `console/actions/tak-mesh-channel`

Rewrites the gateway radio to a channel from the Networks page.

### What goes wrong, and this one has bitten hard

**A channel QR carries more than a channel.** It also carries the **region** and the
**position precision**. Both have caused real problems here:

- A recorded channel URL carried a position precision giving roughly a five-kilometre grid, which
  was the actual cause of coarse positions that were chased for a night. The device setting had
  been corrected; the recorded URL never had.
- The same URL carried `region: EU_868`. Scanning it in the United States would have put the
  fleet onto licensed cellular spectrum, at a defence event.

**Before deploying to another regulatory domain, decode the channel URL and read every field**,
then generate a region-correct QR for the destination and one for the way home. An artefact that
configures devices should be read field by field before it is trusted, especially one that
arrived by export rather than by authorship.

**A quiet mesh is not a broken gateway.** Check what the gateway has actually forwarded before
concluding anything is wrong.

**When files are not reaching a handset, suspect the radio before the code.** A data radio sitting
in bootloader mode presents as a working system that delivers nothing. Check the serial device:
an `ID_MODEL` ending `-BOOT` is the tell. Address radios by their by-id path, because ports
shuffle when things are re-cabled.

**The gateway's dependencies are pinned for a reason.** It needs a specific protobuf handling for
compressed callsigns, and a routine upgrade of the Meshtastic package destroys the fix. If a
gateway that worked stops parsing packets, suspect a dependency that moved rather than the code.

---

## Before you propose any of this

Three questions, every time:

1. **Who else sees the result?** A federation change is visible to a partner. A mesh channel
   change reaches every device that scans it.
2. **How am I reaching this box?** If the answer is the thing you are about to change, say so.
3. **What is the way back?** `vpn-leave` shreds the key. Federation trust handed to a partner
   cannot be un-shown. Say what is reversible and what is not, before rather than after.
