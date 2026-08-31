# Vendored from MilUX-Ltd/milux-vault-sync src/vaultsync/graph.py at commit 42a89de
# (ADR-001: owned by the Vantage Deployed product from 30 Aug 2026; estate copy frozen).
"""Build a knowledge graph from the vault: information objects as nodes, links as edges.

Objects follow a layered ontology (see docs/research/intelligence-ontology.md). An **entity**
is a durable thing (person, organisation, place, facility, materiel, event); a **product** is
a document about entities (orders, intelligence products, reports, plans). Colour carries
meaning: an entity is coloured by its **affiliation** (hostile red, neutral civil ochre,
friendly olive, terrain green) so the red force and local force read apart from the friendly
force; a product is coloured by its kind. Edges come from Obsidian wikilinks, typed relational
frontmatter (entity-to-entity and product-to-entity), and operation membership: every object
carries `operation:` and stars off the operation's index. Pure stdlib so the box and, ported
to Kotlin, the phone build the same graph offline.
"""

from __future__ import annotations

import os
import re

SKIP_DIRS = {".obsidian", ".git", ".stversions", ".stfolder", ".trash"}
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Typed relational frontmatter fields. Each may hold one or more [[wikilinks]] to related
# objects. entity-to-entity relations (member_of, commands, located_at, ...) and the existing
# product relations, all drawn as edges. `operation` is the membership hub, handled separately.
_REF_KEYS = ("refs", "references", "refers_to", "amends", "supersedes",
             "parent", "part_of", "task_org", "objective", "about",
             "location", "covers", "from", "net", "nets", "owning", "unit",
             "member_of", "commands", "commanded_by", "located_at", "owns", "owned_by",
             "operates", "aligned_with", "opposes", "reports_to", "adjacent_to",
             "answers", "answered_by", "indicators", "assigned_to", "order")

# Entity kinds: durable-identity objects. type or `entity:` set to one of these makes a node
# an entity, coloured by affiliation and kind rather than by document type.
ENTITY_TYPES = {"person", "organisation", "place", "facility", "materiel", "event"}

# Product/document type -> family. A non-entity note colours by its kind, as before.
PRODUCT_FAMILY = {
    "orders": ["opord", "frago", "order", "intent", "conops", "task"],
    "intelligence": ["intsum", "intrep", "ipb", "osint", "threat-assessment",
                     "pattern-of-life", "human-terrain-analysis", "atmospherics", "ground-picture"],
    "reporting": ["sitrep", "contact-report", "sigevent", "report", "reporting-triage",
                  "tak-chat-log", "log"],
    "comms": ["pace"],
    "command": ["deployment-index", "operation", "decision-log", "battle-rhythm", "orbat",
                "ccir", "pir", "ffir", "eei"],
}

# Family -> colour. Entity families (hostile/human-terrain/friendly/places/materiel) and
# product families share one palette so the legend is one list.
FAMILY_COLOUR = {
    "orders": "#C0503B",         # terracotta
    "intelligence": "#7B5EA7",   # violet
    "hostile": "#B23A48",        # red force
    "human-terrain": "#C08A2E",  # local force, ochre
    "friendly": "#B5B171",       # friendly force, olive gold
    "reporting": "#586F7C",      # blue-grey
    "places": "#4E7A51",         # terrain, green
    "materiel": "#6E7F94",       # steel
    "comms": "#3E8E8E",          # teal
    "command": "#113308",        # deep green
    "other": "#9AA0A6",          # grey
}
_LEGEND_ORDER = ["orders", "intelligence", "hostile", "human-terrain", "friendly",
                 "reporting", "places", "materiel", "comms", "command", "other"]
OTHER_COLOUR = FAMILY_COLOUR["other"]

# Legacy type -> family, so a note not yet migrated to the entity model still colours
# consistently (the fields-first resolver falls back to this).
_LEGACY_FAMILY = {
    "threat": "hostile", "threat-unit": "hostile", "callsign": "friendly",
    "key-leader": "human-terrain", "population": "human-terrain", "settlement": "places",
    "named-area": "places", "location": "places", "human-terrain": "intelligence",
}

# Every type that is an information object in its own right, so an unlabelled one is still
# shown (a gap in plain sight) rather than dropped as scratch.
_OBJECT_TYPES = (ENTITY_TYPES
                 | {t for types in PRODUCT_FAMILY.values() for t in types}
                 | set(_LEGACY_FAMILY))


def _list(v: str) -> list:
    """Parse a frontmatter list value like [a, b] or a into lowercase members."""
    v = (v or "").strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [p.strip().strip('"').strip("'").lower() for p in v.split(",") if p.strip()]


