# The checks, and what each one actually proves

Grouped by the Cyber Essentials control they sit under, because that is the framing an
operator will be asked about. Every check uses a read or plan action; none changes a box.

A check is only worth running if you can say what it proves. Where a check proves something
weaker than it looks, that is written down here rather than left for someone to discover.

---

## Firewalls

**Open ports against the declared loadout.** `firewall-plan` gives the firewall;
`server_detail` gives the box's declared loadout and its per-port checks. Both sides are
machine-readable, so a mismatch is a fact rather than an opinion.

Do not substitute your own knowledge of which ports a component "normally" uses. If the
loadout is absent (a box that declares nothing) or a component's ports are not in the check
list, say the cross-check could not be completed and why. A guess about default ports
presented as a verified finding is exactly the kind of confident wrongness that costs an
operator trust in the rest of the report.

Report the two directions separately, because they mean opposite things:

- **A port open for an undeclared component** is exposure. Something is reachable that
  nothing on this box is supposed to be serving.
- **A declared component with its port closed** is a broken service waiting to be
  discovered, not an exposure. It belongs in the report, phrased as such.

*Proves*: what the box's own firewall says. It does not prove reachability from anywhere in
particular, because you are not permitted to probe from outside. Do not report "not
reachable from the internet" on this evidence.

**The management surface.** The console's own port, and SSH. Note whether SSH still accepts
passwords, and whether the console is on open auth.

---

## Secure configuration

**Hardening drift.** `harden-plan` computes the difference between the box and its baseline.
Report the diff as the finding. `harden-apply` is not yours, and `harden-revert` exists,
which is worth telling the operator when they weigh applying it.

*Proves*: drift from a baseline. It does not prove the baseline is right for this box.

**Server configuration.** `view-coreconfig` shows what the server is actually running.
Worth reading for: authentication settings, whether anonymous or password access is enabled
alongside certificates, input filter groups, and the connectors in use.

An input with **no filter groups** is a finding on both counts here. Its events belong to no
group, so no client sees them, which makes it a broken input; and it is the exact shape of
fault that sat silently on this estate for a week.

**The declared loadout itself.** A box declaring nothing is judged against its profile,
which on some boxes means being checked for software it does not have. Not a security
finding, but it makes every other check less trustworthy, so raise it early.

---

## User access control

**Certificates against people.** The highest-value check here, and the one with the
quietest failure mode.

`list-certs` gives every certificate the server has issued. Cross-check against who should
still have access. **Deleting an account does not revoke a certificate**: certificate
clients authenticate by certificate with password auth off, so the account is irrelevant to
them, and a guest whose account was deleted still connects.

So the finding to look for is a certificate that is live on the server while its holder is
gone. Revocation is: account, then the CRL, then the tailnet, and the middle step is the one
that works and the one most often skipped.

Also worth reporting:
- certificates approaching expiry, which is an availability finding rather than a security
  one, and should be labelled that way;
- certificates issued for a name that no longer matches the server, which will have broken
  something already (see the lesson about renaming a server certificate);
- a self-signed connector certificate, which is expected on an offline build and is not a
  finding there. Say which case you are looking at.

*Proves*: what the server has issued and revoked. It does not prove who holds a copy of a
private key, and no check available to you does.

**Console credentials.** `list-credentials` shows what the console holds for a box. A
credential still held for a box that has been retired is a finding. Name the credential;
never retrieve or print its value.

**Group membership.** Group changes need a TAK restart to take effect, because the x509
mapping is cached. A group change that was made but never restarted into is a real gap
between the intended posture and the running one, and it will not show up anywhere else.

---

## Patch management

**Release drift.** Which boxes have taken the current Vantage release and which have not, and
what the difference contains. A box behind on a release that fixed a security defect is a
patch finding.

**Third-party baseline.** `vantage-baseline --check` reports what upstream has released
against what the estate is pinned to. Being behind is not automatically a finding: a pin is a
deliberate choice, and moving one needs testing. Report it as drift with the versions named,
and let the operator judge. Where an upstream release is specifically a security fix, say so,
because that changes the calculus.

**Unattended upgrades.** Whether the box is taking distribution security updates at all.

---

## Malware protection

Largely not applicable to these boxes, and **say that rather than scoring it**. They are
appliances running a known service set, not general-purpose workstations. What is worth
checking instead is what can execute: whether anything unexpected is installed, and whether
the software on the box came from the shelf or from somewhere else.

---

## Federation and trust

Not a Cyber Essentials control, and the most consequential thing on a TAK estate, so it gets
its own section.

`federation-ca` gives the trust anchor. What to look at:

- **who is trusted**, and whether that list still matches the current arrangement;
- **what they can see**, through the federation group mapping. Federation trust plus a
  generous group mapping gives a partner more than most people intend, and the two are
  usually configured at different times by different people;
- whether federation is **enabled at all** on a box where nobody expects it to be.

*Proves*: the configured trust. It does not prove what a federate is actually pulling.

---

## Estate posture

Findings that only exist when you look across boxes rather than at one:

- a console still on **open auth**;
- a box that was provisioned but **never enrolled**, so nothing is watching it;
- a box whose **checker is old**, which means it is reporting old truths and nothing else
  would say so;
- **two boxes claiming the same version with different behaviour**, which has happened here
  when parallel builds shipped over each other;
- a **credential or target entry for a box that no longer exists**.

---

## What is deliberately not checked

Written down so nobody assumes it was covered:

- **No external probing.** Nothing is scanned from outside. You cannot say what is reachable
  from the internet, only what the box's firewall is configured to allow.
- **No credential testing.** No login is attempted, ever, against anything. Beyond the rule
  against it, repeated failed auth trips fail2ban and locks the operator out.
- **No key custody.** Nothing available to you shows who holds a copy of a private key.
- **No physical or personnel controls**, which for a deployable kit are a real part of the
  posture and are simply out of scope here. Worth saying in the report so the operator knows
  the boundary of what they have been handed.
