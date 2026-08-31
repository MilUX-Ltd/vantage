# Agent roles

A skill is a procedure. An agent role is a **job**: what an AI connected to this console is
for, which tools it holds, at what autonomy, and what it must never do.

Vantage could already host an agent long before it shipped one. The autonomy dial, the bearer
tokens, the gated actions and the audit trail all existed; what did not exist was a definition,
so every customer had to invent one. That is what this directory fixes.

## What is here

| Role | For | Ships at |
|---|---|---|
| `vantage-agent` | operating a Vantage-managed estate end to end | `propose` |

One role, deliberately. More than one invites a taxonomy nobody asked for, and the honest
description of the job is singular: read the estate, diagnose it, prepare what can be
prepared, and put decisions in front of the operator.

## It knows nothing about any particular estate

`vantage-agent.md` does not name a box, a site, a customer or a network. It cannot: it ships
to everyone. The estate-specific half arrives at runtime through the `agent_context` MCP tool,
which serves the operator's own standing brief from their vault.

That split is the whole reason this can be a product file. The role is ours and is the same
everywhere; the context is theirs and is different everywhere. An agent definition that
hardcoded an estate would be wrong on its first line for every other reader, which is exactly
what happened to the definition this replaces.

## Same gates as skills

An agent definition is instructions an AI follows, so it carries the risk a skill carries and
gets the same treatment:

- **Audited under R-28** before it can ship. `package-vantage.sh` refuses any role without
  `audit_verdict: pass`, and `test-skills.py` fails the build without one.
- **`audit_sha`** fingerprints what the auditor read, so editing a role after its audit turns
  the build red until it has been audited again.
- **Never assumes which AI is reading it.** It references MCP tool names and action ids, and
  the test checks every one of them still exists in the product.

## Frontmatter

The skill fields, plus two that only a role has:

```yaml
autonomy:  the level this role ships at (observe | propose | act)
skills:    the skills it expects to load
```

Both are declarations, not enforcement. The autonomy that actually applies is the one set on
the connection by the operator, and the console is what enforces it.
