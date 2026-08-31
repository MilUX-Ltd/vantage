# Vendored from MilUX-Ltd/milux-vault-sync src/vaultsync/syncserver.py at commit 42a89de
# (ADR-001: owned by the Vantage Deployed product from 30 Aug 2026; estate copy frozen).
"""The authenticated IP sync surface (slice 5 core), the box side of owning sync (ADR-007, ADR-008).

Syncthing sees a folder and mirrors it blindly. Ours knows WHO is asking, from the device roll, and
serves only what that device's entry permits: its deployment's folder, and only files at or below its
clearance ceiling. Three operations:

    index(auth)            -> the files the device may pull, each with its sha256, scoped and filtered
    get_file(auth, rel)    -> one file's bytes, if in scope and within the ceiling
    push(auth, rel, body)  -> write a file the device sends, if in scope and within the ceiling

Every request is authenticated the same way (ADR-008 condition 1): the device signs the canonical
string (see `signing`), and `SyncCore.authorise` refuses it unless the signature verifies against the
enrolled key, the request is bound to the pinned channel, and the request counter is strictly greater
than the last one seen for that device (a monotonic seen-cache, so a captured request cannot be
replayed). A revoked device is refused because `Roll.active` returns None for it, while its key stays
on the roll so its past signatures still verify.

Scope and ceiling are enforced with the pieces already reviewed: `deployment.folder_for` resolves the
device's deployment to a top-level folder, a path-traversal guard keeps requests inside it, and the
classification guard (`may_traverse`) withholds anything above the ceiling, failing closed on a
marking it cannot read.

This module is pure request logic: it holds no socket and no key. The signature verification and the
public-key load import `cryptography` lazily, so the module and its non-crypto tests run anywhere; a
box that actually serves has the library. NOT YET wired into the running 8091 server, and the app's
IP transport is the other half of slice 5; see the build log.
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import classification as CL
from . import deployment as DEPID
from . import signing as SIG
from .roll import Record, Roll


@dataclass
class RequestAuth:
    """Everything needed to authenticate one request: the signed fields plus the signature."""
    method: str
    path_with_query: str
    body: bytes
    device_id: str
    counter: int
    challenge: str
    signature: bytes            # DER ECDSA
    channel_pin: str


class SyncDenied(Exception):
    """A request that failed authentication or authorisation. The reason is safe to log, not to
    return verbatim to the caller (it can name a path or a level)."""


class SyncCore:
    def __init__(self, vault: str, roll: Roll, channel_pin: str, seen: Optional[Dict[str, int]] = None):
        self.vault = vault
        self.roll = roll
        self.channel_pin = (channel_pin or "").lower()
        # per-device last-seen request counter; in-memory here, persisted by the running server.
        self.seen: Dict[str, int] = seen if seen is not None else {}

    # ---- authentication ---------------------------------------------------------------------
    def authorise(self, auth: RequestAuth) -> Record:
        """Return the active roll record for an authentic, fresh, in-channel request, or raise."""
        rec = self.roll.active(auth.device_id)
        if rec is None:
            raise SyncDenied("unknown or revoked device")
        if (auth.channel_pin or "").lower() != self.channel_pin:
            raise SyncDenied("wrong channel")
        if auth.counter <= self.seen.get(auth.device_id, -1):
            raise SyncDenied("stale counter (replay)")
        pub = self._load_key(rec.public_key_b64)
        ok = SIG.verify(
            pub, auth.signature,
            method=auth.method, path_with_query=auth.path_with_query, body=auth.body,
            device_id=auth.device_id, counter=auth.counter, challenge=auth.challenge,
            channel_pin=auth.channel_pin,
        )
        if not ok:
            raise SyncDenied("bad signature")
        # Commit the counter only after a good signature, so a failed attempt cannot burn a counter.
        self.seen[auth.device_id] = auth.counter
        return rec

    @staticmethod
    def _load_key(public_key_b64: str):
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        return load_der_public_key(base64.b64decode(public_key_b64))

    # ---- scope + ceiling --------------------------------------------------------------------
    # Product extension (30 Aug 2026, Matt): a device may carry SEVERAL deployments. The roll
    # field stays one string; multiple labels are joined with "|", split here, so tombstoned
    # single-scope records keep verifying unchanged.
    def _scope_labels(self, rec: Record) -> list:
        return [x.strip() for x in (rec.deployment_scope or "").split("|") if x.strip()]

    def _scope_folders(self, rec: Record) -> list:
        return [DEPID.folder_for(self.vault, lab) or lab for lab in self._scope_labels(rec)]

    def _safe_rel(self, rec: Record, rel: str) -> str:
        """Normalise a requested path and confirm it is inside ONE of the device's deployment
        folders. Rejects absolute paths and any `..` escape. Returns the normalised path."""
        if not rel or os.path.isabs(rel):
            raise SyncDenied("bad path")
        norm = os.path.normpath(rel)
        if norm.startswith("..") or norm == "." or "/../" in ("/" + norm + "/"):
            raise SyncDenied("path traversal")
        folders = self._scope_folders(rec)
        if not folders:
            raise SyncDenied("device has no deployment scope")
        for folder in folders:
            if norm == folder or norm.startswith(folder + os.sep):
                return norm
        raise SyncDenied("out of scope")

    def _within_ceiling(self, rec: Record, text: str) -> bool:
        mark = CL.read_marking_safe(text)
        bearer = CL.Bearer(name=f"EUD:{rec.label}", ceiling=rec.clearance_ceiling)
        return CL.may_traverse(mark, bearer).allowed

    # ---- operations -------------------------------------------------------------------------
    def index(self, auth: RequestAuth) -> List[dict]:
        """The files the device may pull: markdown inside its deployment folder, at or below its
        ceiling, each with its sha256 so the device can tell what changed."""
        rec = self.authorise(auth)
        out: List[dict] = []
        seen_paths = set()
        for folder in self._scope_folders(rec):
            base = os.path.join(self.vault, folder)
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in files:
                    if not name.endswith(".md"):
                        continue
                    full = os.path.join(root, name)
                    try:
                        text = open(full, encoding="utf-8").read()
                    except OSError:
                        continue
                    if not self._within_ceiling(rec, text):
                        continue
                    rel = os.path.relpath(full, self.vault)
                    if rel in seen_paths:
                        continue
                    seen_paths.add(rel)
                    out.append({"path": rel,
                                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        out.sort(key=lambda e: e["path"])
        return out

    def get_file(self, auth: RequestAuth, rel: str) -> bytes:
        """One file's bytes, if in scope and within the ceiling."""
        rec = self.authorise(auth)
        norm = self._safe_rel(rec, rel)
        full = os.path.join(self.vault, norm)
        if not os.path.isfile(full):
            raise SyncDenied("no such file")
        text = open(full, encoding="utf-8", errors="replace").read()
        if not self._within_ceiling(rec, text):
            raise SyncDenied("above ceiling")
        return text.encode("utf-8")

    def push(self, auth: RequestAuth, rel: str, body: bytes) -> dict:
        """Write a file the device sends, into its own deployment folder. The pushed note's marking
        must be at or below the device's ceiling (fail closed), so a device cannot inject a note
        claiming a level it is not cleared for."""
        rec = self.authorise(auth)
        norm = self._safe_rel(rec, rel)
        text = body.decode("utf-8", errors="replace")
        if not self._within_ceiling(rec, text):
            raise SyncDenied("push above ceiling")
        full = os.path.join(self.vault, norm)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".sync.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, full)
        return {"path": norm, "bytes": len(body), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


# ---- product extension (Vantage Deployed, Spec 004) ------------------------------------------
# The estate's push above overwrites unconditionally, because the estate handled conflicts on
# the mesh path. The product routes every push through the vaultapply decision table instead:
# the device states the hash its edit was based on; the box fast-forwards when its copy
# matches, and otherwise stages the incoming version OUTSIDE the vault and reports the
# conflict. Every overwrite first retains the previous version in a history directory outside
# the vault, so no push destroys evidence. The estate-frozen push stays for its tests.

def push_safe(core: SyncCore, auth: RequestAuth, rel: str, body: bytes, base: str,
              quarantine_dir: str, history_dir: str, now_ts: str) -> dict:
    """Conflict-safe push (Spec 004 AC3-AC5). Returns {action, path, sha256}; action is
    created / identical / updated / conflict. Raises SyncDenied as the operations above do."""
    from . import vaultapply

    rec = core.authorise(auth)
    norm = core._safe_rel(rec, rel)
    text = body.decode("utf-8", errors="replace")
    if not core._within_ceiling(rec, text):
        raise SyncDenied("push above ceiling")
    full = os.path.join(core.vault, norm)
    previous = None
    if os.path.isfile(full):
        with open(full, "rb") as fh:
            previous = fh.read()
    last_hash = None if base in ("", "new") else base
    action, path_written, _ = vaultapply.apply_incoming(
        core.vault, norm, body, last_hash, now_ts, quarantine_dir, tag=f"EUD-{rec.label}")
    if action == "updated" and previous is not None:
        keep = os.path.join(history_dir, now_ts, norm)
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        with open(keep, "wb") as fh:
            fh.write(previous)
    return {"action": action, "path": path_written,
            "sha256": hashlib.sha256(body).hexdigest()}


def delete_safe(core: SyncCore, auth: RequestAuth, rel: str, base: str,
                history_dir: str, now_ts: str) -> dict:
    """Conflict-safe delete from a device (device->box deletion). The device states the hash
    its deletion was based on; the box deletes only if its copy still matches, retaining the
    removed content in history first so nothing is destroyed. Returns {action}: deleted /
    already-gone / conflict. A conflict (the box changed the note since the device last saw
    it) refuses the delete: the box's newer version wins and the device re-pulls it."""
    rec = core.authorise(auth)
    norm = core._safe_rel(rec, rel)
    full = os.path.join(core.vault, norm)
    if not os.path.isfile(full):
        return {"action": "already-gone", "path": norm}
    with open(full, "rb") as fh:
        current = fh.read()
    cur_hash = hashlib.sha256(current).hexdigest()
    if base not in ("", "force") and cur_hash != base:
        return {"action": "conflict", "path": norm}   # the box has a newer version; keep it
    keep = os.path.join(history_dir, now_ts, norm)
    os.makedirs(os.path.dirname(keep), exist_ok=True)
    with open(keep, "wb") as fh:
        fh.write(current)
    os.remove(full)
    return {"action": "deleted", "path": norm}
