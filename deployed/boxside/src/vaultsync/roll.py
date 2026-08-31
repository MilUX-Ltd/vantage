# Vendored verbatim from MilUX-Ltd/milux-vault-sync src/vaultsync/roll.py at commit 42a89de
# (ADR-001: owned by the Vantage Deployed product from 30 Aug 2026; estate copy frozen).
"""The device roll (ADR-008): FIRMBASE's record of which devices may sync, and with what scope.

FIRMBASE holds the roll of record; EDGE holds a replica it verifies before serving. Each record
binds a device's public key to a deployment scope and a clearance ceiling: those two fields are what
turn the classification guard into an enforced boundary, not a suggestion.

Two safety properties are built into the model here (the ECDSA signing of the whole document, which
needs `cryptography`, is layered on top and imports lazily):

  - **Revocation is a tombstone, never a deletion (condition 3).** Revoking a device sets `revoked_at`
    and a reason and RETAINS its public key, so signatures it made before revocation still verify and
    no evidence is destroyed. A revoked device is refused for NEW requests (`active()` returns None)
    but its key is still there for `record()`.

  - **The roll is versioned and anti-rollback (condition 2).** Every generation carries a monotonic
    version and a generation time; `document()` is the canonical bytes FIRMBASE signs and EDGE
    verifies. `accepts()` refuses a roll whose version is not strictly greater than the one already
    held, so a captured older roll cannot be replayed to re-admit a revoked device.

Staleness (condition 3): EDGE checks `is_stale()` against a maximum age and degrades (to read-only or
lowest scope) rather than trusting a roll it can no longer confirm is current.

Who may write the roll at FIRMBASE is the operator's curation path, never the resident agent under
the shared unix user; that boundary is enforced at the file/permission layer, this module only
models the data.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class Record:
    device_id: str
    public_key_b64: str          # SubjectPublicKeyInfo DER, base64, as the EUD presents it
    label: str                   # human label for the device
    holder: str                  # who holds it
    deployment_scope: str        # the deployment (operation label) this device may sync
    clearance_ceiling: str       # the highest classification this device may receive
    enrolment_provenance: str    # how it was enrolled (e.g. "qr:FIRMBASE:2026-08-24")
    chain_id: str                # identity chain, for re-enrolment continuity
    anchor: str                  # the roll anchor at enrolment time
    enrolled_at: int             # unix seconds
    revoked_at: Optional[int] = None
    revoked_reason: Optional[str] = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


class Roll:
    """FIRMBASE's roll of record. Construct empty, add/revoke, then `document()` + sign for EDGE."""

    def __init__(self, version: int = 1, generated_at: int = 0):
        self.version = int(version)
        self.generated_at = int(generated_at)
        self._records: Dict[str, Record] = {}

    # ---- membership -------------------------------------------------------------------------
    def add(self, rec: Record) -> None:
        self._records[rec.device_id] = rec

    def record(self, device_id: str) -> Optional[Record]:
        """The record for a device, revoked or not, so past signatures can still be verified."""
        return self._records.get(device_id)

    def active(self, device_id: str) -> Optional[Record]:
        """The record IFF the device may make new requests: present and not revoked."""
        r = self._records.get(device_id)
        return r if (r is not None and not r.revoked) else None

    def revoke(self, device_id: str, reason: str, now: int) -> bool:
        """Tombstone a device: keep the row and its key, mark it revoked. Returns False if unknown."""
        r = self._records.get(device_id)
        if r is None:
            return False
        if not r.revoked:
            r.revoked_at = int(now)
            r.revoked_reason = reason
        return True

    def all_records(self) -> List[Record]:
        return [self._records[k] for k in sorted(self._records)]

    # ---- versioning + integrity -------------------------------------------------------------
    def document(self) -> bytes:
        """Canonical bytes of the whole roll, for signing and hashing. Deterministic: keys sorted,
        records in device-id order, compact separators."""
        payload = {
            "v": self.version,
            "generated_at": self.generated_at,
            "records": [asdict(r) for r in self.all_records()],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def content_hash(self) -> str:
        return hashlib.sha256(self.document()).hexdigest()

    def bumped(self, now: int) -> "Roll":
        """A new generation: version + 1, stamped now, same records. FIRMBASE calls this on a change."""
        nxt = Roll(version=self.version + 1, generated_at=int(now))
        nxt._records = {k: Record(**asdict(v)) for k, v in self._records.items()}
        return nxt

    def accepts(self, incoming: "Roll") -> bool:
        """Anti-rollback: EDGE adopts an incoming roll only if its version is strictly greater."""
        return incoming.version > self.version

    def is_stale(self, now: int, max_age_seconds: int) -> bool:
        """True when the roll is older than the bound; EDGE degrades rather than trusting it."""
        return (int(now) - self.generated_at) > int(max_age_seconds)

    # ---- serialisation ----------------------------------------------------------------------
    def to_json(self) -> str:
        return self.document().decode("utf-8")

    @classmethod
    def from_json(cls, text: str) -> "Roll":
        obj = json.loads(text)
        roll = cls(version=obj.get("v", 1), generated_at=obj.get("generated_at", 0))
        for r in obj.get("records", []):
            roll.add(Record(**r))
        return roll

    # ---- signed document (needs cryptography; box only) -------------------------------------
    def sign_document(self, private_key) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        return private_key.sign(self.document(), ec.ECDSA(hashes.SHA256()))

    def verify_document(self, public_key, signature: bytes) -> bool:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        try:
            public_key.verify(signature, self.document(), ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False
