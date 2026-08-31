---
name: federation
description: Join a TAK server to a partner's, and check who is trusted and what that trust exposes. Use when setting up federation, adding a partner, reviewing federation trust and group mapping, or working out why events are not crossing a link that looks connected. Names the script behind each action.
audited: 2026-08-31
audit_verdict: pass
audited_with: skill-safety-audit v3
audit_sha: b5c2614580bcea2f
origin: the development repository/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: Vantage Community Licence
category: operations
---

# Federation: joining to a partner

Federation reaches past your estate onto somebody else's screen. That is what makes it
different from the rest of the work: a mistake here is visible to a partner, so treat it
as a decision to hand over rather than a task to complete, whatever your autonomy.

## Federation

Federation joins your TAK server to a partner's, so contacts and events cross between them.

### The order it goes in

**1. `federation-enable`** &rarr; `console/actions/tak-federation-enable`

Turns on the v2 federation server on port 9001 and opens it in the firewall. This makes the box
reachable for federation; it does not yet trust anybody.

**2. `federation-ca`** &rarr; `console/actions/tak-federation-ca`

Shows this server's certificate authority certificate. You hand this to the partner so they can
trust you, and they hand you theirs. It is a read action and safe to run at any time.

**3. `federation-connect`** &rarr; `console/actions/tak-federation-connect`

Adds an outbound link to the partner's v2 port. Federation can be established from either end, so
check whether they are connecting to you before you also connect to them.

**4. `federation-groups`** &rarr; `console/actions/tak-federation-groups`

Names the group shared with trusted partners. **This is the step that decides what they can
actually see**, and it is the one most often left at whatever it defaulted to.

### What goes wrong

**Trust and visibility are configured separately, usually at different times.** Federation being
enabled says a partner can connect. The group mapping says what they get once they have. A
generous group with a trusted partner gives them considerably more than most people intend, and
nothing about the working connection will tell you.

So when you review federation, always report the two together: who is trusted, and what that
trust exposes. Either alone is half an answer.

**Federation you did not configure still shows up.** A partner can establish the link from their
side, and a federate nobody recorded from this console is still a live conversation. The estate's
own checker reports connected federates by remote address for exactly this reason; on the day it
was added it found a live federate on the public box that no console record knew about.

**Certificates matter here too.** Federation is mutual TLS. If a server certificate is reissued
under a different name, federation breaks along with everything else that connected by the old
one.

---

## Before you propose any of this

Three questions, every time:

1. **Who else sees the result?** A federation change is visible to a partner. A mesh channel
   change reaches every device that scans it.
2. **Is the partner expecting this?** A link established from your side lands on their server.
   Federation is a conversation between two organisations, not a setting.
3. **What is the way back?** Federation trust handed to a partner cannot be un-shown: they have
   seen what your group exposes for as long as the link was up. Disconnecting stops the flow, it
   does not undo it. Say that before rather than after.
