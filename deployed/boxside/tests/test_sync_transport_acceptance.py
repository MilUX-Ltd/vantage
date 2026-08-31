"""Acceptance tests for Spec 004 (the sync transport). Committed failing.

Every transport operation is signed, so this whole suite needs `cryptography` and its
evidence run is on the NUC; the vendored unit suites cover the pre-crypto logic everywhere.
Run: python3 -m unittest discover -s boxside/tests -v
"""
import base64
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vaultsync import signing  # noqa: E402
from vaultsync.enrolment import parse_qr  # noqa: E402
from vaultsync.syncsurface import BoxState, SyncServer  # noqa: E402

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec as ecc
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

NOW = 1_756_800_000

OPORD = "# OPORD 1\n\nMove at dawn.\n"
HIGH = "---\nclassification: OFFICIAL-SENSITIVE\n---\n\n# Held back\n"
ELSEWHERE = "# Another deployment's note\n"


def call(port, method, path, body=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@unittest.skipUnless(HAVE_CRYPTO, "every transport op is signed; run on the box")
class TransportCase(unittest.TestCase):
    """A demo vault, a running server, and one enrolled-and-confirmed device per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.vault = os.path.join(root, "vault")
        os.makedirs(os.path.join(self.vault, "DEMO", "orders"))
        os.makedirs(os.path.join(self.vault, "OTHER"))
        self._write("DEMO/orders/opord.md", OPORD)
        self._write("DEMO/held-back.md", HIGH)
        self._write("OTHER/elsewhere.md", ELSEWHERE)
        self.state = BoxState(os.path.join(root, "state"), box="kit",
                              base_url="http://test.invalid", vault=self.vault,
                              clock=lambda: NOW + 10)
        self.server = SyncServer(self.state, bind="127.0.0.1", port=0)
        self.server.start()
        self.port = self.server.port

        self.key = ecc.generate_private_key(ecc.SECP256R1())
        spki = self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        payload = parse_qr(self.state.mint_qr(
            device_label="S23", holder="Matt", deployment_scope="DEMO",
            clearance_ceiling="OFFICIAL", now=NOW, ttl_s=600))
        self.pin = payload["pin"]
        body = json.dumps({"tok": payload["tok"],
                           "key": base64.b64encode(spki).decode("ascii")}).encode()
        status, reply = call(self.port, "POST", "/enrol", body)
        assert status == 202, reply
        self.fp = json.loads(reply)["fingerprint"]
        self.state.confirm(self.fp, now=NOW + 60)
        self.counter = 0

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def _write(self, rel, text):
        full = os.path.join(self.vault, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)

    def signed(self, method, path_with_query, body=b"", counter=None, pin=None):
        c = counter if counter is not None else self.counter + 1
        if counter is None:
            self.counter = c
        sig = signing.sign(self.key, method=method, path_with_query=path_with_query,
                           body=body, device_id=self.fp, counter=c, challenge="",
                           channel_pin=pin if pin is not None else self.pin)
        return call(self.port, method, path_with_query, body or None, headers={
            "X-VD-Device": self.fp, "X-VD-Counter": str(c),
            "X-VD-Signature": base64.b64encode(sig).decode("ascii")})

    # ---- AC1 ------------------------------------------------------------------------------
    def test_ac1_index_scoped_and_ceiling_filtered(self):
        status, body = self.signed("GET", "/sync/index")
        self.assertEqual(status, 200)
        files = json.loads(body)["files"]
        paths = [f["path"] for f in files]
        self.assertIn("DEMO/orders/opord.md", paths)
        self.assertNotIn("DEMO/held-back.md", paths)          # above the ceiling
        self.assertNotIn("OTHER/elsewhere.md", paths)         # another deployment
        entry = next(f for f in files if f["path"] == "DEMO/orders/opord.md")
        self.assertEqual(entry["sha256"], signing.body_hash(OPORD.encode()))

    # ---- AC2 ------------------------------------------------------------------------------
    def test_ac2_pull_scope_and_refusals(self):
        p = "/sync/file?path=" + urllib.parse.quote("DEMO/orders/opord.md")
        status, body = self.signed("GET", p)
        self.assertEqual((status, body.decode()), (200, OPORD))
        for bad in ("OTHER/elsewhere.md", "../secrets.md", "DEMO/../OTHER/elsewhere.md",
                    "DEMO/held-back.md", "DEMO/no-such-note.md"):
            status, _ = self.signed("GET", "/sync/file?path=" + urllib.parse.quote(bad))
            self.assertIn(status, (403, 404), bad)

    # ---- AC3 ------------------------------------------------------------------------------
    def test_ac3_push_create_and_fast_forward_with_history(self):
        rel = "DEMO/orders/frago.md"
        status, body = self.signed(
            "POST", f"/sync/push?path={urllib.parse.quote(rel)}&base=new", b"# FRAGO 1\n")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["action"], "created")
        base = signing.body_hash(b"# FRAGO 1\n")
        status, body = self.signed(
            "POST", f"/sync/push?path={urllib.parse.quote(rel)}&base={base}", b"# FRAGO 1a\n")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["action"], "updated")
        with open(os.path.join(self.vault, rel), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# FRAGO 1a\n")
        history = os.path.join(self.state.state_dir, "history")
        kept = [os.path.join(r, n) for r, _, ns in os.walk(history) for n in ns]
        self.assertTrue(any(open(k, encoding="utf-8").read() == "# FRAGO 1\n" for k in kept),
                        "previous version must be retained outside the vault")

    # ---- AC4 ------------------------------------------------------------------------------
    def test_ac4_push_conflict_stages_outside_vault(self):
        rel = "DEMO/orders/opord.md"
        stale = signing.body_hash(b"not what the box holds")
        status, body = self.signed(
            "POST", f"/sync/push?path={urllib.parse.quote(rel)}&base={stale}", b"# rival edit\n")
        self.assertEqual(status, 409)
        with open(os.path.join(self.vault, rel), encoding="utf-8") as f:
            self.assertEqual(f.read(), OPORD)                 # the vault's single version stands
        quarantine = os.path.join(self.state.state_dir, "quarantine")
        staged = [os.path.join(r, n) for r, _, ns in os.walk(quarantine) for n in ns]
        self.assertTrue(any(open(s, encoding="utf-8").read() == "# rival edit\n" for s in staged))
        inside = [n for r, _, ns in os.walk(self.vault) for n in ns if "conflict" in n]
        self.assertEqual(inside, [], "no conflict copy may ever sit inside the vault")

    # ---- AC5 ------------------------------------------------------------------------------
    def test_ac5_push_above_ceiling_refused(self):
        status, _ = self.signed(
            "POST", "/sync/push?path=" + urllib.parse.quote("DEMO/orders/leak.md") + "&base=new",
            HIGH.encode())
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(os.path.join(self.vault, "DEMO", "orders", "leak.md")))

    # ---- AC6 ------------------------------------------------------------------------------
    def test_ac6_signed_matrix_and_shared_watermark(self):
        status, _ = self.signed("GET", "/sync/index", counter=1)
        self.assertEqual(status, 200)
        status, _ = self.signed("GET", "/sync/index", counter=1)          # replay
        self.assertEqual(status, 401)
        p = "/sync/file?path=" + urllib.parse.quote("DEMO/orders/opord.md")
        sig = signing.sign(self.key, method="GET", path_with_query=p, body=b"",
                           device_id=self.fp, counter=2, challenge="", channel_pin=self.pin)
        tampered = "/sync/file?path=" + urllib.parse.quote("DEMO/held-back.md")
        status, _ = call(self.port, "GET", tampered, None, headers={
            "X-VD-Device": self.fp, "X-VD-Counter": "2",
            "X-VD-Signature": base64.b64encode(sig).decode("ascii")})
        self.assertEqual(status, 401)                                     # path is in the signature
        body = b"{}"
        sig = signing.sign(self.key, method="POST", path_with_query="/sync/ping", body=body,
                           device_id=self.fp, counter=2, challenge="", channel_pin=self.pin)
        status, _ = call(self.port, "POST", "/sync/ping", body, headers={
            "X-VD-Device": self.fp, "X-VD-Counter": "2",
            "X-VD-Signature": base64.b64encode(sig).decode("ascii")})
        self.assertEqual(status, 200)                                     # watermark now 2
        status, _ = self.signed("GET", "/sync/index", counter=2)          # ping burned it
        self.assertEqual(status, 401)
        self.state.revoke(self.fp, "end of deployment", now=NOW + 300)
        status, _ = self.signed("GET", "/sync/index", counter=9)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_CRYPTO, "signed ops; run on the box")
class ReadOnlyPosture(TransportCase):
    """--read-only: pulls serve, pushes are politely refused before any auth work."""

    def setUp(self):
        super().setUp()
        self.state.read_only = True

    # The inherited push tests assert writes succeed, which is exactly what this posture
    # forbids; the pull-side inherited tests still run and must still pass.
    test_ac3_push_create_and_fast_forward_with_history = None
    test_ac4_push_conflict_stages_outside_vault = None

    def test_pull_serves_push_refused(self):
        p = "/sync/file?path=" + urllib.parse.quote("DEMO/orders/opord.md")
        status, body = self.signed("GET", p)
        self.assertEqual((status, body.decode()), (200, OPORD))
        status, body = self.signed(
            "POST", "/sync/push?path=" + urllib.parse.quote("DEMO/orders/new.md") + "&base=new",
            b"# blocked\n")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["reason"], "read-only")
        self.assertFalse(os.path.exists(os.path.join(self.vault, "DEMO", "orders", "new.md")))


@unittest.skipUnless(HAVE_CRYPTO, "signed ops; run on the box")
class MultiDeploymentScope(TransportCase):
    """A device carrying several deployments (labels joined with |, Matt 30 Aug): the index
    is the union of its folders, pull and push work in each, and everything else stays out
    of reach."""

    def setUp(self):
        super().setUp()
        self._write("SECOND/plan.md", "# Second deployment plan\n")
        self._write("THIRD/private.md", "# Not for this device\n")
        roll = self.state.roll()
        rec = roll.record(self.fp)
        rec.deployment_scope = "DEMO|SECOND"
        self.state._save_roll(roll.bumped(NOW + 90))

    def test_union_index_pull_push_and_scope_edge(self):
        status, body = self.signed("GET", "/sync/index")
        self.assertEqual(status, 200)
        paths = [f["path"] for f in json.loads(body)["files"]]
        self.assertIn("DEMO/orders/opord.md", paths)
        self.assertIn("SECOND/plan.md", paths)
        self.assertNotIn("THIRD/private.md", paths)
        status, body = self.signed(
            "GET", "/sync/file?path=" + urllib.parse.quote("SECOND/plan.md"))
        self.assertEqual((status, body.decode()), (200, "# Second deployment plan\n"))
        status, _ = self.signed(
            "POST", "/sync/push?path=" + urllib.parse.quote("SECOND/frago.md") + "&base=new",
            b"# pushed into the second scope\n")
        self.assertEqual(status, 200)
        status, _ = self.signed(
            "GET", "/sync/file?path=" + urllib.parse.quote("THIRD/private.md"))
        self.assertEqual(status, 403)


@unittest.skipUnless(HAVE_CRYPTO, "signed ops; run on the box")
class DeviceToBoxDeletion(TransportCase):
    """A device deletes a note it holds; the box removes it, conflict-safe and evidence-kept."""

    def _delete(self, rel, base, counter=None):
        p = f"/sync/delete?path={urllib.parse.quote(rel)}&base={base}"
        return self.signed("POST", p, counter=counter)

    def test_delete_fast_forward_with_history(self):
        rel = "DEMO/orders/opord.md"
        base = signing.body_hash(OPORD.encode())
        status, body = self._delete(rel, base)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["action"], "deleted")
        self.assertFalse(os.path.exists(os.path.join(self.vault, rel)))
        history = os.path.join(self.state.state_dir, "history")
        kept = [os.path.join(r, n) for r, _, ns in os.walk(history) for n in ns]
        self.assertTrue(any(open(k, encoding="utf-8").read() == OPORD for k in kept),
                        "the deleted note must be retained outside the vault")

    def test_delete_idempotent(self):
        rel = "DEMO/orders/opord.md"
        base = signing.body_hash(OPORD.encode())
        self._delete(rel, base)
        status, body = self._delete(rel, base)                # already gone
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["action"], "already-gone")

    def test_delete_conflict_keeps_box_version(self):
        rel = "DEMO/orders/opord.md"
        stale = signing.body_hash(b"a version the box never had")
        status, body = self._delete(rel, stale)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["action"], "conflict")
        with open(os.path.join(self.vault, rel), encoding="utf-8") as f:
            self.assertEqual(f.read(), OPORD)                 # the box's version stands

    def test_delete_out_of_scope_refused(self):
        status, _ = self._delete("OTHER/elsewhere.md", "force")
        self.assertEqual(status, 403)

    def test_delete_revoked_refused(self):
        self.state.revoke(self.fp, "end of deployment", now=NOW + 300)
        status, _ = self._delete("DEMO/orders/opord.md", "force", counter=9)
        self.assertEqual(status, 403)
