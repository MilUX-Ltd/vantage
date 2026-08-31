# Skills

Procedures with judgement in them, written down so an AI connected to a Vantage console does
the job the way someone who has run this estate would.

These are **product content**. They travel in the release tarball, they are the same for
every customer, and they are updated by the update path like any other product file. There
is no second distribution channel: the repository is the registry.

## The rule that keeps them portable

**A skill may never assume which AI is reading it.**

Bodies reference the console's own MCP tool names (`estate_health`, `run_action`) and its
action ids (`harden-plan`, `list-certs`). They do not reference one vendor's API, SDK or
product features. MCP is the cross-vendor standard, so anything that speaks it can use these,
and a customer who prefers a different tool is not stuck with ours.

`console/test-skills.py` enforces the half of that which can be checked mechanically: every
tool and action a skill names must actually exist in the product. A skill referring to a tool
we no longer have is a build failure, not a customer's confusing afternoon. That check is the
main defence against the way documentation normally rots.

## The audit gate

Every skill is audited with `skill-safety-audit` before first use, from any source (R-28).
Shipping one to a customer raises that bar rather than lowering it, so the audit is an
**acceptance criterion**, enforced in two places:

- **`test-skills.py` fails the build** unless every skill here carries `audit_verdict: pass`,
  names the auditor, and records a real audit date. A skill still being written lives outside
  `skills/` until it has been through the audit.
- **`package-vantage.sh` refuses to ship** a skill without that pass verdict, so even a
  mistake in the repo cannot reach a customer.

R-28 also says re-audit on every update, and a date cannot catch an edited body: someone can
change what a skill tells an agent to do and leave last month's pass sitting above it. So each
skill records an `audit_sha` over everything the auditor read, with the `audit_*` lines
excluded so it is not self-referential. **Change a word and the build goes red** until the
skill has actually been audited again. The failure message prints the new hash to record.

## Frontmatter

Same shape as every other skill MilUX writes, because a second convention is a second thing to
maintain:

```yaml
name:           the skill's id, matching its directory
description:    what it does and when to use it, with the trigger phrases in it
audited:        the date it was audited, YYYY-MM-DD
audit_verdict:  pass (anything else does not ship, and does not build)
audited_with:   the auditing skill and version
audit_sha:      fingerprint of what was audited; re-audit when it changes
origin:         the development repository/skills
source:         MilUX Ltd
maintainer:     MilUX Ltd
license:        Vantage Community Licence
category:       engineering | security | operations
```

Bundled `references/` holds the long material a skill points at rather than carrying inline.

## What is here

| Skill | For |
|---|---|
| `vantage-lessons` | diagnosing the estate, and knowing which signals are worth believing |
| `vantage-redteam` | reviewing the security posture against Cyber Essentials, changing nothing |

Both passed `skill-safety-audit` on 31 August 2026 and ship with the release.
