"""Acceptance tests for Spec 002 (the box's enrolment surface over HTTP). Committed failing.

AC4 needs `cryptography` (real keys, real signatures) and is skipped where it is absent; it
MUST be run on a box that has the library before the slice's gate is called met. The device
gate itself (the S23 joining the NUC live) is a named artefact on an internal card, not a test here.
Run: python3 -m unittest discover -s boxside/tests -v
"""
import base64
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vaultsync import signing  # noqa: E402
from vaultsync.enrolment import parse_qr  # noqa: E402
from vaultsync.syncsurface import BoxState, SyncServer, STALENESS_MENU_S  # noqa: E402

try:
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

NOW = 1_756_700_000
FAKE_DER = b"stable-fake-spki-der-for-http-tests"
FAKE_B64 = base64.b64encode(FAKE_DER).decode("ascii")


def call(port, method, path, body=None, headers=None):
    """(status, json_body) for a request against the test server; HTTP errors returned, not raised."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body,
        headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


class ServerCase(unittest.TestCase):
    """A fresh state dir and a running server per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = BoxState(self._tmp.name, box="kit", base_url="http://test.invalid",
                              clock=lambda: NOW + 10)
        self.server = SyncServer(self.state, bind="127.0.0.1", port=0)
        self.server.start()
        self.port = self.server.port

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def mint(self, now=NOW, ttl_s=600):
        qr = self.state.mint_qr(device_label="S23", holder="Cpl Bloggs",
                                deployment_scope="OP TELIC", clearance_ceiling="OFFICIAL",
                                now=now, ttl_s=ttl_s)
        return parse_qr(qr)

    def enrol(self, tok, key_b64=FAKE_B64):
        return call(self.port, "POST", "/enrol",
                    json.dumps({"tok": tok, "key": key_b64}).encode("utf-8"))


class EnrolOverHttp(ServerCase):
    def test_ac1_enrol_pending(self):
        payload = self.mint()
        status, body = self.enrol(payload["tok"])
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["fingerprint"], signing.device_id(FAKE_DER))
        self.assertEqual([p.fingerprint for p in self.state.enrolment.pending()],
                         [signing.device_id(FAKE_DER)])

    def test_ac2_refusals_are_honest_and_bad_key_burns_nothing(self):
        payload = self.mint()
        status, body = self.enrol(payload["tok"], key_b64="%%%not-base64%%%")
        self.assertEqual(status, 400)
        self.assertEqual(body["reason"], "bad-key")
        status, body = self.enrol(payload["tok"])  # same token, good key: unburnt
        self.assertEqual(status, 202)
        status, body = self.enrol(payload["tok"])  # now spent
        self.assertEqual(status, 403)
        self.assertEqual(body["reason"], "used")
        status, body = self.enrol("never-minted")
        self.assertEqual(status, 403)
        self.assertEqual(body["reason"], "unknown")
        stale = self.mint(now=NOW - 700, ttl_s=600)
        status, body = self.enrol(stale["tok"])
        self.assertEqual(status, 403)
        self.assertEqual(body["reason"], "expired")

    def test_ac3_status_lifecycle(self):
        payload = self.mint()
        _, body = self.enrol(payload["tok"])
        fp = body["fingerprint"]
        status, body = call(self.port, "GET", f"/enrol/status/{fp}")
        self.assertEqual((status, body["status"]), (200, "pending"))
        self.state.confirm(fp, now=NOW + 60)
        status, body = call(self.port, "GET", f"/enrol/status/{fp}")
        self.assertEqual((status, body["status"]), (200, "enrolled"))
        self.assertEqual(body["deployment_scope"], "OP TELIC")
        self.assertGreaterEqual(body["roll_version"], 2)
        self.assertIsInstance(body["counter_watermark"], int)
        status, _ = call(self.port, "GET", "/enrol/status/" + "0" * 64)
        self.assertEqual(status, 404)

    def test_ac5_staleness_menu_enforced(self):
        self.assertEqual(self.state.staleness_bound_s(), 7 * 24 * 3600)
        for allowed in STALENESS_MENU_S:
            self.state.set_staleness(allowed)
        with self.assertRaises(ValueError):
            self.state.set_staleness(3600)
        self.state.set_staleness(24 * 3600)
        status, body = call(self.port, "GET", "/sync/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["staleness_bound_s"], 24 * 3600)
        self.assertEqual(body["box"], "kit")


class ReEnrolAfterForget(ServerCase):
    """A device that forgot its binding rescans with the SAME key: no second ceremony, the
    token still burns, no pending is created, and the watermark comes back so the counter
    resumes above it (the Forget-then-rescan defect of 30 Aug, fixed at root)."""

    def test_trusted_key_short_circuits(self):
        payload = self.mint()
        _, body = self.enrol(payload["tok"])
        self.state.confirm(body["fingerprint"], now=NOW + 60)
        again = self.mint()
        status, body = self.enrol(again["tok"])
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "enrolled")
        self.assertEqual(body["deployment_scope"], "OP TELIC")
        self.assertIsInstance(body["counter_watermark"], int)
        self.assertEqual(self.state.enrolment.pending(), [])
        status, body = self.enrol(again["tok"])       # the token burnt all the same
        self.assertEqual((status, body["reason"]), (403, "used"))


