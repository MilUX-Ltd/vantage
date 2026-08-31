"""QR enrolment for Vantage Deployed (Spec 001; ADR-008 condition 4).

The console mints a single-use, minutes-lived token pre-bound to a named device, holder,
deployment scope and clearance ceiling, and renders it as a QR beside the box's sync URL and
channel fingerprint. The device presents the token with its public key; that creates a PENDING
enrolment, never a roll entry. Only the operator's confirmation, made after checking the key
fingerprint against the one the device shows, writes the record into the roll and bumps the
roll generation. Rejection discards the pending record; revocation later is the roll's own
tombstone.

Stdlib only. Stores are small JSON files written atomically; the store directory is the box's
state dir, never inside the vault.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from . import signing
from .roll import Record, Roll

QR_TYPE = "vd-enrol"
QR_VERSION = 1
DEFAULT_TTL_S = 600  # minutes-lived, per ADR-008 condition 4


@dataclass
class Token:
    value: str
    box: str
    device_label: str
    holder: str
    deployment_scope: str
    clearance_ceiling: str
    minted_at: int
    expires_at: int
    used_at: Optional[int] = None


@dataclass
class Pending:
    fingerprint: str            # sha256 of the SPKI DER; becomes the device id on the roll
    device_label: str
    holder: str
    deployment_scope: str
    clearance_ceiling: str
    public_key_b64: str
    box: str
    enrolled_at: int


def _write_atomic(path: str, payload: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, path)


class TokenStore:
    """Minted enrolment tokens, persisted so a console restart cannot resurrect a used one."""

    def __init__(self, path: str):
        self.path = path
        self._tokens: Dict[str, Token] = {}
        self._load()

    def _load(self) -> None:
        """Re-read from disk. The operator CLI and the server are separate processes over one
        state dir, so every operation starts from the file, never a stale in-memory copy."""
        self._tokens = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for obj in json.load(f):
                    self._tokens[obj["value"]] = Token(**obj)

    def _save(self) -> None:
        _write_atomic(self.path, json.dumps(
            [asdict(t) for t in self._tokens.values()], separators=(",", ":")))

    def mint(self, box: str, device_label: str, holder: str, deployment_scope: str,
             clearance_ceiling: str, now: int, ttl_s: int = DEFAULT_TTL_S) -> Token:
        self._load()
        tok = Token(
            value=secrets.token_urlsafe(32), box=box, device_label=device_label,
            holder=holder, deployment_scope=deployment_scope,
            clearance_ceiling=clearance_ceiling, minted_at=int(now),
            expires_at=int(now) + int(ttl_s),
        )
        self._tokens[tok.value] = tok
        self._save()
        return tok

    def consume(self, value: str, now: int) -> Tuple[Optional[Token], str]:
        """Single use: the first good presentation wins, everything after is refused with the
        honest reason. A used token stays 'used' even after its expiry has also passed."""
        self._load()
        tok = self._tokens.get(value)
        if tok is None:
            return None, "unknown"
        if tok.used_at is not None:
            return None, "used"
        if int(now) > tok.expires_at:
            return None, "expired"
        tok.used_at = int(now)
        self._save()
        return tok, "ok"


def pairing_code(fingerprint: str) -> str:
    """Six decimal digits derived from the fingerprint, for the operator read-back (ADR-002
    decision 4). A 64-hex fingerprint is not a human ceremony; the short code is (the
    Bluetooth numeric-comparison and Signal safety-number pattern). The code only has to
    distinguish WHICH device consumed the single-use, minutes-lived token: a raced token
    shows itself anyway, because the real holder's own enrolment is then refused as used,
    which tells the operator to reject the pending impostor. The full fingerprint stays on
    the roll for the record. Pinned cross-language with the app's PairingCode."""
    n = int(hashlib.sha256(fingerprint.encode("ascii")).hexdigest(), 16) % 1_000_000
    return f"{n:06d}"


