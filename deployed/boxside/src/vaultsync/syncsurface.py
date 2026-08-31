"""The box's enrolment and sync surface over HTTP (Spec 002).

`BoxState` owns everything on disk under one state directory (never inside a vault): the
enrolment stores from Spec 001, the roll of record (`roll.json`), the per-device counter
watermarks (`counters.json`), and `config.json` carrying the box name, base URL, the interim
per-box channel id (the TLS pin's stand-in until the transport slice) and the staleness bound
from ADR-002's fixed menu.

`SyncServer` is the stdlib HTTP server over it. Signature verification imports `cryptography`
lazily, so everything except the signed ping runs anywhere; the ping path needs the library,
which the boxes have. The counter watermark commits only after a good signature, so a failed
attempt cannot burn a counter (ADR-008 condition 1).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from . import signing
from .enrolment import Enrolment
from .packs import PackStore
from .enrolment import pairing_code
from . import deployment as DEPID
from .roll import Roll
from .syncserver import RequestAuth, SyncCore, SyncDenied, push_safe, delete_safe

STALENESS_MENU_S = (24 * 3600, 48 * 3600, 7 * 24 * 3600, 30 * 24 * 3600, 90 * 24 * 3600)
DEFAULT_STALENESS_S = 7 * 24 * 3600


def _write_atomic(path: str, payload: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, path)


class BoxState:
    """Everything the surface persists, under one state directory."""

    def __init__(self, state_dir: str, box: Optional[str] = None, base_url: Optional[str] = None,
                 clock=None, vault: Optional[str] = None, read_only: bool = False):
        os.makedirs(state_dir, exist_ok=True)
        self.state_dir = state_dir
        self.clock = clock or time.time  # injectable so tests are not wall-clock coupled
        self.read_only = bool(read_only)  # a serve-only posture: pushes politely refused
        self._config_path = os.path.join(state_dir, "config.json")
        self._roll_path = os.path.join(state_dir, "roll.json")
        self._counters_path = os.path.join(state_dir, "counters.json")
        cfg = {}
        if os.path.exists(self._config_path):
            with open(self._config_path, encoding="utf-8") as f:
                cfg = json.load(f)
        # The channel id is minted once at first start and then never changes without a
        # re-enrolment: it is the box identity the phones bind their signatures to (ADR-002
        # decision 2 accepts that changing it means a re-scan).
        self.box = box or cfg.get("box") or "box"
        self.base_url = base_url or cfg.get("base_url") or ""
        self.vault = vault or cfg.get("vault") or ""
        self.channel_pin = cfg.get("channel_pin") or secrets.token_hex(32)
        # The operator channel's credential (the console's Deployed page presents it). Minted
        # once; printed by `vd-ops config`; never part of any QR payload.
        self.admin_token = cfg.get("admin_token") or secrets.token_urlsafe(32)
        self._staleness_s = int(cfg.get("staleness_bound_s") or DEFAULT_STALENESS_S)
        self._save_config()
        self.enrolment = Enrolment(state_dir)
        self.packs = PackStore(state_dir)

    def _save_config(self) -> None:
        _write_atomic(self._config_path, json.dumps({
            "box": self.box, "base_url": self.base_url, "channel_pin": self.channel_pin,
            "admin_token": self.admin_token, "vault": self.vault,
            "staleness_bound_s": self._staleness_s}, separators=(",", ":")))

    # ---- staleness (ADR-002 decision 1) --------------------------------------------------
    def staleness_bound_s(self) -> int:
        return self._staleness_s

    def set_staleness(self, seconds: int) -> None:
        if int(seconds) not in STALENESS_MENU_S:
            raise ValueError(f"staleness bound must be one of {STALENESS_MENU_S}")
        self._staleness_s = int(seconds)
        self._save_config()

    # ---- the roll ------------------------------------------------------------------------
    def roll(self) -> Roll:
        if os.path.exists(self._roll_path):
            with open(self._roll_path, encoding="utf-8") as f:
                return Roll.from_json(f.read())
        return Roll(version=1, generated_at=0)

    def _save_roll(self, roll: Roll) -> None:
        _write_atomic(self._roll_path, roll.to_json())

    def mint_qr(self, device_label: str, holder: str, deployment_scope: str,
                clearance_ceiling: str, now: int, ttl_s: int = 600) -> str:
        return self.enrolment.mint_qr(
            box=self.box, base_url=self.base_url, channel_pin=self.channel_pin,
            device_label=device_label, holder=holder, deployment_scope=deployment_scope,
            clearance_ceiling=clearance_ceiling, now=now, ttl_s=ttl_s)

    def confirm(self, fingerprint: str, now: int) -> Roll:
        new = self.enrolment.confirm(fingerprint, self.roll(), now=now, box=self.box)
        self._save_roll(new)
        return new

    def revoke(self, fingerprint: str, reason: str, now: int) -> bool:
        roll = self.roll()
        if not roll.revoke(fingerprint, reason, now=now):
            return False
        self._save_roll(roll.bumped(now))
        return True

    def deployments(self):
        """Every deployment this box knows about, from any source: the pack manifest, the
        vault's own folders, and the scopes on enrolled and pending devices."""
        labels = set(self.packs._load().keys())
        if self.vault and os.path.isdir(self.vault):
            try:
                labels.update(d.label for d in DEPID.resolve(self.vault))
            except Exception:
                pass
        labels.update(r.deployment_scope for r in self.roll().all_records())
        labels.update(p.deployment_scope for p in self.enrolment.pending())
        return sorted(x for x in labels if x)

    # ---- counter watermarks (commit only after a good signature) -------------------------
    def counter_seen(self, device_id: str) -> int:
        if not os.path.exists(self._counters_path):
            return 0
        with open(self._counters_path, encoding="utf-8") as f:
            return int(json.load(f).get(device_id, 0))

    def counter_commit(self, device_id: str, counter: int) -> None:
        data = {}
        if os.path.exists(self._counters_path):
            with open(self._counters_path, encoding="utf-8") as f:
                data = json.load(f)
        data[device_id] = int(counter)
        _write_atomic(self._counters_path, json.dumps(data, separators=(",", ":")))


