# Vendored from MilUX-Ltd/milux-vault-sync src/vaultsync/classification.py at commit 42a89de
# (ADR-001: owned by the Vantage Deployed product from 30 Aug 2026; estate copy frozen).
"""Security classification as an enforced control, not just a label.

Grounded in the UK Government Security Classifications (GSC): the tiers are OFFICIAL,
SECRET and TOP SECRET, with OFFICIAL-SENSITIVE a handling caveat on OFFICIAL that in
practice raises how a piece of information is handled and moved. Coalition equivalents
(NATO markings) and releasability caveats are represented so the same note can be judged
for a specific destination. The authoritative detail is the Cabinet Office GSC policy and
the defence security JSPs (JSP 440, JSP 604); this module encodes the handling logic, not
the policy, and a defence security review validates it before any real use.

The point of putting this in code: a note's marking decides what may cross which bearer
and reach which recipient. A degraded open bearer (the Meshtastic proof) is authorised
only to a ceiling; an accredited tactical bearer carries more; a cross-domain guard (the
role 2iC TRUST plays) moves between domains under control. The guard here refuses to move
information above a bearer's ceiling or outside its releasability, and says why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Ordered low to high. OFFICIAL-SENSITIVE sits above OFFICIAL for movement decisions,
# which is the operational treatment of the SENSITIVE caveat even though the GSC tier is
# still OFFICIAL. NATO equivalents are mapped onto the same ladder for coalition work.
LADDER = ["OFFICIAL", "OFFICIAL-SENSITIVE", "SECRET", "TOP SECRET"]

NATO_EQUIV = {
    "NATO UNCLASSIFIED": "OFFICIAL",
    "NATO RESTRICTED": "OFFICIAL-SENSITIVE",
    "NATO CONFIDENTIAL": "SECRET",
    "NATO SECRET": "SECRET",
    "COSMIC TOP SECRET": "TOP SECRET",
}


class ClassificationError(ValueError):
    """A marking that cannot be understood. Fail closed: treat as the highest tier."""


@dataclass(frozen=True)
class Marking:
    level: str                       # a LADDER value
    releasable_to: frozenset = field(default_factory=frozenset)  # nation trigraphs, empty = originator only
    caveats: frozenset = field(default_factory=frozenset)        # e.g. UK EYES ONLY, LOCSEN
    raw: str = ""

    def rank(self) -> int:
        return LADDER.index(self.level)


def normalise(level: str) -> str:
    s = level.strip().upper()
    if s in LADDER:
        return s
    if s in NATO_EQUIV:
        return NATO_EQUIV[s]
    if s.replace(" ", "-") == "OFFICIAL-SENSITIVE":
        return "OFFICIAL-SENSITIVE"
    raise ClassificationError(f"unknown classification: {level!r}")


def leading_level(value: str) -> str:
    """Pull the classification tier from the start of a marking string, ignoring any
    trailing REL TO / caveats. Longest known marking wins (OFFICIAL-SENSITIVE before
    OFFICIAL). Fails closed to TOP SECRET on nothing recognisable."""
    s = value.strip().upper()
    candidates = sorted(LADDER + list(NATO_EQUIV), key=len, reverse=True)
    for c in candidates:
        if s == c or s.startswith(c + " ") or s.startswith(c + ",") or s.startswith(c + "/"):
            return normalise(c)
    # "OFFICIAL SENSITIVE" with a space
    if s.startswith("OFFICIAL SENSITIVE"):
        return "OFFICIAL-SENSITIVE"
    raise ClassificationError(f"no leading classification in {value!r}")


_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_REL = re.compile(r"REL\s+TO\s+([A-Z/ ,]+)", re.IGNORECASE)


def read_marking(note_text: str) -> Marking:
    """Extract a note's marking from its frontmatter. A note with no classification
    field is treated as OFFICIAL (the GSC default for routine business), which is the
    safe assumption for a vault that is OFFICIAL by baseline; raise the baseline per
    deployment if that is wrong for the operation."""
    fm = _FM.match(note_text)
    block = fm.group(1) if fm else ""
    level = "OFFICIAL"
    caveats: set[str] = set()
    rel: set[str] = set()
    for line in block.splitlines():
        k, _, v = line.partition(":")
        key = k.strip().lower()
        val = v.strip().strip("[]").strip()
        if key == "classification" and val:
            level = leading_level(val.split("//")[0])
            m = _REL.search(v)
            if m:
                rel = {t.strip().upper() for t in re.split(r"[/, ]+", m.group(1)) if t.strip()}
        elif key in ("caveat", "caveats", "handling") and val:
            caveats |= {c.strip().upper() for c in re.split(r"[;,]", val) if c.strip()}
        elif key in ("releasable_to", "rel_to", "releasability") and val:
            rel |= {t.strip().upper() for t in re.split(r"[/, ]+", val) if t.strip()}
    return Marking(level, frozenset(rel), frozenset(caveats), block)


@dataclass(frozen=True)
class Bearer:
    """A link's accreditation: the highest it may carry, and who is on it."""
    name: str
    ceiling: str                     # a LADDER value
    audience: frozenset = field(default_factory=frozenset)  # nation trigraphs reachable on this bearer

    def rank(self) -> int:
        return LADDER.index(self.ceiling)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def may_traverse(mark: Marking, bearer: Bearer) -> Decision:
    """May a note with this marking cross this bearer? Fail closed."""
    if mark.rank() > bearer.rank():
        return Decision(False, f"{mark.level} exceeds {bearer.name} ceiling {bearer.ceiling}")
    if "UK EYES ONLY" in mark.caveats and bearer.audience and bearer.audience != frozenset({"GBR"}):
        return Decision(False, f"UK EYES ONLY may not cross {bearer.name} (audience {sorted(bearer.audience)})")
    if mark.releasable_to and bearer.audience and not mark.releasable_to.issuperset(bearer.audience):
        outside = sorted(bearer.audience - mark.releasable_to)
        return Decision(False, f"not releasable to {outside} on {bearer.name}")
    return Decision(True, f"{mark.level} within {bearer.name} ceiling {bearer.ceiling}")


def read_marking_safe(note_text: str) -> Marking:
    """As read_marking, but never raises: an unreadable marking becomes TOP SECRET so a
    malformed note is withheld rather than leaked (fail closed)."""
    try:
        return read_marking(note_text)
    except ClassificationError:
        return Marking("TOP SECRET", raw="unreadable-marking")
