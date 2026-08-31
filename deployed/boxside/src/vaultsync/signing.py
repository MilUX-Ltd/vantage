# Vendored verbatim from MilUX-Ltd/milux-vault-sync src/vaultsync/signing.py at commit 42a89de
# (ADR-001: owned by the Vantage Deployed product from 30 Aug 2026; estate copy frozen).
"""Canonical request signing for the owned IP bearer (ADR-008, condition 1).

Every sync request is signed by the EUD's per-device key and verified by the box against the
enrolled public key in the roll; the box signs what it serves so the EUD verifies the other way.
The signature is over a CANONICAL STRING, serialised here byte-for-byte the same as the Kotlin
`Canonical` in milux-eud-shared:identity, so the two sides never disagree about what was signed.

The signed string (UTF-8, newline-joined, no trailing newline) is, in order:

    milux-sync/1                     protocol tag and version
    <METHOD>                         HTTP method, upper case
    <PATH?QUERY>                     path INCLUDING the query string, exactly as sent
    <body sha256, lower hex>         sha256 of the raw body bytes (empty body hashes the empty string)
    <device id>                      sha256 of the key's SubjectPublicKeyInfo DER, lower hex
    <counter>                        per-device monotonic request counter, decimal
    <challenge or "">                server-issued challenge (hex), or empty in counter-only mode
    <channel pin, lower hex>         the pinned EDGE/FIRMBASE certificate fingerprint

Binding the counter and the channel pin into the signed string is what gives replay and freshness
(condition 1): a captured signature is useless on a different channel or at or below a counter the
server has already seen. The query string is inside the signed path so it cannot be tampered with.

The ECDSA (P-256, SHA-256) sign/verify helpers import `cryptography` lazily, so this module and its
canonical-string tests run anywhere; only a box that actually verifies needs the library installed.
"""
from __future__ import annotations

import hashlib

PROTO = "milux-sync/1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def body_hash(body: bytes) -> str:
    """Lower-hex sha256 of the raw body bytes; the empty body hashes the empty string."""
    return hashlib.sha256(body or b"").hexdigest()


def device_id(spki_der: bytes) -> str:
    """The stable device id: lower-hex sha256 of the SubjectPublicKeyInfo (X.509) DER of the public
    key. Both sides derive it from the same encoded key, so it needs no separate registration."""
    return hashlib.sha256(spki_der).hexdigest()


def signing_string(
    method: str,
    path_with_query: str,
    body: bytes,
    device_id: str,
    counter: int,
    challenge: str = "",
    channel_pin: str = "",
) -> bytes:
    """The exact bytes both sides sign and verify. See the module docstring for the field order."""
    parts = [
        PROTO,
        method.upper(),
        path_with_query,
        body_hash(body),
        device_id,
        str(int(counter)),
        challenge or "",
        (channel_pin or "").lower(),
    ]
    return "\n".join(parts).encode("utf-8")


def sign(private_key, **kwargs) -> bytes:
    """ECDSA P-256 / SHA-256 signature (DER) over the canonical string. `private_key` is a
    cryptography EllipticCurvePrivateKey. Keyword args are the signing_string fields."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    msg = signing_string(**kwargs)
    return private_key.sign(msg, ec.ECDSA(hashes.SHA256()))


def verify(public_key, signature: bytes, **kwargs) -> bool:
    """True if `signature` (DER) verifies over the canonical string under `public_key`
    (a cryptography EllipticCurvePublicKey). Never raises on a bad signature; returns False."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    msg = signing_string(**kwargs)
    try:
        public_key.verify(signature, msg, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
