import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from vaultsync.syncserver import SyncCore, RequestAuth, SyncDenied
from vaultsync.roll import Record, Roll
from vaultsync import signing as SIG

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    HAVE = True
except Exception:
    HAVE = False

PIN = "aabbccdd"


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _note(level, body):
    return f"---\nclassification: {level}\n---\n{body}\n"


def build_vault(root):
    a = os.path.join(root, "Op Alpha"); os.makedirs(a)
    _write(os.path.join(a, "official.md"), _note("OFFICIAL", "# ok\nplain note"))
    _write(os.path.join(a, "secret.md"), _note("SECRET", "# hush"))
    b = os.path.join(root, "Op Bravo"); os.makedirs(b)
    _write(os.path.join(b, "other.md"), _note("OFFICIAL", "# other"))


def a_record(device_id, pub_b64="AAAA", scope="Op Alpha", ceiling="OFFICIAL"):
    return Record(device_id=device_id, public_key_b64=pub_b64, label="S23", holder="Matt",
                  deployment_scope=scope, clearance_ceiling=ceiling,
                  enrolment_provenance="test", chain_id="c1", anchor="a1", enrolled_at=1)


class TestPreCryptoGates(unittest.TestCase):
    """The checks that must fire before any signature work: unknown/revoked device, wrong channel,
    and a replayed (stale) counter. These need no crypto, so they run anywhere."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        build_vault(self.tmp)
        self.roll = Roll(); self.roll.add(a_record("dev1"))
        self.core = SyncCore(self.tmp, self.roll, PIN)

    def _auth(self, counter=1, pin=PIN, device="dev1"):
        return RequestAuth("GET", "/sync/index", b"", device, counter, "", b"sig", pin)

    def test_unknown_device_is_refused(self):
        with self.assertRaises(SyncDenied):
            self.core.authorise(self._auth(device="ghost"))

    def test_revoked_device_is_refused(self):
        self.roll.revoke("dev1", "lost", now=2)
        with self.assertRaises(SyncDenied):
            self.core.authorise(self._auth())

    def test_wrong_channel_is_refused(self):
        with self.assertRaises(SyncDenied):
            self.core.authorise(self._auth(pin="ffff"))

    def test_replay_at_or_below_last_seen_counter_is_refused(self):
        self.core.seen["dev1"] = 5
        with self.assertRaises(SyncDenied) as cm:
            self.core.authorise(self._auth(counter=5))
        self.assertIn("replay", str(cm.exception))
        with self.assertRaises(SyncDenied):
            self.core.authorise(self._auth(counter=4))

    def test_scope_path_traversal_guard(self):
        rec = a_record("dev1")
        for bad in ("../secret", "/etc/passwd", "Op Bravo/other.md", "..", "."):
            with self.assertRaises(SyncDenied):
                self.core._safe_rel(rec, bad)
        # in-scope path is accepted
        self.assertEqual(self.core._safe_rel(rec, "Op Alpha/official.md"), "Op Alpha/official.md")


@unittest.skipUnless(HAVE, "cryptography not installed here; the box has it")
class TestSignedOperations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        build_vault(self.tmp)
        self.priv = ec.generate_private_key(ec.SECP256R1())
        spki = self.priv.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.did = SIG.device_id(spki)
        self.roll = Roll()
        self.roll.add(a_record(self.did, base64.b64encode(spki).decode(), scope="Op Alpha", ceiling="OFFICIAL"))
        self.core = SyncCore(self.tmp, self.roll, PIN)
        self.counter = 0

    def _signed(self, method, path, body=b""):
        self.counter += 1
        sig = SIG.sign(self.priv, method=method, path_with_query=path, body=body,
                       device_id=self.did, counter=self.counter, challenge="", channel_pin=PIN)
        return RequestAuth(method, path, body, self.did, self.counter, "", sig, PIN)

    def test_a_tampered_unsigned_request_is_refused(self):
        auth = self._signed("GET", "/sync/index")
        auth.body = b"changed after signing"     # tamper: body no longer matches the signature
        with self.assertRaises(SyncDenied) as cm:
            self.core.index(auth)
        self.assertIn("signature", str(cm.exception))

    def test_index_is_scoped_and_ceiling_filtered(self):
        rows = self.core.index(self._signed("GET", "/sync/index"))
        paths = {r["path"] for r in rows}
        self.assertIn("Op Alpha/official.md", paths)
        self.assertNotIn("Op Alpha/secret.md", paths)   # above the OFFICIAL ceiling
        self.assertNotIn("Op Bravo/other.md", paths)    # a different deployment

    def test_get_file_respects_ceiling_and_scope(self):
        got = self.core.get_file(self._signed("GET", "/sync/file?path=Op%20Alpha/official.md"), "Op Alpha/official.md")
        self.assertIn(b"plain note", got)
        with self.assertRaises(SyncDenied):   # above ceiling
            self.core.get_file(self._signed("GET", "/sync/file"), "Op Alpha/secret.md")
        with self.assertRaises(SyncDenied):   # out of scope
            self.core.get_file(self._signed("GET", "/sync/file"), "Op Bravo/other.md")

    def test_push_writes_in_scope_and_refuses_above_ceiling(self):
        body = b"---\nclassification: OFFICIAL\n---\n# authored on the EUD\n"
        res = self.core.push(self._signed("POST", "/sync/push", body), "Op Alpha/new.md", body)
        self.assertEqual(res["path"], "Op Alpha/new.md")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "Op Alpha/new.md")))
        # a device cleared to OFFICIAL cannot push a SECRET note
        hush = b"---\nclassification: SECRET\n---\n# nope\n"
        with self.assertRaises(SyncDenied):
            self.core.push(self._signed("POST", "/sync/push", hush), "Op Alpha/hush.md", hush)
        # nor outside its deployment
        with self.assertRaises(SyncDenied):
            self.core.push(self._signed("POST", "/sync/push", body), "Op Bravo/x.md", body)

    def test_counter_advances_and_replay_is_refused(self):
        first = self._signed("GET", "/sync/index")
        self.core.index(first)
        # replay the exact same signed request: its counter is no longer greater than last seen
        with self.assertRaises(SyncDenied) as cm:
            self.core.index(first)
        self.assertIn("replay", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