def parse_qr(text: str) -> Optional[dict]:
    """The payload IFF it is a well-formed vd-enrol QR of a version we speak; None otherwise."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != QR_VERSION or obj.get("t") != QR_TYPE:
        return None
    if not all(k in obj for k in ("box", "url", "tok", "pin", "exp")):
        return None
    return obj


class Enrolment:
    """The box's enrolment surface: mint QRs, take presentations into pending, confirm to roll."""

    def __init__(self, store_dir: str):
        os.makedirs(store_dir, exist_ok=True)
        self.tokens = TokenStore(os.path.join(store_dir, "tokens.json"))
        self._pending_path = os.path.join(store_dir, "pending.json")
        self._pending: Dict[str, Pending] = {}
        self._load_pending()

    def _load_pending(self) -> None:
        """Re-read from disk, for the same cross-process reason as TokenStore._load."""
        self._pending = {}
        if os.path.exists(self._pending_path):
            with open(self._pending_path, encoding="utf-8") as f:
                for obj in json.load(f):
                    self._pending[obj["fingerprint"]] = Pending(**obj)

    def _save_pending(self) -> None:
        _write_atomic(self._pending_path, json.dumps(
            [asdict(p) for p in self._pending.values()], separators=(",", ":")))

    # ---- console side -------------------------------------------------------------------
    def mint_qr(self, box: str, base_url: str, channel_pin: str, device_label: str,
                holder: str, deployment_scope: str, clearance_ceiling: str, now: int,
                ttl_s: int = DEFAULT_TTL_S) -> str:
        tok = self.tokens.mint(box=box, device_label=device_label, holder=holder,
                               deployment_scope=deployment_scope,
                               clearance_ceiling=clearance_ceiling, now=now, ttl_s=ttl_s)
        return json.dumps({
            "v": QR_VERSION, "t": QR_TYPE, "box": box, "url": base_url,
            "tok": tok.value, "pin": (channel_pin or "").lower(), "exp": tok.expires_at,
        }, separators=(",", ":"))

    # ---- device side --------------------------------------------------------------------
    def enrol(self, token_value: str, spki_der_b64: str, now: int) -> Tuple[Optional[Pending], str]:
        """A device presents its token and public key. The key is validated BEFORE the token is
        consumed, so a malformed request cannot burn the single use (the same rule as the
        request counter committing only after a good signature)."""
        try:
            der = base64.b64decode(spki_der_b64, validate=True)
            if not der:
                return None, "bad-key"
        except (ValueError, TypeError):
            return None, "bad-key"
        self._load_pending()
        tok, why = self.tokens.consume(token_value, now)
        if tok is None:
            return None, why
        pending = Pending(
            fingerprint=signing.device_id(der), device_label=tok.device_label,
            holder=tok.holder, deployment_scope=tok.deployment_scope,
            clearance_ceiling=tok.clearance_ceiling, public_key_b64=spki_der_b64,
            box=tok.box, enrolled_at=int(now),
        )
        self._pending[pending.fingerprint] = pending
        self._save_pending()
        return pending, "ok"

    # ---- operator side (condition 4's human check) --------------------------------------
    def pending(self) -> List[Pending]:
        self._load_pending()
        return [self._pending[k] for k in sorted(self._pending)]

    def confirm(self, fingerprint: str, roll: Roll, now: int, box: str) -> Roll:
        """The operator has checked the fingerprint the device shows. Writes the record into a
        new roll generation (anti-rollback: the old generation is no longer accepted)."""
        self._load_pending()
        p = self._pending.pop(fingerprint)
        self._save_pending()
        day = time.strftime("%Y-%m-%d", time.gmtime(int(now)))
        new = roll.bumped(now)
        new.add(Record(
            device_id=p.fingerprint, public_key_b64=p.public_key_b64, label=p.device_label,
            holder=p.holder, deployment_scope=p.deployment_scope,
            clearance_ceiling=p.clearance_ceiling,
            enrolment_provenance=f"qr:{box}:{day}",
            chain_id=hashlib.sha256(f"{p.fingerprint}:{int(now)}".encode()).hexdigest()[:16],
            anchor=roll.content_hash(), enrolled_at=int(now),
        ))
        return new

    def reject(self, fingerprint: str) -> bool:
        self._load_pending()
        if fingerprint not in self._pending:
            return False
        del self._pending[fingerprint]
        self._save_pending()
        return True
