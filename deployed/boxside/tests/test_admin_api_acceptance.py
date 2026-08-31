"""Acceptance tests for the operator (admin) surface: the console's seam onto the box
(ADR-003; the console's Deployed page presents the admin token, minted once into the box
config and never part of any QR). Token auth, not signatures, so the whole suite runs
anywhere. Run: python3 -m unittest discover -s boxside/tests -v
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
from vaultsync.enrolment import pairing_code, parse_qr  # noqa: E402
from vaultsync.syncsurface import BoxState, SyncServer  # noqa: E402

NOW = 1_757_000_000
FAKE_DER = b"admin-suite-fake-spki"
FAKE_B64 = base64.b64encode(FAKE_DER).decode("ascii")


class AdminCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = BoxState(os.path.join(self._tmp.name, "state"), box="kit",
                              base_url="http://test.invalid", clock=lambda: NOW + 10)
        self.server = SyncServer(self.state, bind="127.0.0.1", port=0)
        self.server.start()
        self.port = self.server.port

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def call(self, method, path, body=None, token=None):
        headers = {}
        if token is not None:
            headers["X-VD-Admin"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    def admin(self, method, path, obj=None, raw=None):
        body = raw if raw is not None else (
            json.dumps(obj).encode() if obj is not None else None)
        return self.call(method, path, body, token=self.state.admin_token)

    def test_token_gate(self):
        for token in (None, "", "wrong-token"):
            status, body = self.call("GET", "/admin/overview", token=token)
            self.assertEqual(status, 401, f"token={token!r}")
        status, _ = self.admin("GET", "/admin/overview")
        self.assertEqual(status, 200)

    def test_mint_pending_confirm_revoke_flow(self):
        status, out = self.admin("POST", "/admin/mint", {
            "device": "S23", "holder": "Cpl Bloggs", "deployment": "OP TELIC",
            "ceiling": "OFFICIAL", "ttl": 600})
        self.assertEqual(status, 200)
        payload = parse_qr(out["payload"])
        self.assertEqual(payload["box"], "kit")
        # qr_png_b64 is present where qrencode exists; the payload always is.
        status, reply = self.call(
            "POST", "/enrol",
            json.dumps({"tok": payload["tok"], "key": FAKE_B64}).encode())
        self.assertEqual(status, 202)
        fp = reply["fingerprint"]

        status, over = self.admin("GET", "/admin/overview")
        pend = over["pending"]
        self.assertEqual([p["fingerprint"] for p in pend], [fp])
        self.assertEqual(pend[0]["code"], pairing_code(fp))
        self.assertIn("OP TELIC", over["deployments"])
        self.assertIn("OP TELIC", over["manifest"])

        status, out = self.admin("POST", "/admin/confirm", {"code": pend[0]["code"]})
        self.assertEqual((status, out["status"]), (200, "enrolled"))
        status, over = self.admin("GET", "/admin/overview")
        self.assertEqual(over["pending"], [])
        dev = over["devices"][0]
        self.assertEqual((dev["label"], dev["holder"], dev["revoked"]),
                         ("S23", "Cpl Bloggs", False))

        status, out = self.admin("POST", "/admin/revoke",
                                 {"fingerprint": fp, "reason": "end of deployment"})
        self.assertEqual((status, out["status"]), (200, "revoked"))
        status, over = self.admin("GET", "/admin/overview")
        self.assertTrue(over["devices"][0]["revoked"])

    def test_reject_by_code_and_unknown_refusals(self):
        status, out = self.admin("POST", "/admin/mint",
                                 {"device": "X", "holder": "Y", "deployment": "DEMO"})
        payload = parse_qr(out["payload"])
        self.call("POST", "/enrol",
                  json.dumps({"tok": payload["tok"], "key": FAKE_B64}).encode())
        code = pairing_code(signing.device_id(FAKE_DER))
        status, out = self.admin("POST", "/admin/reject", {"code": code})
        self.assertEqual((status, out["status"]), (200, "rejected"))
        status, _ = self.admin("POST", "/admin/confirm", {"code": "000000"})
        self.assertEqual(status, 404)
        status, _ = self.admin("POST", "/admin/revoke", {"fingerprint": "0" * 64})
        self.assertEqual(status, 404)

    def test_pack_assign_list_remove_over_admin(self):
        data = b"PK\x03\x04 console-pushed pack"
        status, out = self.admin(
            "POST", "/admin/pack?deployment=DEMO&kind=mission&name=console.zip", raw=data)
        self.assertEqual(status, 200)
        self.assertEqual(out["assigned"]["name"], "console.zip")
        status, out = self.admin("GET", "/admin/packs?deployment=DEMO")
        self.assertEqual([p["name"] for p in out["packs"]], ["console.zip"])
        status, _ = self.admin("POST", "/admin/pack?deployment=DEMO&kind=nonsense&name=x",
                               raw=b"z")
        self.assertEqual(status, 400)
        status, out = self.admin("POST", "/admin/pack-remove",
                                 {"deployment": "DEMO", "name": "console.zip"})
        self.assertEqual((status, out["status"]), (200, "removed"))


if __name__ == "__main__":
    unittest.main()
