"""The deployment manifest and pack store (Spec 005; ADR-003).

Assigning a pack to a deployment IS the sharing decision: the operator makes it once, at the
console (or `vd-ops pack` until the console page lands), and this module records it. The
manifest is one JSON file in the box's state dir; pack files live under
`state_dir/packs/<deployment>/`, never inside a vault. The sync surface serves exactly what
the manifest says a deployment carries; for a binary there is no marking to read, so the
operator's curation is the suitability decision, stated plainly in ADR-003.

Stdlib only; stores reload from disk per operation (the operator CLI and the server are
separate processes over one state dir, the Spec 002 lesson).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import List, Optional

KINDS = ("mission", "map")


def _write_atomic(path: str, payload: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, path)


class PackStore:
    """The manifest of which packs each deployment carries, plus the files themselves."""

    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self._manifest_path = os.path.join(state_dir, "manifest.json")
        self._packs_root = os.path.join(state_dir, "packs")

    def _load(self) -> dict:
        if not os.path.exists(self._manifest_path):
            return {}
        with open(self._manifest_path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, manifest: dict) -> None:
        _write_atomic(self._manifest_path, json.dumps(manifest, separators=(",", ":")))

    def add(self, deployment: str, src_path: str, kind: str, now: int) -> dict:
        """Assign a pack to a deployment: copy the file into the pack store and record it.
        Adding the same name again replaces the entry and the file (a pack update)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        name = os.path.basename(src_path)
        with open(src_path, "rb") as f:
            data = f.read()
        dest_dir = os.path.join(self._packs_root, deployment)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        entry = {"name": name, "kind": kind, "size": len(data),
                 "sha256": hashlib.sha256(data).hexdigest(), "added_at": int(now)}
        manifest = self._load()
        rows = [e for e in manifest.get(deployment, []) if e["name"] != name]
        rows.append(entry)
        rows.sort(key=lambda e: e["name"])
        manifest[deployment] = rows
        self._save(manifest)
        return entry

    def remove(self, deployment: str, name: str) -> bool:
        """Unassign a pack. The entry goes; the file goes with it (the manifest is the record
        of what is shared, and an unassigned file lying in the store would blur that)."""
        manifest = self._load()
        rows = manifest.get(deployment, [])
        keep = [e for e in rows if e["name"] != name]
        if len(keep) == len(rows):
            return False
        if keep:
            manifest[deployment] = keep
        else:
            manifest.pop(deployment, None)
        self._save(manifest)
        try:
            os.remove(os.path.join(self._packs_root, deployment, name))
        except OSError:
            pass
        return True

    def list_for(self, deployment: str) -> List[dict]:
        return list(self._load().get(deployment, []))

    def path_for(self, deployment: str, name: str) -> Optional[str]:
        """The file path IFF the manifest lists this pack for this deployment; the manifest,
        not the filesystem, is the authority on what is shared."""
        if os.path.basename(name) != name:
            return None
        if not any(e["name"] == name for e in self._load().get(deployment, [])):
            return None
        path = os.path.join(self._packs_root, deployment, name)
        return path if os.path.isfile(path) else None
