"""Acceptance tests for Spec 005 (pack sync). Committed failing.

AC1 (the manifest) runs anywhere; the signed endpoint tests need `cryptography` and their
evidence run is on the NUC. Run: python3 -m unittest discover -s boxside/tests -v
"""
import base64
import hashlib
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
from vaultsync.packs import PackStore  # noqa: E402
from vaultsync.syncsurface import BoxState, SyncServer  # noqa: E402

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec as ecc
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

NOW = 1_756_900_000


def call(port, method, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class ManifestBasics(unittest.TestCase):
    def test_ac1_manifest_add_replace_remove_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "route-pack.zip")
            with open(src, "wb") as f:
                f.write(b"PK\x03\x04 fake zip body one")
            store = PackStore(os.path.join(tmp, "state"))
            entry = store.add("DEMO", src, kind="mission", now=NOW)
            self.assertEqual(entry["name"], "route-pack.zip")
            self.assertEqual(entry["kind"], "mission")
            self.assertEqual(entry["size"], os.path.getsize(src))
            self.assertEqual(entry["sha256"],
                             hashlib.sha256(open(src, "rb").read()).hexdigest())

            reloaded = PackStore(os.path.join(tmp, "state"))
            self.assertEqual([e["name"] for e in reloaded.list_for("DEMO")],
                             ["route-pack.zip"])
            with open(reloaded.path_for("DEMO", "route-pack.zip"), "rb") as f:
                self.assertEqual(f.read(), b"PK\x03\x04 fake zip body one")

            with open(src, "wb") as f:                    # a pack update: same name, new bytes
                f.write(b"PK\x03\x04 fake zip body TWO longer")
            entry2 = store.add("DEMO", src, kind="mission", now=NOW + 10)
            self.assertEqual(len(store.list_for("DEMO")), 1)
            self.assertNotEqual(entry["sha256"], entry2["sha256"])

            self.assertTrue(store.remove("DEMO", "route-pack.zip"))
            self.assertEqual(store.list_for("DEMO"), [])
            self.assertFalse(store.remove("DEMO", "route-pack.zip"))
            self.assertIsNone(store.path_for("DEMO", "route-pack.zip"))


@unittest.skipUnless(HAVE_CRYPTO, "signed endpoints; run on the box")
class PackTransport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.vault = os.path.join(root, "vault")
        os.makedirs(os.path.join(self.vault, "DEMO"))
        self.state = BoxState(os.path.join(root, "state"), box="kit",
                              base_url="http://test.invalid", vault=self.vault,
                              clock=lambda: NOW + 10)
        self.mission = os.urandom(2_000_000)              # megabyte-scale, binary-safe
        for name, kind, body, dep in (
                ("routes.zip", "mission", self.mission, "DEMO"),
                ("basemap.zip", "map", b"MAPDATA" * 1000, "DEMO"),
                ("secret-elsewhere.zip", "mission", b"NOT FOR DEMO", "OTHER")):
            src = os.path.join(root, name)
            with open(src, "wb") as f:
                f.write(body)
            self.state.packs.add(dep, src, kind=kind, now=NOW)
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
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/enrol", data=body,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.fp = json.loads(r.read())["fingerprint"]
        self.state.confirm(self.fp, now=NOW + 60)
        self.counter = 0

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def signed(self, path, counter=None):
        c = counter if counter is not None else self.counter + 1
        if counter is None:
            self.counter = c
        sig = signing.sign(self.key, method="GET", path_with_query=path, body=b"",
                           device_id=self.fp, counter=c, challenge="", channel_pin=self.pin)
        return call(self.port, "GET", path, headers={
            "X-VD-Device": self.fp, "X-VD-Counter": str(c),
            "X-VD-Signature": base64.b64encode(sig).decode("ascii")})

    def test_ac2_scope(self):
        status, body = self.signed("/sync/packs")
        self.assertEqual(status, 200)
        packs = {p["name"]: p for p in json.loads(body)["packs"]}
        self.assertEqual(set(packs), {"routes.zip", "basemap.zip"})
        self.assertEqual(packs["routes.zip"]["kind"], "mission")
        self.assertEqual(packs["basemap.zip"]["kind"], "map")
        status, _ = self.signed("/sync/pack?name=" + urllib.parse.quote("secret-elsewhere.zip"))
        self.assertEqual(status, 403)

    def test_ac3_binary_round_trip(self):
        status, body = self.signed("/sync/pack?name=routes.zip")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.mission)
        status, listing = self.signed("/sync/packs")
        entry = next(p for p in json.loads(listing)["packs"] if p["name"] == "routes.zip")
        self.assertEqual(entry["sha256"], hashlib.sha256(self.mission).hexdigest())
        self.assertEqual(entry["size"], len(self.mission))
        status, _ = self.signed("/sync/pack?name=no-such.zip")
        self.assertEqual(status, 404)

    def test_ac4_signed_matrix_shared_watermark(self):
        status, _ = self.signed("/sync/packs", counter=1)
        self.assertEqual(status, 200)
        status, _ = self.signed("/sync/packs", counter=1)          # replay
        self.assertEqual(status, 401)
        status, _ = self.signed("/sync/pack?name=basemap.zip", counter=2)
        self.assertEqual(status, 200)
        status, _ = self.signed("/sync/packs", counter=2)          # one stream per device
        self.assertEqual(status, 401)
        self.state.revoke(self.fp, "end of deployment", now=NOW + 300)
        status, _ = self.signed("/sync/packs", counter=9)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