def _resolve_family(fm: dict) -> str:
    """The colour family for a note. Entities colour by affiliation and kind; products by
    their document kind; legacy notes fall back to the old type map."""
    typ = (fm.get("type", "") or "").strip().lower()
    ent = (fm.get("entity", "") or "").strip().lower()
    if not ent and typ in ENTITY_TYPES:
        ent = typ
    aff = (fm.get("affiliation", "") or "").strip().lower()
    if ent:
        if ent in ("place", "facility"):
            return "places"            # terrain is terrain, whatever sits on it
        if aff == "hostile":
            return "hostile"
        if aff in ("neutral", "unknown"):
            return "human-terrain" if ent in ("person", "organisation") else \
                   ("materiel" if ent == "materiel" else "other")
        # friendly or unset
        if ent in ("person", "organisation"):
            return "friendly"
        if ent == "materiel":
            return "materiel"
        return "other"
    for family, types in PRODUCT_FAMILY.items():
        if typ in types:
            return family
    return _LEGACY_FAMILY.get(typ, "other")


def _frontmatter(text: str) -> dict:
    m = _FM.match(text)
    fm: dict[str, str] = {}
    if not m:
        return fm
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if k.strip():
            fm[k.strip().lower()] = v.strip()
    return fm


def _title(fm: dict, rel: str) -> str:
    t = fm.get("title", "").strip().strip('"')
    if t:
        return t
    return os.path.splitext(os.path.basename(rel))[0]


def _op_label(raw: str) -> str:
    v = (raw or "").strip().strip('"').strip("'")
    m = _WIKILINK.search(v)
    if m:
        v = m.group(1)
    return v.strip()


def build_graph(vault: str) -> dict:
    """Return {nodes, links, legend} for the vault. Node id is the note basename. Links come
    from wikilinks, typed relational frontmatter, and operation membership."""
    notes: dict[str, dict] = {}
    raw_links: list[tuple[str, str]] = []
    op_of: dict[str, str] = {}
    index_for: dict[str, str] = {}

    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(root, name), vault)
            try:
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            node_id = os.path.splitext(name)[0]
            fm = _frontmatter(text)
            ntype = (fm.get("type", "") or "").strip().lower()
            entity = (fm.get("entity", "") or "").strip().lower()
            op = _op_label(fm.get("operation", ""))

            if ntype not in _OBJECT_TYPES and entity not in ENTITY_TYPES and not op:
                continue  # untyped scratch, no operation: not an information object

            family = _resolve_family(fm)
            notes[node_id] = {
                "id": node_id,
                "label": _title(fm, rel),
                "type": ntype or "note",
                "entity": entity or (ntype if ntype in ENTITY_TYPES else ""),
                "affiliation": (fm.get("affiliation", "") or "").strip().lower(),
                "roles": _list(fm.get("roles", "")),
                "family": family,
                "colour": FAMILY_COLOUR.get(family, OTHER_COLOUR),
                "classification": (fm.get("classification", "OFFICIAL").split()[0] or "OFFICIAL"),
                "operation": op,
                "unlabelled": not op,
                "path": rel,
            }
            if op:
                op_of[node_id] = op
                if ntype == "deployment-index":
                    index_for.setdefault(op, node_id)
            for m in _WIKILINK.finditer(text):
                raw_links.append((node_id, m.group(1).strip()))
            for key in _REF_KEYS:
                if key in fm:
                    for m in _WIKILINK.finditer(fm[key]):
                        raw_links.append((node_id, m.group(1).strip()))

    for op in sorted(set(op_of.values())):
        hub = index_for.get(op)
        if hub is None:
            hub = f"operation:{op}"
            if hub not in notes:
                notes[hub] = {"id": hub, "label": op, "type": "operation", "entity": "",
                              "affiliation": "", "roles": [], "family": "command",
                              "colour": FAMILY_COLOUR["command"], "classification": "OFFICIAL",
                              "operation": op, "unlabelled": False, "path": ""}
            index_for[op] = hub
    for nid, op in op_of.items():
        hub = index_for[op]
        if hub != nid:
            raw_links.append((nid, hub))

    links = []
    seen = set()
    for src, target in raw_links:
        tid = target if target in notes else os.path.splitext(os.path.basename(target))[0]
        if tid in notes and tid != src and (src, tid) not in seen:
            seen.add((src, tid))
            links.append({"source": src, "target": tid})

    deg: dict[str, int] = {}
    for l in links:
        deg[l["source"]] = deg.get(l["source"], 0) + 1
        deg[l["target"]] = deg.get(l["target"], 0) + 1
    for nid, n in notes.items():
        n["degree"] = deg.get(nid, 0)

    present = {n["family"] for n in notes.values()}
    legend = [{"family": f, "colour": FAMILY_COLOUR[f]} for f in _LEGEND_ORDER if f in present]
    return {"nodes": list(notes.values()), "links": links, "legend": legend}
