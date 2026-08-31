# Vendored from MilUX-Ltd/milux-vault-sync src/vaultsync/vaultapply.py at commit 42a89de
# (ADR-001: owned by the Vantage Deployed product from 30 Aug 2026; estate copy frozen).
"""Apply a received file to a vault without ever silently overwriting a local edit.

Both directions of the mesh need this: the phone applies files the box sends, and (the reverse
slice) the box applies files the phone sends. The phone runs a Kotlin mirror of this decision in
`BridgeService.applyIncoming`; this is the Python side, and the shared, tested statement of the
rule.

The rule, given the hash of what we last wrote for a path:
- no local file            -> write it (created).
- local == incoming        -> nothing to do (identical).
- local == last delivered  -> the user has not touched it since; safe to overwrite (updated).
- otherwise                -> the user edited it locally; keep their single version in the vault
                              and stage the incoming version in a quarantine directory OUTSIDE the
                              vault for resolution (conflict).

The vault must hold exactly one version of any note, so a reader or an LLM can never pick up the
wrong copy: conflicts are resolved out of the vault, and only the resolution is ever in it. This is
why the conflict copy goes to `conflict_dir` (a path outside the vault), never beside the note.
`now_ts` is injected (a timestamp string) so the quarantine name is deterministic in tests.
"""

from __future__ import annotations

import hashlib
import os


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def conflict_name(rel: str, now_ts: str, tag: str = "MESH") -> str:
    """The quarantine name for `rel`, preserving its folders: name.sync-conflict-<ts>-<tag>.ext.
    Used only under a conflict directory OUTSIDE the vault, never in the vault itself."""
    d, base = os.path.split(rel)
    stem, ext = os.path.splitext(base)
    name = "%s.sync-conflict-%s-%s%s" % (stem, now_ts, tag, ext)
    return os.path.join(d, name) if d else name


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def apply_incoming(vault: str, rel: str, incoming: bytes, last_hash: str | None,
                   now_ts: str, conflict_dir: str, tag: str = "MESH") -> tuple[str, str, str | None]:
    """Apply one incoming file. Returns (action, path_written, new_index_hash) where action is
    created / identical / updated / conflict, path_written is the vault-relative path for a normal
    write or the absolute quarantine path for a conflict, and new_index_hash is what the caller
    should record as "last delivered" for `rel` (unchanged on a conflict, since the local edit
    stands). On a conflict the vault keeps the single local version; the incoming version is
    written under `conflict_dir` (outside the vault), never beside the note."""
    out = os.path.join(vault, rel)
    inc_hash = sha256_hex(incoming)
    if not os.path.exists(out):
        _atomic_write(out, incoming)
        return ("created", rel, inc_hash)
    with open(out, "rb") as fh:
        local_hash = sha256_hex(fh.read())
    if local_hash == inc_hash:
        return ("identical", rel, inc_hash)
    if local_hash == last_hash:                       # unchanged since our last delivery
        _atomic_write(out, incoming)
        return ("updated", rel, inc_hash)
    qpath = os.path.join(conflict_dir, conflict_name(rel, now_ts, tag))  # outside the vault
    _atomic_write(qpath, incoming)
    return ("conflict", qpath, last_hash)