@unittest.skipUnless(HAVE_CRYPTO, "needs cryptography; run on the box (AC4)")
class SignedPing(ServerCase):
    def _real_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec as ecc
        key = ecc.generate_private_key(ecc.SECP256R1())
        spki = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return key, spki

    def _enrol_confirm(self, spki):
        payload = self.mint()
        _, body = self.enrol(payload["tok"], base64.b64encode(spki).decode("ascii"))
        fp = body["fingerprint"]
        self.state.confirm(fp, now=NOW + 60)
        return fp, payload["pin"]

    def _ping(self, key, fp, pin, counter, body=b"{}", sign_body=None, sign_pin=None):
        sig = signing.sign(key, method="POST", path_with_query="/sync/ping",
                           body=sign_body if sign_body is not None else body,
                           device_id=fp, counter=counter, challenge="",
                           channel_pin=sign_pin if sign_pin is not None else pin)
        return call(self.port, "POST", "/sync/ping", body, headers={
            "X-VD-Device": fp, "X-VD-Counter": str(counter),
            "X-VD-Signature": base64.b64encode(sig).decode("ascii")})

    def test_ac4_signed_ping_full_matrix(self):
        key, spki = self._real_key()
        fp, pin = self._enrol_confirm(spki)

        status, body = self._ping(key, fp, pin, counter=1)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["label"], "S23")
        self.assertEqual(body["deployment_scope"], "OP TELIC")

        status, _ = self._ping(key, fp, pin, counter=1)          # replay
        self.assertEqual(status, 401)
        status, _ = self._ping(key, fp, pin, counter=2, sign_pin="00" * 32)  # wrong channel
        self.assertEqual(status, 401)
        status, _ = self._ping(key, fp, pin, counter=2, sign_body=b'{"a":1}')  # tampered body
        self.assertEqual(status, 401)
        status, _ = self._ping(key, fp, pin, counter=2)          # counter not burnt by failures
        self.assertEqual(status, 200)

        self.server.stop()                                        # watermark survives restart
        self.state = BoxState(self._tmp.name, box="kit", base_url="http://test.invalid")
        self.server = SyncServer(self.state, bind="127.0.0.1", port=0)
        self.server.start()
        self.port = self.server.port
        status, _ = self._ping(key, fp, pin, counter=2)
        self.assertEqual(status, 401)
        status, _ = self._ping(key, fp, pin, counter=3)
        self.assertEqual(status, 200)

        self.state.revoke(fp, "end of deployment", now=NOW + 300)
        status, body = self._ping(key, fp, pin, counter=4)
        self.assertEqual(status, 403)

    def test_ac4_pending_device_refused(self):
        key, spki = self._real_key()
        payload = self.mint()
        _, body = self.enrol(payload["tok"], base64.b64encode(spki).decode("ascii"))
        status, _ = self._ping(key, body["fingerprint"], payload["pin"], counter=1)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