def _verify_ping(state: BoxState, path: str, body: bytes,
                 device: str, counter: int, signature_b64: str) -> Tuple[int, dict]:
    """The signed request check, in the order that gives nothing away for free: roll
    membership, watermark, then the signature; the watermark commits only after a good one."""
    roll = state.roll()
    rec = roll.record(device)
    if rec is None or rec.revoked or roll.active(device) is None:
        return 403, {"reason": "not-enrolled"}
    if counter <= state.counter_seen(device):
        return 401, {"reason": "counter"}
    try:
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        public_key = load_der_public_key(base64.b64decode(rec.public_key_b64))
        sig = base64.b64decode(signature_b64)
    except Exception:
        return 401, {"reason": "signature"}
    ok = signing.verify(public_key, sig, method="POST", path_with_query=path, body=body,
                        device_id=device, counter=counter, challenge="",
                        channel_pin=state.channel_pin)
    if not ok:
        return 401, {"reason": "signature"}
    state.counter_commit(device, counter)
    return 200, {"ok": True, "label": rec.label, "deployment_scope": rec.deployment_scope}


class _SeenAdapter:
    """SyncCore's seen-cache over BoxState's persisted counters, so ping and the transport
    share ONE watermark per device and it survives restarts."""

    def __init__(self, state: BoxState):
        self._state = state

    def get(self, device_id: str, default: int = 0) -> int:
        return self._state.counter_seen(device_id)

    def __setitem__(self, device_id: str, counter: int) -> None:
        self._state.counter_commit(device_id, counter)


def _denied_status(msg: str) -> int:
    """Map a SyncDenied reason to the honest HTTP status without leaking detail."""
    if "device" in msg:
        return 403
    if "channel" in msg or "counter" in msg or "signature" in msg:
        return 401
    if "no such file" in msg:
        return 404
    return 403


