# Vendored from MilUX-Ltd/milux-vault-sync src/vaultsync/deployment.py at commit 42a89de
# (ADR-001: owned by the Vantage Deployed product from 30 Aug 2026; estate copy frozen).
"""One definition of a deployment, for every surface.

A deployment IS its `operation:` label; a top-level vault folder is its HOME. The binding lives in
the folder's index note (`type: deployment-index`, directly in the folder): that note's
`operation:` is the label. A folder with no index note falls back to its own name as the label, so
an informal folder still works, and a mismatch between label and folder name is reported rather
than silently splitting the product in two (the graph and the boards match on the label, the file
tree and the sync paths live on the folder; before this module each surface chose for itself).

Everything that scopes to a deployment resolves through here: label -> folder for path scoping,
folder -> label for display and for frontmatter matching. Kotlin mirror: Deployment.kt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import graph as G

_SKIP = {".obsidian", ".git", ".stversions", ".stfolder", ".trash"}


@dataclass(frozen=True)
class Dep:
    folder: str        # the top-level folder name (the home; sync paths and file scoping)
    label: str         # the operation label (the identity; frontmatter and board matching)
    has_index: bool    # a deployment-index note was found
    mismatch: bool     # label and folder name differ: legal, but worth saying out loud


def _index_label(folder_path: str) -> str | None:
    """The `operation:` of the folder's deployment-index note, if one exists."""
    try:
        names = sorted(os.listdir(folder_path))
    except OSError:
        return None
    for name in names:
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(folder_path, name), encoding="utf-8", errors="replace") as fh:
                fm = G._frontmatter(fh.read())
        except OSError:
            continue
        if (fm.get("type", "") or "").strip().lower() == "deployment-index":
            label = G._op_label(fm.get("operation", ""))
            if label:
                return label
    return None


def resolve(vault: str) -> list[Dep]:
    """Every deployment in the vault, one entry per top-level folder, folder-name order."""
    out: list[Dep] = []
    try:
        entries = sorted(os.listdir(vault))
    except OSError:
        return out
    for name in entries:
        path = os.path.join(vault, name)
        if not os.path.isdir(path) or name.startswith(".") or name in _SKIP:
            continue
        label = _index_label(path)
        out.append(Dep(folder=name, label=label or name,
                       has_index=label is not None,
                       mismatch=label is not None and label != name))
    return out


def folder_for(vault: str, label: str) -> str | None:
    """The home folder for a deployment named by its label. A folder name is accepted directly,
    so callers rolled out at different times keep working."""
    if not label:
        return None
    deps = resolve(vault)
    for d in deps:
        if d.label == label:
            return d.folder
    for d in deps:
        if d.folder == label:
            return d.folder
    return None


def label_for(vault: str, folder: str) -> str:
    """The label of the deployment homed in `folder` (the folder name when nothing better)."""
    for d in resolve(vault):
        if d.folder == folder:
            return d.label
    return folder


def mismatches(vault: str) -> list[Dep]:
    """Deployments whose label and folder name differ; surfaces show these, never hide them."""
    return [d for d in resolve(vault) if d.mismatch]
