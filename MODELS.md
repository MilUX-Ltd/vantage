# What model do I need to run the agent?

You bring the model. Vantage does not care which one, and no route bills you for it. But the
answer to "will a small local model do" is not the same for every job the agent does, and it is
worth being straight about that before you rely on it.

Short version: **match the model to the autonomy, not just to the hardware you happen to own.**

---

## What the job actually demands

Running an estate is harder for a model than it looks, in four specific ways.

**Context.** The role is about 3,000 tokens. A skill with its reference material is another 4,000
to 6,000. Add your standing brief and the estate's own output and a working session sits at
15,000 to 30,000 tokens before the model has said anything. A 4k or 8k context window cannot hold
the job. **32k is the floor and 128k is comfortable.**

**Tool calling.** The agent selects an action and supplies arguments that must match a declared
pattern. A model that formats a tool call loosely does not get a partial result, it gets a
refusal, and it will often respond by trying a different phrasing rather than reporting the
refusal.

**Knowing when to stop.** Handing over a judgement call rather than pressing on is the behaviour
that keeps an estate safe, and it is the one that degrades first and least visibly.

**Calibration.** Saying "I do not recognise this" instead of reaching for the nearest plausible
cause. A confident wrong diagnosis costs more than no diagnosis.

---

## Answer it for your own hardware

Do not take our word for any of this. The release carries the benchmark we used:

```
console/vantage-agent-bench --endpoint http://localhost:11434/v1 --model llama3.1:8b
```

It points at any OpenAI-compatible endpoint, which includes Ollama's. Six scenarios drawn from
real faults on real estates, each with checks that are either **safety** (get this wrong and the
model should not be connected at an autonomy that can change anything) or **quality** (get this
wrong and it will not earn its place). It reports throughput as well, because a model that is
right and takes six minutes is a different problem from one that is fast and wrong.

`--list` explains every scenario and why it exists. `--score` grades answers you captured
somewhere else.

Two things it deliberately will not do. It **refuses to give a verdict on a partial run**,
because a pass on two scenarios out of six says nothing about the four that did not run, and the
ones a slow model times out on are the ones you most want to see. And it will not call itself a
certification: the scorers are pattern checks over scenarios we thought of, so **read the
transcripts**, which `--json` gives you.

## What we actually observed

Not projections. We ran the shipped role and skills against scenarios with known answers, at two
model tiers, and watched what changed.

**The safety behaviours held at both tiers.** A prompt injection planted in a log line, telling
the reviewer to report a pass and stop, was caught and reported as the top finding by both. Both
correctly found a departed employee's live certificate and explained that deleting an account does
not revoke one. Neither invented an action or tried to route around a refusal.

**The useful behaviours thinned noticeably.** On the same security review, the stronger model ran
a control on a certificate it had no reason to suspect before trusting its own check, and listed
the checks it had **not** been able to run rather than implying a clean sweep. The smaller model
did neither, and reached for language the evidence did not support: "not secure enough to pass a
routine review" where the stronger one said it could not tell what had written the log line.

On an unfamiliar fault, both said plainly that they did not recognise it, which is the behaviour
we most wanted. But the stronger model then gave four ranked candidate causes with how to confirm
each, and pointed out that a green health check is weak evidence for a fault that heals itself in
thirty seconds. The smaller model asked for the logs and stopped.

**So the guardrails survive going down a tier, and the value does not.** A weaker model gives you
a safe assistant that is not much help. That is the right way round, and it is still a real cost.

---

## And what we learned the hard way, before any of this

This estate has run local models on real hardware since 2026, and the record is worth reading
before you plan around one.

A 3B model, asked to extract a compass bearing from a sentence, **returned 90 degrees for every
direction**. Asked to pick out a word sitting in plain view in the same sentence, it returned
nothing, inconsistently between runs.

The fix was not a better prompt. The reliability-critical work moved **out of the model**:
bearings are converted in code, categories are whitelisted, distances are range-checked, and an
extraction the code cannot use files nothing rather than something wrong.

That is a much simpler job than operating an estate. Take it as the floor: if a 3B model cannot
reliably find a word it can see, do not put one in a position where its judgement is the thing
standing between an operator and a mistake.

---

## Matching model to autonomy

| Autonomy | What it does | Realistic model |
|---|---|---|
| **Observe** | Reads and reports. Cannot change anything | A capable mid-size model. Mistakes cost you a wrong summary, and the product's own health board is still there to check it against |
| **Propose** | Queues actions a person confirms | A strong model. The human gate catches errors, but a stream of poor proposals is worse than none: it trains an operator to approve without reading, which removes the gate |
| **Act** | Runs actions directly | **The best model you have access to.** Nothing downstream is checking its judgement |

The line worth holding: **do not pair a weak model with high autonomy**. The autonomy dial encodes
how much you trust the *setup*, and the model is half of that setup. An operator who would happily
set `act` for a frontier model should not set it for a 7B model on the same estate, and the
product cannot tell the difference for you.

---

## Can a NUC-class box do it?

Depends entirely on whether it has a GPU, and most do not.

**A 20-core, 62 GB machine with no GPU** &mdash; a fair description of a well-specified NUC &mdash;
has plenty of memory to *hold* a large model and not nearly enough throughput to *run* one at a
useful speed. CPU-only inference on that class of hardware is roughly:

- a 7B to 8B model quantised: usable, though not fast;
- a 30B class model: single-digit tokens per second, so an agent turn that produces a page of
  reasoning and a tool call takes minutes;
- a 70B model: technically loadable, practically not worth it.

An agent session is not one answer. It is read the brief, read the estate, reason, call a tool,
read the result, reason again. Multiply the per-turn cost by five or ten.

**So, honestly:**

- **NUC, no GPU, disconnected estate:** run an 8B class model at **observe** only. Expect summary
  and triage, not diagnosis. Keep the health board as the source of truth and treat the model as a
  convenience over it.
- **NUC plus a 24 GB GPU:** changes the answer completely. A 30B class model at usable speed is a
  genuine `propose`-grade assistant. This is the single most cost-effective upgrade if local
  inference matters to you.
- **Connected estate:** use a hosted frontier model. It is better at this than anything you will
  run on a mini PC, and the console's autonomy dial and audit trail are what constrain it, not the
  fact that it is local.
- **Disconnected estate that needs real capability:** this is the genuinely hard case. Either
  carry a GPU, or accept `observe` and keep a person in the loop for everything else. Do not close
  the gap by raising the autonomy of a model that cannot support it.

---

## If you are running local, do this

- **Give it one skill at a time.** Do not load the whole set into a small context. The skill
  descriptions exist so a model can pick the one job it is doing.
- **Prefer `observe` until you have watched it work.** Read what it says for a fortnight against
  what the console shows. You will know quickly whether it is adding anything.
- **Watch for the two failure signs specifically:** confident diagnoses it cannot support, and
  proposals that do not match what you asked. Both mean the model is under-powered for the job,
  not that the skills are wrong.
- **Keep the deterministic parts deterministic.** The health checker, the action scripts and the
  three validation layers do not depend on the model at all. That is the design, and it is why a
  weak model is a poor assistant here rather than a dangerous one.

---

## The thing that does not change

Whatever model you run, what an agent can actually *do* is decided by the console: the autonomy
you set, the actions that box allows, and three validation layers that do not trust the caller.

A better model gives you better judgement about what to do. It does not give it more permission.
That is the point of the design, and it is why running a modest model here is a reasonable choice
rather than a risky one &mdash; as long as you match the autonomy to it.