def _qr_png_b64(payload: str):
    """The enrolment QR as base64 PNG via qrencode (present on the boxes); None without it."""
    import base64 as b64
    import subprocess
    try:
        out = subprocess.run(["qrencode", "-t", "PNG", "-o", "-", payload],
                             capture_output=True, timeout=10)
        if out.returncode == 0 and out.stdout:
            return b64.b64encode(out.stdout).decode("ascii")
    except Exception:
        pass
    return None


class _Handler(BaseHTTPRequestHandler):
    state: BoxState  # set by SyncServer on the subclass

    def _send(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def log_message(self, fmt, *args):  # quiet by default; the server log carries what matters
        pass

    def _core(self) -> SyncCore:
        return SyncCore(vault=self.state.vault, roll=self.state.roll(),
                        channel_pin=self.state.channel_pin, seen=_SeenAdapter(self.state))

    def _auth(self, body: bytes) -> Optional[RequestAuth]:
        try:
            return RequestAuth(
                method=self.command, path_with_query=self.path, body=body,
                device_id=self.headers["X-VD-Device"],
                counter=int(self.headers["X-VD-Counter"]), challenge="",
                signature=base64.b64decode(self.headers["X-VD-Signature"]),
                channel_pin=self.state.channel_pin)
        except (KeyError, TypeError, ValueError):
            return None

    def _admin_ok(self) -> bool:
        import hmac
        given = self.headers.get("X-VD-Admin") or ""
        return bool(given) and hmac.compare_digest(given, self.state.admin_token)

    def _admin_gate(self) -> bool:
        if self._admin_ok():
            return True
        self._send(401, {"reason": "admin token required"})
        return False

    def do_GET(self):
        if self.path == "/admin/overview":
            if not self._admin_gate():
                return
            roll = self.state.roll()
            now = int(self.state.clock())
            return self._send(200, {
                "box": self.state.box, "base_url": self.state.base_url,
                "staleness_bound_s": self.state.staleness_bound_s(),
                "roll_version": roll.version,
                "deployments": self.state.deployments(),
                "manifest": {d: self.state.packs.list_for(d)
                             for d in self.state.deployments()},
                "pending": [{
                    "fingerprint": p.fingerprint, "code": pairing_code(p.fingerprint),
                    "device": p.device_label, "holder": p.holder,
                    "deployment": p.deployment_scope, "age_s": max(0, now - p.enrolled_at),
                } for p in self.state.enrolment.pending()],
                "devices": [{
                    "fingerprint": r.device_id, "label": r.label, "holder": r.holder,
                    "deployment": r.deployment_scope, "ceiling": r.clearance_ceiling,
                    "revoked": r.revoked, "revoked_reason": r.revoked_reason,
                } for r in roll.all_records()],
            })
        if self.path.startswith("/admin/packs?"):
            if not self._admin_gate():
                return
            from urllib.parse import parse_qs, urlparse
            dep = (parse_qs(urlparse(self.path).query).get("deployment") or [""])[0]
            return self._send(200, {"packs": self.state.packs.list_for(dep)})
        if self.path == "/sync/index":
            auth = self._auth(b"")
            if auth is None:
                return self._send(400, {"reason": "bad-request"})
            try:
                return self._send(200, {"files": self._core().index(auth)})
            except SyncDenied as e:
                return self._send(_denied_status(str(e)), {"reason": "refused"})
        if self.path.startswith("/sync/file?"):
            auth = self._auth(b"")
            if auth is None:
                return self._send(400, {"reason": "bad-request"})
            from urllib.parse import parse_qs, urlparse
            rel = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            try:
                data = self._core().get_file(auth, rel)
            except SyncDenied as e:
                return self._send(_denied_status(str(e)), {"reason": "refused"})
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/sync/packs" or self.path.startswith("/sync/pack?"):
            auth = self._auth(b"")
            if auth is None:
                return self._send(400, {"reason": "bad-request"})
            core = self._core()
            try:
                rec = core.authorise(auth)
            except SyncDenied as e:
                return self._send(_denied_status(str(e)), {"reason": "refused"})
            if self.path == "/sync/packs":
                merged, names = [], set()
                for lab in core._scope_labels(rec):
                    for e in self.state.packs.list_for(lab):
                        if e["name"] not in names:
                            names.add(e["name"])
                            merged.append(e)
                return self._send(200, {"packs": merged})
            from urllib.parse import parse_qs, urlparse
            name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
            full = None
            for lab in core._scope_labels(rec):
                full = self.state.packs.path_for(lab, name)
                if full:
                    break
            if full is None:
                # Out of scope and unknown look the same from outside on purpose, except a
                # name another deployment holds, which is honestly a scope refusal.
                others = any(any(e["name"] == name for e in self.state.packs.list_for(d))
                             for d in self.state.packs._load())
                return self._send(403 if others else 404, {"reason": "refused"})
            with open(full, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/enrol/status/"):
            fp = self.path.rsplit("/", 1)[1]
            roll = self.state.roll()
            rec = roll.active(fp)
            if rec is not None:
                return self._send(200, {"status": "enrolled",
                                        "deployment_scope": rec.deployment_scope,
                                        "roll_version": roll.version,
                                        "counter_watermark": self.state.counter_seen(fp)})
            if roll.record(fp) is not None:
                return self._send(200, {"status": "revoked"})
            if any(p.fingerprint == fp for p in self.state.enrolment.pending()):
                return self._send(200, {"status": "pending"})
            return self._send(404, {"reason": "unknown"})
        if self.path == "/sync/config":
            return self._send(200, {"box": self.state.box,
                                    "staleness_bound_s": self.state.staleness_bound_s(),
                                    "roll_version": self.state.roll().version})
        return self._send(404, {"reason": "no-such-path"})

    def do_POST(self):
        body = self._body()
        if self.path.startswith("/admin/"):
            if not self._admin_gate():
                return
            route = self.path.split("?")[0]
            if route == "/admin/mint":
                try:
                    obj = json.loads(body.decode("utf-8"))
                except ValueError:
                    return self._send(400, {"reason": "bad-request"})
                payload = self.state.mint_qr(
                    device_label=str(obj.get("device") or "device"),
                    holder=str(obj.get("holder") or ""),
                    deployment_scope=str(obj.get("deployment") or ""),
                    clearance_ceiling=str(obj.get("ceiling") or "OFFICIAL"),
                    now=int(self.state.clock()),
                    ttl_s=int(obj.get("ttl") or 600))
                return self._send(200, {"payload": payload,
                                        "qr_png_b64": _qr_png_b64(payload)})
            if route in ("/admin/confirm", "/admin/reject", "/admin/revoke"):
                try:
                    obj = json.loads(body.decode("utf-8"))
                except ValueError:
                    return self._send(400, {"reason": "bad-request"})
                fp = str(obj.get("fingerprint") or "")
                if not fp and obj.get("code"):
                    want = str(obj["code"]).replace(" ", "")
                    hits = [p.fingerprint for p in self.state.enrolment.pending()
                            if pairing_code(p.fingerprint) == want]
                    if len(hits) != 1:
                        return self._send(404, {"reason": "no single pending match"})
                    fp = hits[0]
                if route == "/admin/confirm":
                    try:
                        roll = self.state.confirm(fp, now=int(self.state.clock()))
                    except KeyError:
                        return self._send(404, {"reason": "no such pending"})
                    return self._send(200, {"status": "enrolled", "roll_version": roll.version})
                if route == "/admin/reject":
                    ok = self.state.enrolment.reject(fp)
                    return self._send(200 if ok else 404,
                                      {"status": "rejected" if ok else "no such pending"})
                ok = self.state.revoke(fp, str(obj.get("reason") or "revoked"),
                                       now=int(self.state.clock()))
                return self._send(200 if ok else 404,
                                  {"status": "revoked" if ok else "no such device"})
            if route == "/admin/pack":
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                dep = (q.get("deployment") or [""])[0]
                kind = (q.get("kind") or [""])[0]
                name = os.path.basename((q.get("name") or ["pack.zip"])[0])
                if not dep or kind not in ("mission", "map") or not body:
                    return self._send(400, {"reason": "bad-request"})
                import tempfile
                with tempfile.TemporaryDirectory() as td:
                    src = os.path.join(td, name)
                    with open(src, "wb") as fh:
                        fh.write(body)
                    entry = self.state.packs.add(dep, src, kind=kind,
                                                 now=int(self.state.clock()))
                return self._send(200, {"assigned": entry})
            if route == "/admin/pack-remove":
                try:
                    obj = json.loads(body.decode("utf-8"))
                except ValueError:
                    return self._send(400, {"reason": "bad-request"})
                ok = self.state.packs.remove(str(obj.get("deployment") or ""),
                                             str(obj.get("name") or ""))
                return self._send(200 if ok else 404,
                                  {"status": "removed" if ok else "no such pack"})
            return self._send(404, {"reason": "no-such-path"})
        if self.path == "/enrol":
            try:
                obj = json.loads(body.decode("utf-8"))
                tok, key = str(obj["tok"]), str(obj["key"])
            except (ValueError, KeyError, UnicodeDecodeError):
                return self._send(400, {"reason": "bad-request"})
            try:
                fp = signing.device_id(base64.b64decode(key, validate=True))
            except (ValueError, TypeError):
                return self._send(400, {"reason": "bad-key"})
            roll = self.state.roll()
            active = roll.active(fp)
            if active is not None and active.public_key_b64 == key:
                tok_rec, why = self.state.enrolment.tokens.consume(
                    tok, now=int(self.state.clock()))
                if tok_rec is None:
                    return self._send(403, {"reason": why})
                return self._send(200, {
                    "status": "enrolled", "fingerprint": fp,
                    "deployment_scope": active.deployment_scope,
                    "counter_watermark": self.state.counter_seen(fp)})
            pending, why = self.state.enrolment.enrol(tok, key, now=int(self.state.clock()))
            if why == "ok":
                return self._send(202, {"status": "pending", "fingerprint": pending.fingerprint})
            return self._send(400 if why == "bad-key" else 403, {"reason": why})
        if self.path.startswith("/sync/push?"):
            if self.state.read_only:
                return self._send(403, {"reason": "read-only"})
            auth = self._auth(body)
            if auth is None:
                return self._send(400, {"reason": "bad-request"})
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            rel = (q.get("path") or [""])[0]
            base = (q.get("base") or [""])[0]
            now_ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(int(self.state.clock())))
            try:
                out = push_safe(self._core(), auth, rel, body, base,
                                quarantine_dir=os.path.join(self.state.state_dir, "quarantine"),
                                history_dir=os.path.join(self.state.state_dir, "history"),
                                now_ts=now_ts)
            except SyncDenied as e:
                return self._send(_denied_status(str(e)), {"reason": "refused"})
            return self._send(409 if out["action"] == "conflict" else 200, out)
        if self.path.startswith("/sync/delete?"):
            if self.state.read_only:
                return self._send(403, {"reason": "read-only"})
            auth = self._auth(body)
            if auth is None:
                return self._send(400, {"reason": "bad-request"})
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            rel = (q.get("path") or [""])[0]
            base = (q.get("base") or [""])[0]
            now_ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(int(self.state.clock())))
            try:
                out = delete_safe(self._core(), auth, rel, base,
                                  history_dir=os.path.join(self.state.state_dir, "history"),
                                  now_ts=now_ts)
            except SyncDenied as e:
                return self._send(_denied_status(str(e)), {"reason": "refused"})
            return self._send(409 if out["action"] == "conflict" else 200, out)
        if self.path == "/sync/ping":
            try:
                device = self.headers["X-VD-Device"]
                counter = int(self.headers["X-VD-Counter"])
                sig = self.headers["X-VD-Signature"]
            except (KeyError, TypeError, ValueError):
                return self._send(400, {"reason": "bad-request"})
            status, obj = _verify_ping(self.state, self.path, body, device, counter, sig)
            return self._send(status, obj)
        return self._send(404, {"reason": "no-such-path"})


class SyncServer:
    """The stdlib HTTP server over a BoxState, run on a background thread."""

    def __init__(self, state: BoxState, bind: str, port: int):
        handler = type("BoundHandler", (_Handler,), {"state": state})
        self._httpd = ThreadingHTTPServer((bind, port), handler)
        self.port = self._httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
