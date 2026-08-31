#!/usr/bin/env python3
# Vantage - fleet management for TAK.  Copyright (c) 2026 MilUX Ltd.
# Source-available under the Vantage Community Licence (see LICENSE): free for
# non-commercial use; commercial, government, and MOD use require a licence -
# matt@milux.co.uk
"""vantage-console-serve.py - estate status + management. v1.1.0.

1.1.0 adds estate software management in three layers. INVENTORY: each box's tak-health (>= 1.1.0)
reports what is installed, what version, and whether it is running; the console renders it per box.
BASELINE: /etc/vantage-console/desired.json (root-installed) names the version each managed component
should be; the console shows drift between is and ought. CONVERGE: drift rows link to the matching
gated action (push-checker, update-cloudtak) with the box preselected - one click to review, a
confirm to run, never automatic. The catalogue stays a set of known, reviewed, pinned operations;
there is deliberately no general "deploy software" primitive.

0.2.0's read-only board, plus an ACTIONS layer for managing the estate from one place. The board
stays exactly as read-only as before; actions are a separate, deliberately-gated surface:

  * OFF unless /etc/vantage-console/actions.json exists. No config, no write surface. A bare console
    is read-only, which is the safe default.
  * Every action is ONE allowlisted operation reached through its OWN scoped forced-command SSH
    key (id_action_*). The console cannot run a free-text command; a cracked console gets no shell.
  * Credential-minting actions (issuing a cert, enrolling a device) require an operator passphrase.
    Softer actions (restart a unit, push the checker) need only an explicit confirm.
  * Every attempt writes an audit line: who, what, where, when, outcome. Secrets are never logged.

Standard library only (the box is offline). /api/health.json stays the machine-readable read-only
view; POST /api/action/<id> is the write path.
"""
import base64, hashlib, hmac, html, json, os, re, secrets, socket, subprocess, sys, threading, zlib
import time as _time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.12.1"
STATE = os.environ.get("VANTAGE_CONSOLE_STATE", "/var/lib/vantage-console/state.json")
HISTORY = os.environ.get("VANTAGE_CONSOLE_HISTORY", "/var/lib/vantage-console/history.ndjson")
ACTIONS_CONFIG = os.environ.get("VANTAGE_CONSOLE_ACTIONS", "/etc/vantage-console/actions.json")
ACTION_KEYS = os.environ.get("VANTAGE_CONSOLE_ACTION_KEYS", "/var/lib/vantage-console/.ssh")
AUDIT = os.environ.get("VANTAGE_CONSOLE_AUDIT", "/var/lib/vantage-console/agent/actions.log")
# 1.1.0 estate management. DESIRED is the baseline the inventory is judged against;
# ARTIFACTS holds the files push actions distribute. Both are root-installed and the
# console only READS them: the console cannot alter what it is trusted to push.
DESIRED = os.environ.get("VANTAGE_CONSOLE_DESIRED", "/etc/vantage-console/desired.json")
INSTANCE_FILE = os.environ.get("VANTAGE_CONSOLE_INSTANCE", "/etc/vantage-console/instance.json")

# 1.33.0 white-label: the instance file separates the product (this code) from the
# operator running it. Everything the product hard-coded about MilUX now defaults here
# and is overridable per install - the seam that lets a customer stand up their own
# branded console. Defaults reproduce the MilUX/Vantage identity exactly, so an install
# with no instance file behaves as before. Colours are validated as hex; text is
# length-capped and HTML-escaped at every use.
INSTANCE_DEFAULTS = {
    "product_name": "Vantage",
    "maker": "MilUX",
    "tagline": "fleet management for TAK",
    # the agent is generic until an instance names one, and hidden until one is wired
    # in - a fresh install has nothing for a chat tab to talk to (card 6178)
    "agent_name": "Agent",
    "agent_enabled": True,
    "console_mode": "admin",
    "accent": "#B5B171",          # olive gold
    "accent_warm": "#A35C17",     # burnt ochre - the mark's rays, the family gesture
    "accent_2": "#D2C78D",        # light gold beige
    "ink": "#F7F6EB",             # soft neutral
    "band": "#113308",            # deep green
    # PKI build defaults are the OPERATOR'S identity, not the maker's: empty until the
    # instance sets them, so the Deploy wizard asks rather than assumes
    "org": "", "org_unit": "", "country": "", "state": "", "city": "",
    "font": "standard",
    # Google Maps JS API key for the Networks map (Spec 003). Empty by default: the map
    # then draws its own plan view instead, so a console with no key and no internet still
    # shows the mesh. A Maps browser key is public by design - it ships in the page and is
    # protected by an HTTP-referrer restriction at Google's end, not by being hidden - so it
    # lives in the instance file with the rest of the operator's identity, not in the
    # credentials vault. Restrict it to this console's origin when you mint it.
    "maps_key": "",
}
_INSTANCE_TEXT = ("product_name", "maker", "tagline", "agent_name",
                  "org", "org_unit", "state", "city")
_INSTANCE_COLOR = ("accent", "accent_2", "ink", "band", "accent_warm")
# the typeface choices: self-contained stacks, no extra fetches. "standard" is the
# built-in Manrope / Bai Jamjuree pairing; the others override the two tokens.
FONT_STACKS = {
    "standard": None,
    "system": ('-apple-system,"Segoe UI",system-ui,Roboto,Helvetica,Arial,sans-serif',
               '-apple-system,"Segoe UI",system-ui,Roboto,Helvetica,Arial,sans-serif'),
    "serif": ('Georgia,"Iowan Old Style","Times New Roman",serif',
              'Georgia,"Iowan Old Style","Times New Roman",serif'),
}


# ---- console authentication (1.38.0) ----------------------------------------------------------
# The console holds the keys to the fleet, so as a PRODUCT it carries a login. One operator
# password, scrypt-hashed, stored root-owned in /etc/vantage-console/auth.json (written through
# the setup helper). Sessions are random tokens held in memory: a restart logs everyone out,
# which for an estate console is a feature. With NO auth file the console runs open as it
# always has - our tailnet-trusted install keeps working, and the Overview shows a banner
# until a password is set (trust-on-first-use, the same model infra-TAK uses at install).
#
# Deliberately open, always: /login, /healthz, /api/health.json (monitoring), the favicon,
# /eud and /store/file/ (the device-download shelf - phones bootstrapping in the field have
# no session), and /api/propose (localhost-only by its own check - the agent's channel).
AUTH_FILE = os.environ.get("VANTAGE_CONSOLE_AUTH_FILE", "/etc/vantage-console/auth.json")
# VANTAGE_CONSOLE_AUTH=open declares a DELIBERATELY open console (a trusted private
# network, or a test rig): the welcome gate stands down and the no-password banner
# shows instead. The product default is the gate.
AUTH_OPEN_MODE = os.environ.get("VANTAGE_CONSOLE_AUTH", "") == "open"
SESSION_HOURS = 12
_SESSIONS = {}          # token -> expiry epoch
_LOGIN_FAIL = {}        # ip -> [epoch, ...] sliding window
_AUTH_LOCK = threading.Lock()

AUTH_OPEN_PREFIXES = ("/store/file/", "/eud")
AUTH_OPEN_PATHS = ("/login", "/healthz", "/api/health.json", "/favicon.svg",
                   "/favicon.ico", "/api/propose", "/mcp")


def auth_configured():
    try:
        with open(AUTH_FILE) as fh:
            d = json.load(fh)
        return bool(d.get("scrypt"))
    except Exception:
        return False


def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return {"scrypt": h.hex(), "salt": salt.hex(), "n": 2**14, "r": 8, "p": 1}


def verify_operator_password(pw):
    try:
        with open(AUTH_FILE) as fh:
            d = json.load(fh)
        h = hashlib.scrypt(str(pw).encode(), salt=bytes.fromhex(d["salt"]),
                           n=int(d.get("n", 2**14)), r=int(d.get("r", 8)),
                           p=int(d.get("p", 1)), dklen=32)
        return hmac.compare_digest(h.hex(), d["scrypt"])
    except Exception:
        return False


def session_create():
    tok = secrets.token_urlsafe(32)
    with _AUTH_LOCK:
        now = _time.time()
        for t in [t for t, exp in _SESSIONS.items() if exp < now]:
            del _SESSIONS[t]
        _SESSIONS[tok] = now + SESSION_HOURS * 3600
    return tok


def session_valid(handler):
    cookie = handler.headers.get("Cookie", "") or ""
    m = re.search(r"(?:^|;\s*)vsession=([A-Za-z0-9_-]{20,64})", cookie)
    if not m:
        return False
    with _AUTH_LOCK:
        exp = _SESSIONS.get(m.group(1))
        if not exp or exp < _time.time():
            _SESSIONS.pop(m.group(1), None)
            return False
        _SESSIONS[m.group(1)] = _time.time() + SESSION_HOURS * 3600
    return True


def login_throttled(ip):
    with _AUTH_LOCK:
        now = _time.time()
        fails = [t for t in _LOGIN_FAIL.get(ip, []) if now - t < 600]
        _LOGIN_FAIL[ip] = fails
        return len(fails) >= 5


def login_failed(ip):
    with _AUTH_LOCK:
        _LOGIN_FAIL.setdefault(ip, []).append(_time.time())


def auth_required(path):
    """Does this path need a session? Only asked when auth IS configured."""
    if path in AUTH_OPEN_PATHS:
        return False
    return not any(path.startswith(p) for p in AUTH_OPEN_PREFIXES)


def render_login(error=""):
    e = html.escape
    inst = load_instance()
    doc = page_head(inst["product_name"] + " — sign in", inst)
    doc.append("<main id=main class=wrap style='max-width:420px;margin-top:10vh'>")
    doc.append(f"<div style='display:flex;gap:12px;align-items:center;margin-bottom:18px'>"
               f"{brand_mark(inst, on='light')}<span class=brand style='font-size:22px'>"
               f"{e(inst['product_name'])}</span></div>")
    doc.append("<form class=depcard method=post action=/login style='display:block'>"
               "<label class=fl>Operator password"
               "<input type=password name=password autofocus autocomplete=current-password required>"
               "</label>")
    if error:
        doc.append(f"<div class='banner stale' style='margin:10px 0'>{e(error)}</div>")
    doc.append("<div class=fedpop-act><button class='a-go confirm' type=submit>Sign in</button>"
               "</div></form>")
    doc.append(f"<p class=meta style='margin-top:14px'>{e(inst['product_name'])} {VERSION}. "
               "Sessions last 12 hours; a console restart signs everyone out.</p>")
    doc.append("</main></body></html>")
    return "".join(doc)


def render_welcome():
    """The very first screen a fresh console shows: set the operator password. Written
    for someone new to TAK - it says what this is and why the password comes first."""
    e = html.escape
    inst = load_instance()
    doc = page_head("Welcome — " + inst["product_name"], inst)
    doc.append("<main id=main class=wrap style='max-width:560px;margin-top:7vh'>")
    doc.append(f"<div style='display:flex;gap:12px;align-items:center;margin-bottom:14px'>"
               f"{brand_mark(inst, on='light')}<span class=brand style='font-size:22px'>"
               f"{e(inst['product_name'])}</span></div>")
    doc.append(f"<h2 class=title>Welcome</h2>"
               "<p class=doct>This console builds and runs TAK servers - the mapping "
               "and messaging backbone that ATAK and iTAK devices connect to. From here "
               "you will install your first server, issue credentials to devices, and "
               "watch everything's health in one place.</p>"
               "<p class=doct><b>First: set the operator password.</b> It signs you in, "
               "and the console asks for it again before anything dangerous. Until it is "
               "set, nothing else is reachable.</p>")
    doc.append("<form class=depcard id=wform style='display:block'>"
               "<label class=fl>Operator password"
               "<input type=password id=w-new minlength=12 autofocus "
               "autocomplete=new-password required>"
               "<span class=hint>at least 12 characters. Length beats complexity: a "
               "phrase of three or four unrelated words is strong and typeable.</span></label>"
               "<label class=fl>Confirm password"
               "<input type=password id=w-new2 minlength=12 autocomplete=new-password required>"
               "</label>"
               "<div class=fedpop-act><button class='a-go confirm' type=submit>"
               "Set password and enter</button>"
               "<span id=w-res class=lib-status role=status style='margin-left:12px'></span>"
               "</div></form>")
    doc.append("""<script>(function(){
var f=document.getElementById('wform');
f.addEventListener('submit',function(ev){ev.preventDefault();
  var r=document.getElementById('w-res');
  var n1=document.getElementById('w-new').value,
      n2=document.getElementById('w-new2').value;
  if(n1!==n2){r.textContent='The two passwords do not match.';return;}
  r.textContent='Setting\\u2026';
  fetch('/api/setup/password',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:'operator',new:n1})})
  .then(function(x){return x.json().then(function(j){return{code:x.status,j:j};});})
  .then(function(x){
    if(x.code!==200){r.textContent=x.j.error||'failed';return;}
    var lf=document.createElement('form');lf.method='post';lf.action='/login';
    var pi=document.createElement('input');pi.type='hidden';pi.name='password';pi.value=n1;
    lf.appendChild(pi);document.body.appendChild(lf);lf.submit();
  }).catch(function(){r.textContent='could not reach the console';});
});})();</script>""")
    doc.append("</main></body></html>")
    return "".join(doc)


def load_instance():
    """The instance identity, defaults filled and every value sanitised. Never raises:
    a broken file falls back to defaults so the console always renders."""
    inst = dict(INSTANCE_DEFAULTS)
    try:
        with open(INSTANCE_FILE) as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            for k in _INSTANCE_TEXT:
                v = raw.get(k)
                if isinstance(v, str) and v.strip():
                    inst[k] = v.strip()[:40]
            for k in _INSTANCE_COLOR:
                v = raw.get(k)
                if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v.strip()):
                    inst[k] = v.strip()
            if re.fullmatch(r"[A-Z]{2}", str(raw.get("country", ""))):
                inst["country"] = raw["country"]
            if "agent_enabled" in raw:
                inst["agent_enabled"] = bool(raw["agent_enabled"])
            if raw.get("font") in FONT_STACKS:
                inst["font"] = raw["font"]
            if raw.get("console_mode") in ("admin", "client"):
                inst["console_mode"] = raw["console_mode"]
            mk = str(raw.get("maps_key", "")).strip()
            if re.fullmatch(r"[A-Za-z0-9_.~-]{0,64}", mk):
                inst["maps_key"] = mk
    except Exception:
        pass
    return inst
ARTIFACTS = os.environ.get("VANTAGE_CONSOLE_ARTIFACTS", "/var/lib/vantage-console/artifacts")
# 1.7.0 gated agent. OpenClaw reads the estate and PROPOSES an action here; it never executes.
# A proposal is a normal catalogue action, target-bound and pre-filled, that a human still reads
# and confirms. The console holds the keys and the audit; the agent holds neither. PROPOSALS is
# the queue the agent appends to (via /api/propose) and the operator clears from the Agent page.
# PROPOSALS and AUDIT are the console's only write surface. The board is otherwise read-only
# (ProtectSystem=strict, ReadOnlyPaths=/var/lib/vantage-console); a single writable subdir,
# /var/lib/vantage-console/agent, is carved with ReadWritePaths in the unit so the queue and the
# audit trail can be written while state, keys, baseline and artefacts stay read-only.
PROPOSALS = os.environ.get("VANTAGE_CONSOLE_PROPOSALS", "/var/lib/vantage-console/agent/proposals.json")
# Chat with Sam: his own OpenClaw control UI, exposed on the tailnet by OpenClaw's Tailscale Serve
# (gateway.tailscale.mode "serve"). This is Sam's real chat window; the console only links to it,
# so an OpenClaw update cannot break it. Loopback + Serve is the documented, safe remote-access path.
SAM_CHAT_URL = os.environ.get("VANTAGE_CONSOLE_SAM_CHAT", "https://openclaw-nuc.tail532ded.ts.net/")
# Native chat with Sam: the console is a paired OpenClaw client (sam_client.py) over loopback.
# The device identity and the gateway token are written by bring-sam-online.sh (run once).
SAM_IDENTITY = os.environ.get("SAM_IDENTITY", "/var/lib/vantage-console/agent/sam-device.json")
SAM_TOKEN_FILE = os.environ.get("SAM_TOKEN_FILE", "/var/lib/vantage-console/agent/sam-gateway-token")
SAM_GATEWAY_URL = os.environ.get("SAM_GATEWAY_URL", "ws://127.0.0.1:18789")
# 1.12.0 federation map positions. Server-side, so an arrangement survives a refresh and every
# operator sees the same map; lives under agent/ because that is the one writable subdir.
FEDPOS = os.environ.get("VANTAGE_CONSOLE_FEDPOS", "/var/lib/vantage-console/agent/fedmap-positions.json")
BIND = os.environ.get("VANTAGE_CONSOLE_BIND", "127.0.0.1")
PORT = int(os.environ.get("VANTAGE_CONSOLE_PORT", "8090"))
STALE_AFTER = int(os.environ.get("VANTAGE_CONSOLE_STALE_AFTER", "900"))

# The action catalogue. To extend estate management: add an entry here, its box-side script, and a
# scoped key. `inputs` patterns are enforced on BOTH sides; the box re-validates and never trusts us.
# Ordered safe -> dangerous (the panel renders in this order). Each carries a `risk` (read/write/
# destructive) that drives a coloured tag, so an operator can read the blast radius off the label.
ACTIONS = {
    "tail-logs": {
        "label": "View recent logs", "verb": "tail-logs", "key": "id_action_logs", "group": "box",
        "desc": "Shows the last N lines of a service's journal.",
        "risk": "read", "tag": "Read only", "needs_passphrase": False, "read": True, "result": "read",
        "inputs": [{"name": "unit", "label": "Service", "pattern": r"^[a-z0-9@._-]{1,60}$",
                    "help": "e.g. tak-meshtastic-gateway, takserver-messaging"},
                   {"name": "lines", "label": "Lines", "pattern": r"^[0-9]{1,4}$",
                    "help": "lines to show, e.g. 200"}],
        "confirm": "Show logs for “{unit}” on {target}.",
    },
    "list-certs": {
        "label": "List certificates", "verb": "list-certs", "key": "id_action_certs", "group": "tak", "needs": "takserver",
        "desc": "Lists client certificates with days to expiry.",
        "risk": "read", "tag": "Read only", "needs_passphrase": False, "read": True, "result": "read",
        "inputs": [], "confirm": "List certificates on {target}.",
    },
    "le-renew": {
        "label": "Renew public TLS certificate", "verb": "le-renew", "key": "id_action_lerenew", "group": "tak", "needs": "certbot",
        "desc": "Runs certbot on the public box. No effect unless the certificate is near expiry.",
        "risk": "write", "tag": "Low risk", "needs_passphrase": False, "result": "read",
        "inputs": [], "confirm": "Renew the public TLS certificate on {target}.",
    },
    "restart-service": {
        "label": "Restart service", "verb": "restart-service", "key": "id_action_svc", "group": "box",
        "desc": "Restarts one allowed service. Short outage while it comes back.",
        "risk": "write", "tag": "Brief outage", "needs_passphrase": False, "result": "text",
        "inputs": [{"name": "unit", "label": "Service", "pattern": r"^[a-z0-9@._-]{1,60}$",
                    "help": "e.g. tak-meshtastic-gateway"}],
        "confirm": "Restart “{unit}” on {target}. Brief outage while it restarts.",
    },
    "push-checker": {
        "label": "Update health checker", "verb": "push-checker", "key": "id_action_checker", "group": "software",
        "desc": "Updates a box to the current tak-health so it stops reporting stale results.",
        "risk": "write", "tag": "Low risk", "needs_passphrase": False, "result": "text",
        # The artefact is piped over the forced-command channel with its sha256 as the one
        # argument; the box verifies the hash, syntax-checks, and runs the candidate's own
        # self-test as a gate before installing. The file itself is root-installed under
        # ARTIFACTS, so the console distributes it but cannot modify it.
        "artifact": "tak-health.sh", "timeout": 120,
        "inputs": [], "confirm": "Update the health checker on {target} to the current version.",
    },
    "provision-server": {
        "label": "Provision TAK server", "verb": "provision", "key": "id_action_provision",
        "group": "deploy",
        "desc": "Stands up a complete MilUX TAK Server on the box, non-interactively: harden, "
                "install, database, PKI, CoreConfig, Let's Encrypt, start.",
        "risk": "destructive", "tag": "Builds a server", "needs_passphrase": False,
        "result": "text",
        # The provisioner is pushed per-run (artifact + sha256, the push-checker model) and the
        # parameters travel as ONE base64 JSON argument that the box's priv wrapper re-validates
        # field by field. It runs as a background JOB (an install takes many minutes), streamed
        # to the Deploy page - not through the synchronous action path.
        "artifact": "vantage-tak-provision.sh", "job": True,
        "inputs": [
            {"name": "fqdn", "label": "Server FQDN",
             "pattern": r"^[a-z0-9][a-z0-9-]{0,62}(\.[a-z0-9][a-z0-9-]{0,62}){1,10}$",
             "help": "e.g. tak.example.org - must already resolve to the box for Let's Encrypt"},
            {"name": "le_email", "label": "Let's Encrypt email",
             "pattern": r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
             "help": "expiry notices go here"},
            {"name": "org", "label": "Organisation", "pattern": r"^[A-Za-z0-9._ -]{1,40}$",
             "help": "PKI organisation, e.g. MilUX"},
            {"name": "org_unit", "label": "Org unit", "pattern": r"^[A-Za-z0-9._ -]{1,40}$",
             "help": "e.g. TAK"},
            {"name": "country", "label": "Country", "pattern": r"^[A-Z]{2}$",
             "help": "2-letter code, e.g. GB"},
            {"name": "state", "label": "State/county", "pattern": r"^[A-Za-z ]{2,30}$",
             "help": "e.g. England"},
            {"name": "city", "label": "City", "pattern": r"^[A-Za-z .-]{2,40}$",
             "help": "e.g. Andover"},
            {"name": "deb", "label": "TAK Server .deb path",
             "pattern": r"^/[A-Za-z0-9._/-]{4,200}\.deb$",
             "help": "already on the target box, e.g. /root/takserver_5.7-RELEASE43_all.deb"},
            {"name": "components", "label": "Extra components",
             # must match every token the provisioner's components stage can install
             # (the case dispatch in vantage-tak-provision.sh); the box re-validates and
             # rejects anything unknown. Keep the two lists in step - drift here is what
             # made the Modules Install button fail with "invalid components".
             "pattern": r"^((cloudtak|mediamtx|maps|mosquitto|nodered|ollama|takbot|lanntp)"
                        r"(,(cloudtak|mediamtx|maps|mosquitto|nodered|ollama|takbot|lanntp))*)?$",
             "help": "optional module tokens: cloudtak, mediamtx, maps, mosquitto, nodered, "
                     "ollama, takbot, lanntp - or leave empty"},
            {"name": "ca_pass", "label": "Certificate password",
             "pattern": r"^$|^[A-Za-z0-9._!-]{8,64}$", "optional": True, "secret": True,
             "input_type": "password",
             "help": "typed into devices when importing certificates. Leave blank for a "
                     "strong generated one (recommended); choose a simple one only for "
                     "an experimental build"},
            {"name": "dry_run", "label": "Dry run", "pattern": r"^[01]$",
             "help": "1 = print every step without changing the box; run this first"},
            {"name": "stage", "label": "Stage", "pattern": r"^(components)?$",
             "hidden": True,
             "help": "empty = full build; components = install extras on a built box"},
        ],
        "confirm": "Provision a TAK server on {target} as {fqdn}. This installs software, "
                   "creates a PKI and opens firewall ports on that box.",
    },
    "update-cloudtak": {
        "label": "Update CloudTAK stack", "verb": "update-cloudtak", "key": "id_action_cloudtak", "group": "software", "needs": "cloudtak",
        "desc": "Applies the pinned CloudTAK compose on the box: pulls pinned images and recreates changed containers.",
        "risk": "write", "tag": "Service refresh", "needs_passphrase": False, "result": "read",
        "timeout": 420,
        "inputs": [], "confirm": "Apply the pinned CloudTAK stack on {target}. Changed containers restart.",
    },
    "load-images": {
        "label": "Deploy images offline", "verb": "load-images", "key": "id_action_loadimg",
        "group": "software", "needs": "cloudtak",
        "desc": "Loads a saved CloudTAK/MediaMTX image tarball from the admin box and applies the "
                "pinned compose - no internet. The offline alternative to Update CloudTAK; the "
                "console streams the tarball over the box's scoped key and it is docker-loaded.",
        "risk": "write", "tag": "Offline deploy", "needs_passphrase": False, "result": "read",
        "timeout": 3600, "catalogue": False, "inputs": [],
    },
    "deploy-mesh-gateway": {
        "label": "Deploy Meshtastic TAK gateway", "verb": "deploy-mesh-gateway",
        "key": "id_action_meshdeploy", "group": "network", "needs": "takserver",
        "desc": "Installs the vendored Meshtastic TAK gateway on the box: its own venv from "
                "the shipped artefact, the systemd unit on the chosen radio, the TAK UDP "
                "input created with its filter group, the gateway joined to the chosen "
                "channel. TAK Server restarts once to pick up the input.",
        "risk": "destructive", "tag": "Installs the gateway", "needs_passphrase": False,
        "result": "text",
        # The provision-server transport: artefact + sha256 streamed over the scoped key,
        # every input re-validated box-side, run as a job with a streamed log. The channel
        # URL (which embeds the PSK) is resolved console-side from the channel store and
        # injected into the job args - it never appears in a page, a URL or the audit.
        "artifact": "vantage-mesh-gateway-install.sh", "job": True, "timeout": 900,
        "inputs": [
            {"name": "serial", "label": "Radio serial device",
             "pattern": r"^/dev/serial/by-id/[A-Za-z0-9._:+=-]{4,180}$",
             "help": "the radio's stable by-id path on the box, from "
                     "ls -l /dev/serial/by-id/ - never a bare ttyACM number, those "
                     "shuffle on reboot"},
            {"name": "region", "label": "Region", "pattern": r"^(EU_868|US)$",
             "help": "the LoRa regulatory region the radios transmit in. This is a legal "
                     "setting: pick the region for the country you operate in, and "
                     "re-check before transmitting abroad"},
            {"name": "channel", "label": "Channel", "pattern": r"^[A-Za-z0-9_-]{1,11}$",
             "help": "a channel created on the Networks page; the gateway joins it"},
            {"name": "filter_group", "label": "TAK filter group",
             "pattern": r"^[A-Za-z0-9_-]{1,40}$",
             "help": "the TAK group mesh traffic belongs to. Only clients whose "
                     "certificate carries this group see mesh markers - set at input "
                     "creation because the input manager cannot add it later"},
            {"name": "bundle_file", "label": "Gateway bundle from the Store shelf",
             "pattern": r"^[A-Za-z0-9._+-]{1,80}\.tgz$",
             "help": "a bundle cut with cut-bundle.sh (vendored gateway plus wheels, "
                     "ADR-002), uploaded to Store > mesh-gateway"}],
        "push_key": "id_action_pkgpush",
        "confirm": "Deploy the Meshtastic TAK gateway to {target}: radio {serial}, region "
                   "{region}, channel {channel}. Installs the gateway, creates the TAK "
                   "input with filter group {filter_group}, and starts the service. TAK "
                   "Server restarts once - a brief outage.",
    },
    "apply-mesh-channel": {
        "label": "Apply mesh channel to gateway", "verb": "apply-mesh-channel",
        "key": "id_action_meshchan", "group": "network", "needs": None,
        "desc": "Rewrites the gateway radio to a channel from the Networks page: the "
                "gateway service stops (it holds the radio), the channel is written, the "
                "service restarts. Trackers and phones rejoin by scanning the channel's "
                "current QR.",
        "risk": "write", "tag": "Brief mesh outage", "needs_passphrase": False,
        "result": "text",
        "artifact": "vantage-mesh-gateway-install.sh", "job": True, "timeout": 300,
        "fixed_args": {"stage": "channel"},
        "inputs": [
            {"name": "channel", "label": "Channel", "pattern": r"^[A-Za-z0-9_-]{1,11}$",
             "help": "a channel created on the Networks page"}],
        "confirm": "Apply channel {channel} to the gateway on {target}. The gateway stops, "
                   "the radio is rewritten, the gateway restarts: a brief mesh outage, and "
                   "devices on the old channel drop until they rejoin.",
    },
    "destroy-server": {
        "label": "Destroy this server", "verb": "destroy", "key": "id_action_destroy",
        "group": "box", "needs": None,
        "desc": "ERASE the estate footprint from the box - TAK Server, its certificates and "
                "database, the CloudTAK/MediaMTX stack and volumes, the vault. Irreversible, and "
                "needs the box reachable. The console gates it with a typed DESTROY and the "
                "operator password.",
        "risk": "write", "tag": "Destroy", "needs_passphrase": True, "result": "text",
        "timeout": 600, "catalogue": False, "inputs": [],
    },
    "console-mode": {
        "label": "Set this box's console role", "verb": "console-mode",
        "key": "id_action_conmode", "group": "box", "needs": None,
        "desc": "Promote or demote the Vantage console running ON this box between admin "
                "(full estate control) and client (watch and operate, never reshape), from "
                "here. The box must run its own Vantage console. Driven by the Console role "
                "control on this page, gated with the operator password.",
        "risk": "write", "tag": "Changes console role", "needs_passphrase": True,
        "result": "text", "catalogue": False, "inputs": [],
    },
    "kiosk": {
        "label": "Manage this box's kiosk screen", "verb": "kiosk",
        "key": "id_action_kiosk", "group": "box", "needs": None,
        "desc": "For a box with a screen attached. Install the boot-to-console kiosk (cage and "
                "a browser) so the screen shows this box's own Vantage console, then turn it on "
                "or off from here - off hands the screen back to a normal text login. Driven by "
                "the Kiosk control on this page, gated with the operator password.",
        "risk": "write", "tag": "Manages the kiosk screen", "needs_passphrase": True,
        "result": "text", "catalogue": False, "inputs": [],
    },
    "upgrade-server": {
        "label": "Upgrade TAK Server", "verb": "upgrade-server", "key": "id_action_upgrade",
        "group": "software", "needs": "takserver",
        "desc": "Upgrades TAK Server in place from a package on the Store shelf: backup of "
                "CoreConfig and certs, stop, install, database migration, start. First "
                "installs go through Deploy - this action refuses an empty box.",
        "risk": "destructive", "tag": "Upgrades TAK", "needs_passphrase": False,
        "result": "text",
        # Two-phase JOB: the package streams to the box's staging inbox behind the
        # push key (hash-verified both ends), then the upgrade runs behind its own key
        # and root wrapper. Neither phase goes through the synchronous action path.
        "job": True, "push_key": "id_action_pkgpush",
        "inputs": [
            {"name": "deb_file", "label": "Package from the Store shelf",
             "pattern": r"^[A-Za-z0-9._-]{1,80}\.deb$",
             "help": "a .deb already in Store > tak-server"},
            {"name": "deb_sha256", "label": "Package sha256",
             "pattern": r"^[0-9a-f]{64}$",
             "help": "as recorded on the shelf - verified before push and again on the box"},
        ],
        "confirm": "Upgrade TAK Server on {target} using {deb_file}. The service stops "
                   "during the upgrade; CoreConfig and certificates are backed up first.",
    },
    "provision-infratak": {
        "label": "Deploy infra-TAK stack", "verb": "provision", "key": "id_action_provision",
        "group": "deploy",
        "desc": "Stand up infra-TAK (takwerx) on an enrolled box: its browser platform on "
                "port 5001, from which its own marketplace deploys the TAK ecosystem. Offered "
                "as a stack CHOICE alongside the MilUX TAK Server; Vantage still manages the "
                "box above it.",
        "risk": "destructive", "tag": "Builds a server", "needs_passphrase": False,
        "result": "text", "artifact": "vantage-infratak-provision.sh", "job": True,
        "inputs": [
            {"name": "ref", "label": "infra-TAK version",
             "pattern": r"^[A-Za-z0-9._/-]{0,60}$",
             "help": "a tag or branch to pin, e.g. main - leave empty for main"},
            {"name": "dry_run", "label": "Dry run", "pattern": r"^[01]$",
             "help": "1 = print every step without changing the box; run this first"},
        ],
        "confirm": "Deploy infra-TAK on {target}. This installs Docker and infra-TAK's "
                   "platform on that box.",
    },
    "harden-plan": {
        "label": "Read security posture", "verb": "harden", "key": "id_action_harden",
        "group": "box", "needs": None, "read": True,
        "desc": "Read whether this box is at Standard or Hardened posture, and which controls are on.",
        "risk": "read", "tag": "Read-only", "needs_passphrase": False, "result": "text",
        "fixed_args": {"mode": "plan"}, "inputs": [], "confirm": "",
    },
    "harden-apply": {
        "label": "Harden this box", "verb": "harden", "key": "id_action_harden",
        "group": "box", "needs": None,
        "desc": "Move this box to Hardened posture: SSH hardened (root stays key-only), kernel "
                "sysctl, auditd, fail2ban, core dumps off. Reversible; every file backed up first; "
                "sshd is validated before reload so you are never locked out.",
        "risk": "destructive", "tag": "Changes the box", "needs_passphrase": True, "result": "text",
        "fixed_args": {"mode": "apply"}, "inputs": [],
        "confirm": "Harden {target}. SSH, kernel and services are changed on that box "
                   "(root SSH stays key-only). Reversible from here.",
    },
    "harden-revert": {
        "label": "Revert to Standard", "verb": "harden", "key": "id_action_harden",
        "group": "box", "needs": None,
        "desc": "Undo hardening: restore the backed-up sshd config and remove the hardening "
                "drop-ins. Installed packages (auditd, fail2ban) are left in place.",
        "risk": "write", "tag": "Changes the box", "needs_passphrase": True, "result": "text",
        "fixed_args": {"mode": "revert"}, "inputs": [],
        "confirm": "Revert {target} to Standard posture.",
    },
    "firewall-plan": {
        "label": "Read firewall drift", "verb": "firewall", "key": "id_action_firewall",
        "group": "box", "needs": None, "read": True,
        "desc": "Read the firewall against this box's declared policy: which TAK ports are open, "
                "missing, or exposed to Anywhere when they should be tailnet-only.",
        "risk": "read", "tag": "Read-only", "needs_passphrase": False, "result": "text",
        "fixed_args": {"mode": "plan"}, "inputs": [], "confirm": "",
    },
    "firewall-apply": {
        "label": "Reconcile firewall", "verb": "firewall", "key": "id_action_firewall",
        "group": "box", "needs": None,
        "desc": "Bring the firewall in line with policy: open declared TAK ports at the right "
                "scope, and on a private box remove stray Anywhere rules. Port 22 is never "
                "touched; a human's own extra rules are left alone.",
        "risk": "write", "tag": "Changes the box", "needs_passphrase": True, "result": "text",
        "fixed_args": {"mode": "apply"}, "inputs": [],
        "confirm": "Reconcile the firewall on {target} to its declared policy.",
    },
    "issue-cert": {
        "label": "Issue client certificate", "verb": "issue-cert", "key": "id_action_cert", "group": "tak", "needs": "takserver",
        "desc": "Creates a client certificate and returns a .p12 to download.",
        "risk": "write", "tag": "New credential", "needs_passphrase": True, "result": "p12",
        "inputs": [{"name": "name", "label": "Certificate name",
                    "pattern": r"^[A-Za-z0-9._-]{1,40}$",
                    "help": "e.g. alpha-1  (letters, digits, . _ - ; max 40)"}],
        "confirm": "Issue certificate “{name}” on {target}. This creates a new credential.",
    },
    "enrol-device": {
        "label": "Enrol device (QR)", "verb": "enrol-device", "key": "id_action_enrol", "group": "tak", "needs": "takserver",
        "desc": "Creates an enrolment credential and returns a QR code a phone scans to join this server.",
        "risk": "write", "tag": "New credential", "needs_passphrase": True, "result": "img",
        "inputs": [{"name": "user", "label": "Username", "pattern": r"^[A-Za-z0-9._-]{1,40}$",
                    "help": "device or person, e.g. alpha-1 (no leading dash)"},
                   {"name": "group", "label": "Group", "pattern": r"^[A-Za-z0-9_-]{1,40}$",
                    "help": "the team this device belongs to; devices see only their own "
                            "group's traffic"},
                   {"name": "password_b64", "label": "Device password",
                    "pattern": r"^[A-Za-z0-9+/=]{0,88}$", "optional": True,
                    "secret": True, "encode": "b64", "input_type": "password",
                    "help": "leave blank and a strong one is generated; if you choose "
                            "your own: 15+ chars with an upper, lower, digit and symbol "
                            "(! . _ ~ -)"}],
        "confirm": "Create enrolment credential \u201c{user}\u201d in group \u201c{group}\u201d on {target}. This mints a device credential; an existing user\u2019s groups and password are replaced.",
    },
    "deploy-console": {
        "label": "Install a console on this box", "verb": "install-console",
        "key": "id_action_install_console", "group": "network", "needs": "",
        "desc": "Install a Vantage console (deployed edition) on this box, so it can "
                "manage itself and take part in estate sync. Same console version this "
                "one runs, pushed and installed in place; nothing is fetched from the "
                "internet.",
        "more": "<b>Choosing who can reach it:</b> <b>this box's estate address</b> is the "
                "address this console already reaches the box on - no addresses to work "
                "out. If the box is joined to a VPN (WireGuard, Tailscale or OpenVPN), "
                "that <i>is</i> its VPN address, so everyone on that private network can "
                "open the new console; if the VPN is off, it is unreachable from outside, "
                "exactly as the box itself is. <b>This box only</b> suits a box with its "
                "own screen - pair it with the kiosk option. Boxes join a VPN from "
                "<a href='/operations#ag-network'>Operations &rsaquo; Network</a> "
                "(VPN join / status / leave).",
        "risk": "write", "tag": "Installs software", "needs_passphrase": True,
        "read": False, "result": "text", "gen_artifact": "console",
        "inputs": [{"name": "bind",
                    "label": "Reachable from",
                    "pattern": r"^(estate|local|all|[A-Za-z0-9.:_-]{1,64})?$",
                    "choices": [("estate", "this box's estate address (recommended)"),
                                ("local", "this box only - its own screen / kiosk"),
                                ("all", "every network the box is on")],
                    "help": "who can open the new console. The address is worked out for "
                            "you; the box's firewall still applies.",
                    "optional": True},
                   {"name": "kiosk",
                    "label": "Boot to the console (kiosk)",
                    "pattern": r"^(yes|no)?$",
                    "choices": ["no", "yes"],
                    "help": "for a box with a screen: install a full-screen browser "
                            "that boots straight to this console, so powering the box "
                            "on lands on Vantage. Takes over the display.",
                    "optional": True}],
        "confirm": "Install a Vantage console on {target}. Software is installed and a "
                   "service starts on that box.",
        "timeout": 180,
    },
    "vpn-status": {
        "label": "VPN status", "verb": "vpn", "key": "id_action_vpn",
        "group": "network", "needs": "",
        "desc": "The private-network state of this box for one provider: interface, "
                "address, and whether the network answers.",
        "risk": "read", "tag": "Read only", "needs_passphrase": False, "read": True,
        "result": "text",
        "inputs": [{"name": "provider", "label": "Provider",
                    "pattern": r"^(wireguard|tailscale|openvpn)$",
                    "choices": ["wireguard", "tailscale", "openvpn"],
                    "help": "which VPN to report on"}],
        "fixed_args": {"mode": "status"},
        "confirm": "Read VPN status on {target}.",
    },
    "vpn-join": {
        "label": "Join a private network", "verb": "vpn", "key": "id_action_vpn",
        "group": "network", "needs": "",
        "desc": "Installs the provider if needed and joins this box to your private "
                "network. The join material is a secret: sent once, written root-only "
                "on the box, never logged.",
        "risk": "write", "tag": "Changes networking", "needs_passphrase": True,
        "read": False, "result": "text",
        "inputs": [{"name": "provider", "label": "Provider",
                    "pattern": r"^(wireguard|tailscale|openvpn)$",
                    "choices": ["wireguard", "tailscale", "openvpn"],
                    "help": "WireGuard: paste a wg-quick config. Tailscale: paste a "
                            "one-use auth key. OpenVPN: paste the client .ovpn with "
                            "inline certificates."},
                   {"name": "conf", "label": "Join material",
                    "pattern": r"^[A-Za-z0-9+/=_-]{4,65536}$",
                    "encode": "b64", "secret": True, "kind": "textarea",
                    "help": "treated as a secret; what to paste depends on the "
                            "provider - see its hint above"}],
        "fixed_args": {"mode": "join"},
        "confirm": "Join {target} to the chosen private network. Networking on that "
                   "box changes; existing connections are untouched but new routes "
                   "appear.",
    },
    "vpn-leave": {
        "label": "Leave the private network", "verb": "vpn", "key": "id_action_vpn",
        "group": "network", "needs": "",
        "desc": "Brings the provider's interface down, disables it at boot, and "
                "shreds the join material on the box.",
        "risk": "write", "tag": "Changes networking", "needs_passphrase": True,
        "read": False, "result": "text",
        "inputs": [{"name": "provider", "label": "Provider",
                    "pattern": r"^(wireguard|tailscale|openvpn)$",
                    "choices": ["wireguard", "tailscale", "openvpn"],
                    "help": "which VPN to leave"}],
        "fixed_args": {"mode": "leave"},
        "confirm": "Leave the chosen private network on {target}. If you manage the "
                   "box OVER that network, you will lose this console's path to it.",
    },
    "show-cert-password": {
        "label": "Show certificate password", "verb": "capass", "key": "id_action_capass",
        "group": "tak", "needs": "takserver",
        "desc": "Reveals this box's certificate password - what a device asks for when "
                "importing a .p12 issued here. A secret, so your operator password is "
                "required and the reveal is audited.",
        "risk": "write", "tag": "Reveals a secret", "needs_passphrase": True, "read": False,
        "result": "capass", "inputs": [],
        "confirm": "Reveal the certificate password for {target}. It appears on screen "
                   "and the reveal is logged.",
    },
    "set-loadout": {
        "label": "Declare the box's loadout", "verb": "set-loadout", "key": "id_action_loadout",
        "group": "box", "needs": "",
        "desc": "Declares which modules this box actually carries, so its health checks "
                "judge what is fitted and SKIP what is not - visibly, never silently. "
                "Removing a module from the declaration reduces monitoring coverage, so "
                "the whole action asks for your operator password and the box logs a "
                "before/after line. 'clear' returns to the profile's full expectations.",
        "risk": "write", "tag": "Changes monitoring", "needs_passphrase": True, "read": False,
        "result": "text",
        "inputs": [{"name": "components", "label": "Components",
                    "pattern": r"^(clear|[a-z]+(,[a-z]+)*)$",
                    "help": "comma list from: takserver cloudtak mediamtx maps takbot "
                            "mosquitto nodered ollama lanntp mesh tailscale docker "
                            "(takserver required), or 'clear'"},
                   {"name": "tilesets", "label": "Expected tilesets",
                    "pattern": r"^([0-9]{1,3}|-)?$",
                    "help": "how many tilesets the maps module should serve; blank or - for no count"}],
        "confirm": "Declare the loadout of {target} as “{components}”. Health checks "
                   "will judge exactly this set; anything removed stops being watched.",
    },
    "list-credentials": {
        "label": "List credentials", "verb": "list-credentials", "key": "id_action_credlist",
        "group": "tak", "needs": "takserver",
        "desc": "Lists the device credentials issued on this box: client certificates and stored "
                "enrolment tokens. Names and dates only, no secret material.",
        "risk": "read", "tag": "Read only", "needs_passphrase": False, "read": True,
        "result": "credlist", "inputs": [],
        "confirm": "List device credentials on {target}.",
    },
    "fetch-credential": {
        "label": "Download credential", "verb": "fetch-credential", "key": "id_action_credfetch",
        "group": "tak", "needs": "takserver",
        "desc": "Returns one issued credential for re-download: a certificate .p12, or an "
                "enrolment token with its QR rebuilt from the current host.",
        "risk": "read", "tag": "Returns a secret", "needs_passphrase": True, "result": "cred",
        "inputs": [{"name": "ctype", "label": "Kind", "pattern": r"^(cert|enrol)$",
                    "choices": ["cert", "enrol"],
                    "help": "cert or enrol"},
                   {"name": "name", "label": "Name", "pattern": r"^[A-Za-z0-9._-]{1,40}$",
                    "help": "the credential's name"}],
        "confirm": "Download the {ctype} credential “{name}” from {target}.",
    },
    "view-coreconfig": {
        "label": "View CoreConfig", "verb": "view-coreconfig", "key": "id_action_cfgview", "group": "tak", "needs": "takserver",
        "desc": "Shows a redacted summary of the server's CoreConfig: inputs, connectors, TLS, auth, federation. Never shows passwords.",
        "risk": "read", "tag": "Read only", "needs_passphrase": False, "read": True, "result": "read",
        "inputs": [], "confirm": "Show the CoreConfig summary for {target}.",
    },
    "backup-coreconfig": {
        "label": "Back up CoreConfig", "verb": "backup-coreconfig", "key": "id_action_cfgbak", "group": "tak", "needs": "takserver",
        "desc": "Copies CoreConfig and the user file to a timestamped, root-only backup on the box. Changes nothing live.",
        "risk": "write", "tag": "Low risk", "needs_passphrase": False, "result": "text",
        "inputs": [], "confirm": "Back up CoreConfig and the user file on {target}.",
    },
    "set-group": {
        "label": "Add user to group", "verb": "set-group", "key": "id_action_group", "group": "tak", "needs": "takserver",
        "desc": "Adds a user to a group. Never removes. Takes effect on their next reconnect.",
        "risk": "write", "tag": "Access change", "needs_passphrase": True, "result": "text",
        "inputs": [{"name": "user", "label": "User", "pattern": r"^[A-Za-z0-9._-]{1,40}$",
                    "help": "username as listed in UserAuthenticationFile"},
                   {"name": "group", "label": "Group", "pattern": r"^[A-Za-z0-9_-]{1,40}$",
                    "help": "an existing group name, or __ANON__ for the open channel"}],
        "confirm": "Add “{user}” to group “{group}” on {target}.",
    },
    "revoke-cert": {
        "label": "Revoke client certificate", "verb": "revoke-cert", "key": "id_action_revoke", "group": "tak", "needs": "takserver",
        "desc": "Revokes a certificate and adds it to the CRL. That client can no longer connect.",
        "risk": "destructive", "tag": "Destructive", "needs_passphrase": True, "result": "text",
        "inputs": [{"name": "name", "label": "Certificate name",
                    "pattern": r"^[A-Za-z0-9._-]{1,40}$",
                    "help": "exact certificate name"}],
        "confirm": "Revoke certificate “{name}” on {target}. {name} will stop connecting. This cannot be undone here.",
    },
    "federation-ca": {
        "label": "Show federation CA", "verb": "federation-ca", "key": "id_action_fedca", "group": "federation", "needs": "takserver",
        "desc": "Shows this server's CA certificate — hand it to a partner so they can trust your federation link.",
        "risk": "read", "tag": "Read only", "needs_passphrase": False, "read": True, "result": "read",
        "inputs": [], "confirm": "Show the federation CA for {target}.",
    },
    "federation-enable": {
        "label": "Enable federation", "verb": "federation-enable", "key": "id_action_feden", "group": "federation", "needs": "takserver",
        "desc": "Turns on the v2 federation server (port 9001) and opens it in the firewall. Manual group mapping; brief restart.",
        "risk": "write", "tag": "Brief outage", "needs_passphrase": False, "result": "text",
        "inputs": [], "confirm": "Enable the federation server on {target}. Brief restart while it comes back.",
    },
    "federation-connect": {
        "label": "Add federation link", "verb": "federation-connect", "key": "id_action_fedconn", "group": "federation", "needs": "takserver",
        "desc": "Adds an outbound federation link to a partner's v2 port. Trust must be exchanged both ways first.",
        "risk": "write", "tag": "Brief outage", "needs_passphrase": False, "result": "text",
        "inputs": [{"name": "displayName", "label": "Partner name", "pattern": r"^[A-Za-z0-9._-]{1,48}$",
                    "help": "a label for the partner, e.g. Cloud"},
                   {"name": "address", "label": "Partner address", "pattern": r"^[a-z0-9.-]{1,253}$",
                    "help": "hostname or IP of the partner server"},
                   {"name": "port", "label": "Port", "pattern": r"^[0-9]{2,5}$",
                    "help": "the partner's federation port, usually 9001"}],
        "confirm": "Federate {target} → “{displayName}” ({address}:{port}). Brief restart.",
    },
    "federation-groups": {
        "label": "Share federation group", "verb": "federation-groups", "key": "id_action_fedgroups",
        "group": "federation", "needs": "takserver",
        "desc": "Names the group this box shares with its trusted federation partners: written "
                "into every federate entry and as a default for every trusted partner CA. A live "
                "link with no shared group carries nothing - TAK creates federate entries empty.",
        "risk": "write", "tag": "Traffic crosses", "needs_passphrase": False, "result": "text",
        "inputs": [{"name": "group", "label": "Group", "pattern": r"^[A-Za-z0-9_-]{1,40}$",
                    "help": "the group to share both ways, e.g. MilUX. Run on BOTH sides of a link."}],
        "confirm": "Share group “{group}” with ALL of {target}'s trusted federation partners. "
                   "Restarts once, only if something changes.",
    },
}

# ---------- read-only view helpers (unchanged from 0.2.0) --------------------------------------
def age_seconds(iso):
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return None


def human_age(sec):
    if sec is None:
        return "unknown"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h {(sec % 3600) // 60}m ago"
    return f"{sec // 86400}d ago"


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, f"no state file at {STATE} - has the collector run?"
    except Exception as e:
        return None, f"cannot read state: {e}"


def load_history(max_lines=9000):
    try:
        with open(HISTORY) as fh:
            lines = fh.read().splitlines()
    except Exception:
        return []
    out = []
    for raw in lines[-max_lines:]:
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def series_for(history, target, key, points=96):
    vals = []
    for line in history:
        for t in line.get("targets", []):
            if t.get("name") == target:
                v = (t.get("values") or {}).get(key)
                if isinstance(v, (int, float)):
                    vals.append(v)
    return vals[-points:]


def verdicts_for(history, target, points=48):
    out = []
    for line in history:
        for t in line.get("targets", []):
            if t.get("name") == target:
                out.append(t.get("result") or "UNKNOWN")
    return out[-points:]


def sparkline(vals, w=180, h=26):
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    step = w / (len(vals) - 1)
    pts = " ".join(f"{i*step:.1f},{h-2-(v-lo)/(hi-lo)*(h-5):.1f}" for i, v in enumerate(vals))
    area = f"0,{h} " + pts + f" {w},{h}"
    return (f"<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none' aria-hidden='true'>"
            f"<polygon points='{area}' fill='var(--brand)' opacity='.08'/>"
            f"<polyline points='{pts}' fill='none' stroke='var(--blue)' stroke-width='1.6' "
            f"stroke-linejoin='round'/></svg>")


def last_seen(history, target):
    for line in reversed(history):
        for t in line.get("targets", []):
            if t.get("name") == target and t.get("reachable"):
                return line.get("ts")
    return None


def changed_since_last(history, target):
    res = []
    for line in history[-2:]:
        for t in line.get("targets", []):
            if t.get("name") == target:
                res.append(t.get("result"))
    if len(res) == 2 and res[0] != res[1]:
        return f"{res[0]} → {res[1]}"
    return ""


def uptime_strip(history, name, n=48, mini=False):
    v = verdicts_for(history, name, n)
    if not v:
        return ""
    cells = "".join(f"<i class='{html.escape(x)}' title='{html.escape(x)}'></i>" for x in v)
    return f"<div class='{'strip mini' if mini else 'strip'}'>{cells}</div>"


# ---------- actions ----------------------------------------------------------------------------
def load_actions_config():
    """Actions are OFF unless this file exists. Returns the config or None (read-only console)."""
    try:
        with open(ACTIONS_CONFIG) as fh:
            return json.load(fh)
    except Exception:
        return None


def enabled_actions(cfg):
    if not cfg:
        return []
    en = set(cfg.get("enabled", []))
    return [a for a in ACTIONS if a in en]   # registry order = safe -> dangerous


# ---------- desired state (1.1.0) --------------------------------------------------------------
# The estate baseline: which version each managed component SHOULD be. The inventory says what IS;
# this file says what OUGHT to be; the console renders the difference and offers the matching
# gated action. Root-installed config, absent = no drift judgements, only facts.
def load_desired():
    try:
        with open(DESIRED) as fh:
            return json.load(fh).get("components", {}) or None
    except Exception:
        return None


def version_current(reported, want):
    """A reported version matches the baseline exactly, or is the baseline plus a
    suffix (git describe's `v13.70.0-1-gabc` is still v13.70.0). Unknown never
    drifts: an empty version is a visibility gap, not an update to push."""
    r, w = (reported or "").strip(), (want or "").strip()
    if not r or not w:
        return True
    return r == w or r.startswith(w + "-") or r.startswith(w + "+")


def software_rows(t):
    """A box's inventory, with the bootstrap case covered: a checker too old to
    report software still reports its own version through the collector, and THAT
    is the drift that matters most - it is what the update fixes. Without this,
    an estate of old checkers shows no drift at all, which buried the one
    signpost the operator needed on day one."""
    rows = t.get("software") or []
    if not rows and t.get("checker_version"):
        rows = [{"name": "tak-health", "version": t.get("checker_version"),
                 "state": "installed (no inventory until updated)"}]
    return rows


def software_drift(state, desired):
    """[(target, label, component, reported, want), ...] across the estate."""
    out = []
    for t in state.get("targets", []):
        for s in software_rows(t):
            want = (desired or {}).get(s.get("name"))
            if want and not version_current(s.get("version"), want):
                out.append((t.get("name"), t.get("label", t.get("name")),
                            s.get("name"), (s.get("version") or "?"), want))
    return out


# Which gated action converges which component. Only components listed here get an
# Update control on their drift row; everything else is information.
COMPONENT_ACTION = {"tak-health": "push-checker", "cloudtak": "update-cloudtak",
                    "takserver": "upgrade-server"}


# ---------- service links (1.5.0) --------------------------------------------------------------
# The web endpoints each box serves, as clickable links. The host is link_host (the reachable
# name the console already uses - a tailnet name or the public FQDN, so links work from the
# operator's browser). Which links show is driven by the software inventory: TAK admin is on
# every TAK server; CloudTAK and the media player appear only where the box runs them.
def service_links(t):
    host = t.get("link_host") or t.get("fqdn")
    if not host:
        return []
    sw = {s.get("name") for s in (t.get("software") or [])}
    le = t.get("profile") == "cloud"   # only the public box has a browser-trusted (LE) cert
    out = []
    if "takserver" in sw or not sw:
        # 8446 is browser-trusted where Let's Encrypt is configured (the public box);
        # 8443 is TAK's own cert everywhere (expect a browser warning, and it wants the
        # admin client certificate).
        if le:
            out.append(("TAK admin / WebTAK", f"https://{host}:8446",
                        "browser-trusted (Let's Encrypt); needs the admin client cert"))
        out.append(("TAK admin / WebTAK", f"https://{host}:8443",
                    "TAK's own cert (expect a warning); needs the admin client cert"))
    if "cloudtak" in sw:
        out.append(("CloudTAK", f"http://{host}:5000", "browser-based TAK client"))
    if "mediamtx" in sw:
        out.append(("Media player (HLS)", f"http://{host}:8888", "video restreamer"))
    return out


# ---------- supervised recovery (1.4.0) --------------------------------------------------------
# Our answer to infra-TAK's Guard Dog, kept inside the doctrine: DETECT, PROPOSE, one CLICK to
# heal - never silent, never automatic. When a check fails in a way a catalogue action can fix,
# the server page offers that action pre-filled next to the failing row. The operator still
# reads the confirm and still clicks. The console never acts on its own initiative.
def recovery_for(check):
    """A failing check -> (action_id, {input: value}) that would heal it, or None. The map is
    deliberately small and specific; a proposal we are not sure about is worse than none."""
    cat = (check.get("category") or "").lower()
    name = check.get("name") or ""
    status = check.get("status")
    if status not in ("FAIL", "WARN"):
        return None
    # A dead peripheral service -> restart it (the box still enforces its own allowlist, so a
    # proposal to restart something off-list simply gets a clean refusal).
    if cat == "service" and status == "FAIL" and "@boot" not in name:
        return ("restart-service", {"unit": name})
    if cat == "media" and status == "FAIL":
        return ("restart-service", {"unit": "mediamtx"})
    return None


def verify_passphrase(cfg, given):
    pw = (cfg or {}).get("passphrase") or {}
    want = pw.get("sha256", "")
    if not want:
        return False
    got = hashlib.sha256(((pw.get("salt", "")) + (given or "")).encode()).hexdigest()
    return hmac.compare_digest(got, want)


def audit(entry):
    try:
        with open(AUDIT, "a") as fh:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 **entry}) + "\n")
    except Exception:
        pass


def agent_activity(limit=40):
    """The tail of the audit log that belongs to an agent - tool reads, proposals and acts,
    attributed to whichever connection made them. Lets an operator watch the agent work."""
    rows = []
    try:
        with open(AUDIT) as fh:
            lines = fh.readlines()[-4000:]
    except Exception:
        return []
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        act = e.get("action", "")
        via = str(e.get("source", "") or "") + " " + str(e.get("client", "") or "")
        is_agent = (act in ("mcp-tool", "agent-connect", "agent-revoke", "agent-key",
                            "agent-autonomy", "agent-key-clear")
                    or "mcp:" in via or "chat:" in via or "openclaw" in via)
        if not is_agent:
            continue
        rows.append(e)
        if len(rows) >= limit:
            break
    return rows


def validate_action_request(aid, target, inputs, cfg):
    """The shared gate for BOTH executing an action (run_action) and accepting a proposed one
    (ingest_proposal). It answers one question - is this a real, enabled, well-formed request
    for a known action against a known target - and never trusts its caller, because one caller
    is a language model. Returns (ok, err, argv, safe_inputs); argv is the positional args the
    forced command will receive, in the action's declared input order."""
    if aid not in ACTIONS:
        return False, "unknown action", None, None
    if aid not in enabled_actions(cfg):
        return False, "action not enabled", None, None
    a = ACTIONS[aid]
    if target not in (cfg or {}).get("targets", {}):
        return False, "unknown target", None, None
    argv = []
    for f in a["inputs"]:
        v = str((inputs or {}).get(f["name"], ""))
        if not re.match(f["pattern"], v):
            return False, f"invalid {f['name']}", None, None
        argv.append(v)
    # fixed_args are the action's own trailing positional args - a fixed mode word the
    # console (not the caller) supplies. Several actions share one forced-command key and
    # dispatch on this (harden plan|apply|revert, firewall plan|apply). They are trusted
    # because they come from the registry, not the request; the box's script allowlists
    # them regardless.
    for fa in (a.get("fixed_args") or {}).values():
        argv.append(str(fa))
    safe_inputs = {}
    for f in a["inputs"]:
        v = (inputs or {}).get(f["name"])
        safe_inputs[f["name"]] = "(secret)" if (f.get("secret") and v) else v
    return True, None, argv, safe_inputs


def resolve_artifact(name):
    """Find a provisioner/artifact in ARTIFACTS. A console migrated from the pre-2.0
    milux-console namespace kept these files under their old milux- names (the migration
    moved the directory, not the files inside it), while the 2.0 action registry looks for
    the vantage- name - so fall back to the old name. Returns the path, or None when
    neither is present."""
    p = os.path.join(ARTIFACTS, name)
    if os.path.exists(p):
        return p
    if name.startswith("vantage-"):
        legacy = os.path.join(ARTIFACTS, "milux-" + name[len("vantage-"):])
        if os.path.exists(legacy):
            return legacy
    return None


def artifact_missing_error(a):
    """A human error for a missing provisioner: names what could not run and what to do,
    instead of a raw '[Errno 2] No such file or directory' path."""
    return {"error": 'This console can\'t run "' + str(a.get("label", "this action"))
            + '": the installer script it needs (' + str(a.get("artifact", "")) + ") is not "
            "on this console. Re-run the console installer on this box to restore it, then "
            "try again."}


def run_action(aid, target, inputs, passphrase, confirm, client, passphrase_ok=False):
    cfg = load_actions_config()
    ok, err, argv, safe_inputs = validate_action_request(aid, target, inputs, cfg)
    if not ok:
        return (403 if err == "action not enabled" else 400), {"error": err}
    a = ACTIONS[aid]
    if a.get("job"):
        return 400, {"error": "this action runs as a job - use the Deploy page"}
    if not confirm:
        return 400, {"error": "confirmation required"}
    if a["needs_passphrase"] and not passphrase_ok \
            and not verify_passphrase(cfg, passphrase):
        audit({"action": aid, "target": target, "inputs": safe_inputs,
               "result": "DENIED", "reason": "passphrase", "client": client})
        return 403, {"error": "passphrase incorrect"}

    # Push actions carry a file: read it, pipe it on stdin, and send its sha256 as the
    # first argument so the far end can verify what actually arrived. The box re-hashes
    # and re-validates; nothing is installed on trust.
    payload = None
    if a.get("gen_artifact") == "console":
        bind = (inputs or {}).get("bind") or "estate"
        # the operator picks WHO can reach it; the address is worked out here. "estate" =
        # the host this console already reaches the box on (its VPN address when the box is
        # on one); a loopback dest (self-target) has no estate address, so fall through to
        # every interface - the box's firewall still governs. Literal addresses (old
        # callers) pass through untouched.
        if bind in ("estate", "local", "all"):
            dest_host = cfg["targets"][target].rsplit("@", 1)[-1]
            if bind == "local":
                bind = "127.0.0.1:8090"
            elif bind == "all" or dest_host in ("127.0.0.1", "localhost", "::1"):
                bind = "0.0.0.0:8090"
            else:
                bind = f"{dest_host}:8090"
        want_kiosk = str((inputs or {}).get("kiosk", "")) == "yes"
        # with kiosk, the installer lays down tak-kiosk-priv and runs its `install`; the kiosk
        # derives the box's own console URL itself, so nothing about it is passed from here
        try:
            payload = build_console_installer(kiosk=want_kiosk)
        except Exception as e:
            return 500, {"error": f"could not build the console installer: {e}"[:200]}
        argv = [hashlib.sha256(payload.encode()).hexdigest(),
                base64.b64encode(str(bind).encode()).decode()]
    elif a.get("artifact"):
        art = resolve_artifact(a["artifact"])
        try:
            if not art:
                raise FileNotFoundError(a["artifact"])
            with open(art) as fh:
                payload = fh.read()
        except Exception as e:
            audit({"action": aid, "target": target, "inputs": safe_inputs,
                   "result": "ERROR", "reason": f"artifact unavailable: {e}"[:200], "client": client})
            return 500, artifact_missing_error(a)
        argv = [hashlib.sha256(payload.encode()).hexdigest()] + argv

    key = os.path.join(ACTION_KEYS, a["key"])
    remote = a["verb"] + ((" " + " ".join(argv)) if argv else "")
    cmd = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=accept-new", cfg["targets"][target], remote]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           input=payload, timeout=a.get("timeout", 90))
    except Exception as e:
        audit({"action": aid, "target": target, "inputs": safe_inputs,
               "result": "ERROR", "reason": str(e)[:200], "client": client})
        return 502, {"error": f"action failed: {e}"}

    ok = p.returncode == 0
    out = {"status": "ok" if ok else "error"}
    parsed = {}
    for ln in (p.stdout or "").splitlines():
        k, _, v = ln.partition(" ")
        parsed[k] = v
    if not ok:
        out["message"] = (p.stderr or p.stdout or f"rc={p.returncode}").strip()[:400]
    elif a["result"] == "p12":
        out["name"] = parsed.get("OK", "")
        out["fingerprint"] = parsed.get("FP", "")
        out["p12"] = parsed.get("P12", "")           # base64, offered as a download by the page
        out["message"] = f"Issued {out['name']}"
    elif a["result"] == "img":
        out["name"] = parsed.get("OK", "")
        out["password"] = parsed.get("PASS", "")
        out["url"] = parsed.get("URL", "")
        out["itak"] = parsed.get("ITAK", "")
        out["png"] = parsed.get("PNG", "")
        out["message"] = f"Enrolment credential ready for {out['name']}. Scan with ATAK, or use the iTAK line."
    elif a["result"] == "capass":
        if "CAPASS" in parsed:
            out["message"] = ("Certificate password: " + parsed["CAPASS"] + "\n"
                              "This unlocks every .p12 issued on this box - a device asks "
                              "for it when importing a certificate. Note it somewhere safe.")
        else:
            out["message"] = parsed.get("NONE", "no password information returned")
    elif a["result"] == "credlist":
        creds = []
        for ln in (p.stdout or "").splitlines():
            parts = ln.split("|")
            if parts[0] == "CERT" and len(parts) >= 3:
                creds.append({"ctype": "cert", "name": parts[1],
                              "expires": parts[2].replace("_", " ")})
            elif parts[0] == "ENROL" and len(parts) >= 4:
                creds.append({"ctype": "enrol", "name": parts[1],
                              "created": parts[2], "group": parts[3]})
        out["credentials"] = creds
        out["message"] = f"{len(creds)} credential(s)"
    elif a["result"] == "cred":
        out["name"] = parsed.get("OK", "")
        out["ctype"] = parsed.get("TYPE", "")
        if out["ctype"] == "cert":
            out["file"] = parsed.get("FILE", f"{out['name']}.p12")
            out["p12"] = parsed.get("P12", "")
        else:
            out["url"] = parsed.get("URL", "")
            out["itak"] = parsed.get("ITAK", "")
            out["png"] = parsed.get("PNG", "")
        out["message"] = f"{out['name']} ready"
    elif a["result"] == "read":
        out["message"] = (p.stdout or "").rstrip()[:40000]
    else:
        out["message"] = (p.stdout or "done").strip()[:400]
    if ok and aid == "federation-connect":
        record_fedlink(target, safe_inputs.get("displayName"),
                       safe_inputs.get("address"), safe_inputs.get("port"))
    if ok and aid == "federation-groups":
        record_fedgroup(target, safe_inputs.get("group"))
    audit({"action": aid, "target": target, "inputs": safe_inputs,
           "result": "OK" if ok else "FAIL", "client": client})
    return (200 if ok else 502), out


# ---------- the gated agent queue (1.7.0) ------------------------------------------------------
# ---- the agent hub (1.48.0) ------------------------------------------------------------------
# The console does not decide how an agent is used - the OPERATOR does. Each connection is
# one way in (an MCP socket, a pasted API key, a resident OpenClaw) carrying an autonomy
# level the operator chose: observe (read only), propose (suggest, a human confirms), or
# act (execute its allowed actions directly). Tokens are minted once, shown once, and stored
# only as a SHA-256 - the console can verify a token but never reproduce it. No route bills
# MilUX for inference: MCP is a socket the client's own subscription pays for; the key route
# runs on the operator's key; OpenClaw runs on the operator's hardware.
AGENT_CONN_FILE = os.environ.get("VANTAGE_CONSOLE_AGENT_CONNS",
                                 "/var/lib/vantage-console/agent/connections.json")
AUTONOMY = ("observe", "propose", "act")
AGENT_ROUTES = ("mcp", "apikey", "openclaw")


def load_connections():
    try:
        with open(AGENT_CONN_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save_connections(items):
    try:
        os.makedirs(os.path.dirname(AGENT_CONN_FILE), exist_ok=True)
        tmp = AGENT_CONN_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(items, fh, indent=2)
        os.replace(tmp, AGENT_CONN_FILE)
        return True
    except Exception:
        return False


def _token_hash(tok):
    return hashlib.sha256(("vantage-agent:" + tok).encode()).hexdigest()


def create_connection(data, client):
    name = str(data.get("name", "")).strip()[:40]
    route = str(data.get("route", ""))
    autonomy = str(data.get("autonomy", "propose"))
    if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", name):
        return 400, {"error": "name: letters, numbers and simple punctuation, max 40"}
    if route not in AGENT_ROUTES:
        return 400, {"error": "unknown route"}
    if autonomy not in AUTONOMY:
        return 400, {"error": "autonomy must be observe, propose or act"}
    conns = load_connections()
    if len(conns) >= 32:
        return 400, {"error": "connection limit reached"}
    tok = secrets.token_urlsafe(32)
    rec = {"id": os.urandom(6).hex(), "name": name, "route": route, "autonomy": autonomy,
           "token_sha256": _token_hash(tok),
           "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "last_used": None, "calls": 0}
    conns.append(rec)
    if not save_connections(conns):
        return 500, {"error": "could not store the connection"}
    audit({"action": "agent-connect", "route": route, "autonomy": autonomy,
           "name": name, "result": "OK", "client": client})
    # the token is returned ONCE, here, and never again
    return 200, {"id": rec["id"], "token": tok, "route": route, "autonomy": autonomy}


def update_connection(data, client):
    cid = str(data.get("id", ""))
    autonomy = str(data.get("autonomy", ""))
    if autonomy not in AUTONOMY:
        return 400, {"error": "bad autonomy"}
    conns = load_connections()
    hit = next((c for c in conns if c.get("id") == cid), None)
    if not hit:
        return 404, {"error": "no such connection"}
    hit["autonomy"] = autonomy
    save_connections(conns)
    audit({"action": "agent-autonomy", "name": hit.get("name"), "autonomy": autonomy,
           "result": "OK", "client": client})
    return 200, {"id": cid, "autonomy": autonomy}


def revoke_connection(data, client):
    cid = str(data.get("id", ""))
    conns = load_connections()
    keep = [c for c in conns if c.get("id") != cid]
    if len(keep) == len(conns):
        return 404, {"error": "no such connection"}
    save_connections(keep)
    audit({"action": "agent-revoke", "id": cid, "result": "OK", "client": client})
    return 200, {"revoked": cid}


# The API-key route: the operator pastes their own Anthropic-compatible key so the built-in
# chat works without any external agent. The key is stored 0600 in the console's own private
# dir (vantage-console-owned, never group- or world-readable) and never leaves this box - it is
# used only to call the model the operator chose, billed to the operator's own account.
def tls_active():
    tls_dir = os.environ.get("VANTAGE_CONSOLE_TLS_DIR", "/etc/vantage-console/tls")
    return (os.path.isfile(os.path.join(tls_dir, "fullchain.pem"))
            and os.path.isfile(os.path.join(tls_dir, "privkey.pem")))


AGENT_KEY_FILE = os.environ.get("VANTAGE_CONSOLE_AGENT_KEY",
                                "/var/lib/vantage-console/agent/apikey.json")


def load_agent_key():
    try:
        with open(AGENT_KEY_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_agent_key(data, client):
    key = str(data.get("key", "")).strip()
    endpoint = str(data.get("endpoint", "") or "https://api.anthropic.com").strip()
    model = str(data.get("model", "") or "claude-sonnet-4-5").strip()
    autonomy = str(data.get("autonomy", "propose"))
    if key and not re.fullmatch(r"[A-Za-z0-9._-]{20,200}", key):
        return 400, {"error": "that does not look like an API key"}
    if not re.fullmatch(r"https://[A-Za-z0-9.:/-]{1,120}", endpoint):
        return 400, {"error": "endpoint must be an https URL"}
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", model):
        return 400, {"error": "bad model id"}
    if autonomy not in AUTONOMY:
        return 400, {"error": "bad autonomy"}
    cur = load_agent_key()
    # an empty key field means "keep the stored key, change the other settings"
    if not key:
        key = cur.get("key", "")
    rec = {"key": key, "endpoint": endpoint, "model": model, "autonomy": autonomy,
           "set": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        os.makedirs(os.path.dirname(AGENT_KEY_FILE), exist_ok=True)
        fd = os.open(AGENT_KEY_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, json.dumps(rec).encode())
        os.close(fd)
        os.replace(AGENT_KEY_FILE + ".tmp", AGENT_KEY_FILE)
    except Exception as e:
        return 500, {"error": f"could not store the key: {e}"[:120]}
    audit({"action": "agent-key", "model": model, "autonomy": autonomy,
           "result": "OK", "has_key": bool(key), "client": client})
    return 200, {"set": bool(key), "model": model, "endpoint": endpoint, "autonomy": autonomy}


def clear_agent_key(client):
    try:
        os.remove(AGENT_KEY_FILE)
    except Exception:
        pass
    audit({"action": "agent-key-clear", "result": "OK", "client": client})
    return 200, {"cleared": True}


def connection_for_token(tok):
    """The connection a bearer token belongs to, or None. Updates last-used counters."""
    if not tok or len(tok) < 20:
        return None
    h = _token_hash(tok)
    conns = load_connections()
    for c in conns:
        if hmac.compare_digest(c.get("token_sha256", ""), h):
            c["last_used"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            c["calls"] = int(c.get("calls", 0)) + 1
            save_connections(conns)
            return c
    return None


# ---- peers (1.58.0): consoles that can read each other -------------------------------------
# Every console is the full console; peering is how an estate gets more than one pair of
# eyes. A peer holds a token this console minted (stored only as a hash, shown once) and
# may READ: the estate snapshot, and vault folders offered to it. Writing stays local -
# a peer pulls what it wants, so the sync question "what moves between servers?" always
# has an inspectable answer: exactly what the sync rules pulled, and nothing else.
PEERS_IN_FILE = os.environ.get("VANTAGE_CONSOLE_PEERS_IN",
                               "/var/lib/vantage-console/agent/peers-in.json")
PEERS_OUT_FILE = os.environ.get("VANTAGE_CONSOLE_PEERS_OUT",
                                "/var/lib/vantage-console/agent/peers-out.json")


def _load_json_list(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_json_list(path, items):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(items, fh, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def peers_mint(data, client):
    """Grant a peer read access: mint its token, store only the hash, show it once."""
    blocked = client_mode_block()
    if blocked:
        return blocked
    name = str(data.get("name", "")).strip()[:40]
    if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", name):
        return 400, {"error": "name: letters, numbers and simple punctuation, max 40"}
    items = _load_json_list(PEERS_IN_FILE)
    if len(items) >= 16:
        return 400, {"error": "peer limit reached"}
    tok = secrets.token_urlsafe(32)
    rec = {"id": os.urandom(6).hex(), "name": name,
           "token_sha256": _token_hash(tok),
           "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "last_seen": None, "pulls": 0}
    items.append(rec)
    if not _save_json_list(PEERS_IN_FILE, items):
        return 500, {"error": "could not store the grant"}
    audit({"action": "peer-grant", "name": name, "result": "OK", "client": client})
    return 200, {"id": rec["id"], "token": tok, "name": name}


def peer_for_token(tok):
    if not tok or len(tok) < 20:
        return None
    h = _token_hash(tok)
    items = _load_json_list(PEERS_IN_FILE)
    for p_ in items:
        if hmac.compare_digest(p_.get("token_sha256", ""), h):
            p_["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            p_["pulls"] = int(p_.get("pulls", 0)) + 1
            _save_json_list(PEERS_IN_FILE, items)
            return p_
    return None


def sync_discover(client):
    """Find consoles on nodes this estate already knows: enrolled boxes' FQDNs and
    peers' peers. Probes :8090/api/health.json (an open, harmless endpoint) with a
    short timeout - so Add console is a dropdown of real nodes, never free text."""
    import urllib.request as _ur
    state, _err = load_state()
    hosts = {}
    for t in (state or {}).get("targets", []):
        fq = str(t.get("fqdn") or "").strip()
        if fq:
            hosts[fq] = t.get("label") or t.get("name")
    for c in _load_json_list(PEERS_CACHE):
        for sv in ((c.get("snapshot") or {}).get("servers") or []):
            fq = str(sv.get("fqdn") or "").strip()
            if fq and fq not in hosts:
                hosts[fq] = f"{sv.get('label') or sv.get('name')} (via {c.get('name')})"
    known = {p_.get("url") for p_ in _load_json_list(PEERS_OUT_FILE)}
    found = []
    for fq, label in sorted(hosts.items())[:24]:
        url = f"http://{fq}:8090"
        ok = False
        try:
            with _ur.urlopen(url + "/api/health.json", timeout=3) as r:
                ok = r.status == 200 and b"estate_result" in r.read(2048)
        except Exception:
            ok = False
        if ok:
            found.append({"url": url, "label": label, "fqdn": fq,
                          "already_peered": url in known})
    audit({"action": "sync-discover", "found": len(found), "result": "OK",
           "client": client})
    return 200, {"consoles": found}


def peers_add(data, client):
    """Register a console this one reads: its URL and the token IT minted for us."""
    name = str(data.get("name", "")).strip()[:40]
    url = str(data.get("url", "")).strip().rstrip("/")
    tok = str(data.get("token", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", name):
        return 400, {"error": "bad name"}
    if not re.fullmatch(r"https?://[A-Za-z0-9.:\[\]-]{1,200}(:[0-9]{2,5})?", url):
        return 400, {"error": "url must be http(s)://host[:port]"}
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", tok):
        return 400, {"error": "that does not look like a peer token"}
    items = [p_ for p_ in _load_json_list(PEERS_OUT_FILE) if p_.get("name") != name]
    rec = {"id": os.urandom(6).hex(), "name": name, "url": url, "token": tok,
           "added": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    items.append(rec)
    _save_json_list(PEERS_OUT_FILE, items)
    try:
        os.chmod(PEERS_OUT_FILE, 0o600)
    except Exception:
        pass
    code, snap = peer_pull({"id": rec["id"]}, client)
    audit({"action": "peer-add", "name": name, "url": url, "result": "OK", "client": client})
    return 200, {"id": rec["id"], "name": name,
                 "first_pull": "ok" if code == 200 else (snap or {}).get("error", "failed")}


def peers_remove(data, client):
    pid = str(data.get("id", ""))
    changed = False
    for path in (PEERS_IN_FILE, PEERS_OUT_FILE):
        items = _load_json_list(path)
        keep = [p_ for p_ in items if p_.get("id") != pid]
        if len(keep) != len(items):
            _save_json_list(path, keep)
            changed = True
    if not changed:
        return 404, {"error": "no such peer"}
    audit({"action": "peer-remove", "id": pid, "result": "OK", "client": client})
    return 200, {"removed": pid}


def estate_snapshot():
    """What a peer may know about this console: identity, servers, offered vault folders."""
    state, _err = load_state()
    inst = load_instance()
    servers = []
    for t in (state or {}).get("targets", []):
        sw = {r.get("name"): r.get("version") for r in (t.get("software") or [])}
        servers.append({"name": t.get("name"), "label": t.get("label"),
                        "fqdn": t.get("fqdn"), "result": t.get("result"),
                        "counts": t.get("counts"),
                        "takserver": sw.get("takserver")})
    folders = []
    root = store_resolve("", "vault")
    if root and os.path.isdir(root):
        folders = sorted(d for d in os.listdir(root)
                         if os.path.isdir(os.path.join(root, d))
                         and not d.startswith("."))
    return {"console": inst.get("product_name", "Vantage"),
            "mode": inst.get("console_mode", "admin"),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "servers": servers, "vault_folders": folders}


DEPLOYED_UNIT = """[Unit]
Description=Vantage Deployed console
After=network-online.target
[Service]
Type=simple
User=vantage-console
Group=vantage-console
Environment=VANTAGE_CONSOLE_EDITION=deployed
Environment=VANTAGE_CONSOLE_BIND=__BIND__
Environment=VANTAGE_CONSOLE_PORT=__PORT__
ExecStart=/usr/bin/python3 /usr/local/lib/vantage-console/vantage-console-serve.py
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
[Install]
WantedBy=multi-user.target
"""




def build_console_installer(kiosk=False):
    """A self-contained bash installer that stands up a deployed-edition console on the
    box it runs on. It embeds THIS console's program (so the estate stays one version)
    and its unit as base64, writes them, and starts the service bound where the operator
    asked (VANTAGE_CONSOLE_BIND=addr[:port], set by the priv wrapper). With kiosk=True it
    also lays down tak-kiosk-priv (the ONE source of the boot-to-console kiosk, shared with
    the later kiosk action) and runs its `install`, so the screen boots straight to this
    box's own console - the 'power on and the screen is Vantage' experience. The kiosk
    derives that URL itself, on the box. The marker line is what the forced-command gate
    checks for."""
    libdir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(libdir, "vantage-console-serve.py"), "rb") as fh:
        serve_b64 = base64.b64encode(fh.read()).decode()
    collect_path = os.path.join(libdir, "vantage-console-collect.py")
    collect_b64 = ""
    if os.path.isfile(collect_path):
        with open(collect_path, "rb") as fh:
            collect_b64 = base64.b64encode(fh.read()).decode()
    # the kiosk installer is ONE script (console/actions/tak-kiosk-priv), used here at build
    # time and by the tak-kiosk action afterwards, so the two can never drift. With kiosk=True
    # we lay it down and run its `install` op, which derives the box's own console URL itself.
    kiosk_priv_b64 = ""
    kiosk_path = os.path.join(libdir, "actions", "tak-kiosk-priv")
    if kiosk and os.path.isfile(kiosk_path):
        with open(kiosk_path, "rb") as fh:
            kiosk_priv_b64 = base64.b64encode(fh.read()).decode()
    unit_b64 = base64.b64encode(DEPLOYED_UNIT.encode()).decode()
    # "enter it once": the new console starts with THIS console's identity - design,
    # typeface, and the PKI build defaults - but only when the box has no instance.json
    # yet, so a re-install never clobbers a box's own customization.
    inst_b64 = base64.b64encode(json.dumps(load_instance()).encode()).decode()
    # the health poll: without it the deployed console has no state to render and the board
    # sits at 503 forever. Same oneshot service + minute timer the admin installer lays down.
    # Only when the console ships a collector (it always does), and it runs a poll immediately
    # so the board is populated the moment the install finishes, not 20s later.
    collect_units = (
        "cat > /etc/systemd/system/vantage-console-collect.service <<'CUNIT'\n"
        "[Unit]\nDescription=Vantage console - health poll\n[Service]\nType=oneshot\n"
        "User=vantage-console\nGroup=vantage-console\n"
        "ExecStart=/usr/bin/python3 /usr/local/lib/vantage-console/vantage-console-collect.py\n"
        "CUNIT\n"
        "cat > /etc/systemd/system/vantage-console-collect.timer <<'CTIM'\n"
        "[Unit]\nDescription=Vantage console - poll every minute\n[Timer]\n"
        "OnBootSec=20\nOnUnitActiveSec=60\n[Install]\nWantedBy=timers.target\n"
        "CTIM\n") if collect_b64 else ""
    collect_enable = (
        "systemctl enable --now vantage-console-collect.timer\n"
        "systemctl start vantage-console-collect.service 2>/dev/null || true\n"
        ) if collect_b64 else ""
    return (
        "#!/usr/bin/env bash\n"
        "# VANTAGE-CONSOLE-INSTALLER - stands up the deployed-edition console on this box.\n"
        "set -euo pipefail\n"
        'B="${VANTAGE_CONSOLE_BIND:-127.0.0.1}"\n'
        'ADDR="${B%%:*}"; PORT="${B##*:}"; [ "$PORT" = "$B" ] && PORT=8090\n'
        "export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1\n"
        # migrate a pre-2.0 milux-console IN PLACE (idempotent) before we install: stop it so
        # it frees port 8090, rename its user/group and directories into the vantage-console
        # namespace, rename its provisioner artifacts, and drop its old units. Without this, a
        # box that still runs the old console (e.g. an un-migrated dev box) would collide on the
        # port and the new console would fail to start. A box with no old console skips it all.
        "if id milux-console >/dev/null 2>&1 || [ -e /etc/milux-console ] || "
        "[ -e /var/lib/milux-console ] || [ -e /usr/local/lib/milux-console ]; then\n"
        "  for u in milux-console milux-console-deployed milux-console-collect.timer "
        "milux-console-collect.service; do "
        'systemctl stop "$u" 2>/dev/null || true; systemctl disable "$u" 2>/dev/null || true; done\n'
        "  if id milux-console >/dev/null 2>&1 && ! id vantage-console >/dev/null 2>&1; then "
        "usermod -l vantage-console milux-console || true; "
        "usermod -d /var/lib/vantage-console vantage-console 2>/dev/null || true; fi\n"
        "  if getent group milux-console >/dev/null 2>&1 && ! getent group vantage-console "
        ">/dev/null 2>&1; then groupmod -n vantage-console milux-console || true; fi\n"
        "  for d in /etc /var/lib /usr/local/lib; do "
        '[ -e "$d/milux-console" ] && [ ! -e "$d/vantage-console" ] && '
        'mv "$d/milux-console" "$d/vantage-console" || true; done\n'
        "  if [ -d /var/lib/vantage-console/artifacts ]; then "
        'for f in /var/lib/vantage-console/artifacts/milux-*; do [ -e "$f" ] || continue; '
        'b=$(basename "$f"); mv -n "$f" "/var/lib/vantage-console/artifacts/vantage-${b#milux-}" '
        "|| true; done; fi\n"
        "  rm -f /usr/local/lib/vantage-console/milux-console-*.py\n"
        "  rm -f /etc/systemd/system/milux-console.service "
        "/etc/systemd/system/milux-console-deployed.service "
        "/etc/systemd/system/milux-console-collect.service "
        "/etc/systemd/system/milux-console-collect.timer\n"
        "  rm -rf /etc/systemd/system/milux-console.service.d\n"
        "  systemctl daemon-reload || true\n"
        "fi\n"
        "id vantage-console >/dev/null 2>&1 || useradd --system --home /var/lib/vantage-console "
        "--shell /usr/sbin/nologin vantage-console\n"
        "install -d -m 755 /usr/local/lib/vantage-console\n"
        "install -d -o vantage-console -g vantage-console -m 750 /var/lib/vantage-console "
        "/var/lib/vantage-console/agent /var/lib/vantage-console/agent/store\n"
        "install -d -m 755 /etc/vantage-console\n"
        "[ -f /etc/vantage-console/instance.json ] || printf %s '" + inst_b64 + "' "
        "| base64 -d > /etc/vantage-console/instance.json\n"
        "for d in tak-server mission-packs map-packs software; do "
        "install -d -o vantage-console -g vantage-console -m 750 "
        "/var/lib/vantage-console/agent/store/$d; done\n"
        "base64 -d > /usr/local/lib/vantage-console/vantage-console-serve.py <<'B64SERVE'\n"
        + serve_b64 + "\nB64SERVE\n"
        + ("base64 -d > /usr/local/lib/vantage-console/vantage-console-collect.py <<'B64COLLECT'\n"
           + collect_b64 + "\nB64COLLECT\n" if collect_b64 else "")
        + "chmod 644 /usr/local/lib/vantage-console/*.py\n"
        "[ -f /etc/vantage-console/targets.json ] || cat > /etc/vantage-console/targets.json <<TJSON\n"
        '{"targets":[{"name":"self","label":"$(hostname -s)","kind":"local",'
        '"command":"/usr/local/bin/tak-health","profile":"deployed"}]}\n'
        "TJSON\n"
        "base64 -d > /tmp/mc-unit <<'B64UNIT'\n" + unit_b64 + "\nB64UNIT\n"
        'sed -e "s/__BIND__/$ADDR/" -e "s/__PORT__/$PORT/" /tmp/mc-unit '
        "> /etc/systemd/system/vantage-console-deployed.service; rm -f /tmp/mc-unit\n"
        + collect_units
        + "systemctl daemon-reload\n"
        "systemctl enable --now vantage-console-deployed.service\n"
        + collect_enable
        # open the console port in the box firewall unless it binds to loopback only -
        # otherwise the console listens but no browser can reach it. Stored even while ufw
        # is inactive, so a later hardening pass cannot silently wall it off. The provider's
        # own cloud firewall (security group) is separate and stays the operator's to open.
        + 'if [ "$ADDR" != "127.0.0.1" ] && [ "$ADDR" != "localhost" ] && [ "$ADDR" != "::1" ] '
        "&& command -v ufw >/dev/null 2>&1; then ufw allow \"$PORT/tcp\" >/dev/null 2>&1 || true; fi\n"
        + "sleep 2\n"
        "if systemctl is-active --quiet vantage-console-deployed; then\n"
        '  echo "OK deployed console is up on http://$ADDR:$PORT"\n'
        "else\n"
        '  echo "ERR the deployed console did not start"; '
        "journalctl -u vantage-console-deployed -n 15 --no-pager || true; exit 2\n"
        "fi\n"
        + (("install -d -m 755 /usr/local/bin\n"
            "base64 -d > /usr/local/bin/tak-kiosk-priv <<'B64KIOSK'\n" + kiosk_priv_b64
            + "\nB64KIOSK\n"
            "chmod 755 /usr/local/bin/tak-kiosk-priv\n"
            "/usr/local/bin/tak-kiosk-priv install || echo 'WARN kiosk install did not complete'\n")
           if kiosk and kiosk_priv_b64 else ""))


def build_mission_pack(data, client):
    """Package a Knowledge Vault folder as a TAK data package (mission package) into
    Store > mission-packs. Same MANIFEST/manifest.xml format proven in
    common/build-datapackage.py: a zip ATAK and iTAK both import. Text notes carry
    fine; any binary files already in the folder ride along too. This is the bridge
    from 'structured knowledge' to 'something a device pulls'."""
    import uuid as _uuid, zipfile, io
    folder = str(data.get("folder", "")).strip().strip("/")
    name = str(data.get("name", "")).strip() or (folder.rsplit("/", 1)[-1] if folder else "")
    if not folder or not re.fullmatch(STORE_NAME_RE + r"(/" + STORE_NAME_RE + r")*", folder):
        return 400, {"error": "choose a vault folder"}
    if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,60}", name):
        return 400, {"error": "pack name: letters, numbers and simple punctuation, max 60"}
    src = store_resolve(folder, "vault")
    if not src or not os.path.isdir(src):
        return 404, {"error": "no such vault folder"}
    # collect files, capped, skipping dotfiles and the trash
    files, total = [], 0
    for dp, dn, fn in os.walk(src):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in sorted(fn):
            if f.startswith("."):
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, src)
            sz = os.path.getsize(full)
            total += sz
            if total > 380 * 1024 * 1024:
                return 400, {"error": "folder is over ~380MB - too large for the default "
                                      "TAK upload limit; split it or sideload"}
            files.append((full, rel))
    if not files:
        return 400, {"error": "that folder has no files to package"}
    uid = str(_uuid.uuid4())
    contents = "".join('    <Content ignore="false" zipEntry="%s"/>\n'
                       % html.escape(rel, quote=True) for _f, rel in files)
    manifest = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<MissionPackageManifest version="2">\n  <Configuration>\n'
                '    <Parameter name="uid" value="%s"/>\n'
                '    <Parameter name="name" value="%s"/>\n'
                '    <Parameter name="onReceiveDelete" value="false"/>\n'
                '  </Configuration>\n  <Contents>\n%s  </Contents>\n'
                '</MissionPackageManifest>\n'
                % (uid, html.escape(name, quote=True), contents))
    store_ensure()
    dest_dir = os.path.join(STORE_ROOT, "mission-packs")
    os.makedirs(dest_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "mission-pack"
    out = os.path.join(dest_dir, safe + ".zip")
    n = 2
    while os.path.exists(out):
        out = os.path.join(dest_dir, f"{safe}-{n}.zip"); n += 1
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        z.writestr("MANIFEST/manifest.xml", manifest)
        for full, rel in files:
            z.write(full, rel)
    try:
        os.chmod(out, 0o664)
    except Exception:
        pass
    size_mb = os.path.getsize(out) / 1048576
    audit({"action": "mission-pack-build", "target": folder,
           "pack": os.path.basename(out), "files": len(files), "uid": uid,
           "result": "OK", "client": client})
    return 200, {"pack": os.path.basename(out), "files": len(files),
                 "size_mb": round(size_mb, 1), "uid": uid,
                 "path": "mission-packs/" + os.path.basename(out)}


def vault_export(folder):
    """One vault folder as a bundle a peer can pull: text notes only, size-capped."""
    if not re.fullmatch(STORE_NAME_RE, folder or ""):
        return 400, {"error": "bad folder name"}
    root = store_resolve(folder, "vault")
    if not root or not os.path.isdir(root):
        return 404, {"error": "no such folder"}
    files, total = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.startswith(".") or not fn.lower().endswith(VAULT_TEXT_EXT):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, "rb") as fh:
                    raw = fh.read(VAULT_TEXT_MAX)
            except Exception:
                continue
            total += len(raw)
            if total > 4 * 1024 * 1024:
                break
            rel = os.path.relpath(full, store_resolve("", "vault"))
            files.append({"path": rel,
                          "content_b64": base64.b64encode(raw).decode(),
                          "mtime": int(os.path.getmtime(full))})
    return 200, {"folder": folder, "files": files}


def peer_fetch(rec, path):
    """GET one peer endpoint with its token. stdlib only; 15s cap."""
    import urllib.request as _ur
    req = _ur.Request(rec["url"] + path,
                      headers={"Authorization": "Bearer " + rec.get("token", "")})
    with _ur.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


PEERS_CACHE = os.environ.get("VANTAGE_CONSOLE_PEERS_CACHE",
                             "/var/lib/vantage-console/agent/peers-cache.json")


def peer_pull(data, client):
    """Refresh one outbound peer's snapshot into the cache."""
    pid = str(data.get("id", ""))
    rec = next((p_ for p_ in _load_json_list(PEERS_OUT_FILE) if p_.get("id") == pid), None)
    if not rec:
        return 404, {"error": "no such peer"}
    try:
        snap = peer_fetch(rec, "/api/estate/export")
    except Exception as e:
        return 502, {"error": f"could not reach {rec.get('name')}: {e}"[:200]}
    cache = {c.get("id"): c for c in _load_json_list(PEERS_CACHE)}
    cache[pid] = {"id": pid, "name": rec.get("name"), "url": rec.get("url"),
                  "pulled": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "snapshot": snap}
    _save_json_list(PEERS_CACHE, list(cache.values()))
    audit({"action": "peer-pull", "name": rec.get("name"), "result": "OK", "client": client})
    return 200, {"name": rec.get("name"), "servers": len(snap.get("servers", [])),
                 "vault_folders": snap.get("vault_folders", [])}


def vault_pull(data, client):
    """Pull one folder from a peer into this vault. Never clobbers newer local work:
    a local file younger than the peer's copy is left alone and reported."""
    pid = str(data.get("peer", ""))
    folder = str(data.get("folder", ""))
    rec = next((p_ for p_ in _load_json_list(PEERS_OUT_FILE) if p_.get("id") == pid), None)
    if not rec:
        return 404, {"error": "no such peer"}
    if not re.fullmatch(STORE_NAME_RE, folder):
        return 400, {"error": "bad folder name"}
    try:
        bundle = peer_fetch(rec, "/api/vault/export?folder="
                            + __import__("urllib.parse", fromlist=["quote"]).quote(folder))
    except Exception as e:
        return 502, {"error": f"could not reach {rec.get('name')}: {e}"[:200]}
    if "files" not in bundle:
        return 502, {"error": str(bundle.get("error", "peer returned no files"))[:200]}
    created, updated, kept = [], [], []
    for f in bundle["files"][:400]:
        rel = str(f.get("path", ""))
        full = store_resolve(rel, "vault")
        if full is None or not rel.lower().endswith(VAULT_TEXT_EXT):
            continue
        try:
            raw = base64.b64decode(str(f.get("content_b64", "")))[:VAULT_TEXT_MAX]
        except Exception:
            continue
        if os.path.exists(full):
            if os.path.getmtime(full) >= int(f.get("mtime", 0)):
                kept.append(rel)
                continue
            bucket = updated
        else:
            bucket = created
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".peer-tmp"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, full)
        try:
            os.chmod(full, 0o664)
        except Exception:
            pass
        bucket.append(rel)
    # remember the subscription so the sync map can draw it
    log_path = os.path.join(os.path.dirname(PEERS_CACHE), "sync-rules.json")
    rules = [r for r in _load_json_list(log_path)
             if not (r.get("peer") == pid and r.get("folder") == folder)]
    rules.append({"peer": pid, "peer_name": rec.get("name"), "folder": folder,
                  "last_pull": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "created": len(created), "updated": len(updated), "kept": len(kept)})
    _save_json_list(log_path, rules)
    audit({"action": "vault-pull", "target": folder, "peer": rec.get("name"),
           "result": "OK", "client": client})
    return 200, {"created": created, "updated": updated, "kept": kept}


# ---- the MCP server: the console as a socket any agent can plug into ------------------------
# JSON-RPC 2.0 over HTTP POST, bearer-token authed against one connection. The tools a caller
# sees depend on that connection's autonomy: observe gets reads only; propose adds a
# propose_action tool (a human still confirms); act adds run_action (the agent executes,
# audited under the connection's name). No inference happens here - the caller's own model
# does the thinking; the console only answers tool calls.
def mcp_read_tools():
    return [
        {"name": "estate_health",
         "description": "The whole estate's current health: overall verdict and every server "
                        "with its result and check counts.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "server_detail",
         "description": "One server's full health checks, software inventory and federation.",
         "inputSchema": {"type": "object",
                         "properties": {"name": {"type": "string",
                                                 "description": "the server's estate name"}},
                         "required": ["name"]}},
        {"name": "list_credentials",
         "description": "Device credentials issued on a server (names and dates only, no "
                        "secret material).",
         "inputSchema": {"type": "object",
                         "properties": {"name": {"type": "string"}}, "required": ["name"]}},
        {"name": "vault_search",
         "description": "Search the knowledge vault: returns matching notes with a snippet. "
                        "The estate's own knowledge, for the agent to read.",
         "inputSchema": {"type": "object",
                         "properties": {"query": {"type": "string"}},
                         "required": ["query"]}},
        {"name": "vault_read",
         "description": "Read one knowledge-vault note in full, by its path.",
         "inputSchema": {"type": "object",
                         "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "agent_context",
         "description": "The operator's standing brief for connected agents: identity, "
                        "standing orders and estate background, maintained in the Knowledge "
                        "Vault. Read this first in a new session.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "list_actions",
         "description": "The gated actions available, and which servers each applies to - so "
                        "you know what you may propose or run.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]


def mcp_write_tools(autonomy):
    _vault_build = {
        "name": "vault_build",
        "description": "Build a structured Knowledge Vault section from content you "
                       "produce - turn a raw dump into folders and notes. Pass a tree: "
                       "folders are objects, notes are strings of their Markdown. Nested "
                       "folders nest. Existing notes are never overwritten. This is how "
                       "you turn 'here is a pile of information' into a deployment pack.",
        "inputSchema": {"type": "object",
                        "properties": {"tree": {"type": "object",
                                       "description": "e.g. {\"Exercise Bravo\": "
                                       "{\"Orders.md\": \"# Orders...\", \"Comms.md\": "
                                       "\"# Comms...\"}}"}},
                        "required": ["tree"]}}
    _mission_pack = {
        "name": "mission_pack_build",
        "description": "Package a Knowledge Vault folder as a TAK mission pack (a data "
                       "package ATAK and iTAK both import) into Store > mission-packs. "
                       "After you have structured a deployment with vault_build, this "
                       "turns it into something a device can pull. Then run_action with "
                       "a push action to deploy it to a server.",
        "inputSchema": {"type": "object",
                        "properties": {"folder": {"type": "string",
                                       "description": "the vault folder to package"},
                                       "name": {"type": "string",
                                       "description": "the pack name shown in ATAK"}},
                        "required": ["folder"]}}
    _vault_write = {
        "name": "vault_write",
        "description": "Create or update one Knowledge Vault note. path is vault-relative "
                       "and ends .md or .txt; mode 'create' refuses to overwrite, "
                       "'update' replaces, 'append' adds to the end.",
        "inputSchema": {"type": "object",
                        "properties": {"path": {"type": "string"},
                                       "content": {"type": "string"},
                                       "mode": {"type": "string",
                                                "enum": ["create", "update", "append"]}},
                        "required": ["path", "content"]}}
    if autonomy == "propose":
        return [{"name": "propose_action",
                 "description": "Propose a gated action for a human to confirm in the console. "
                                "You cannot execute; a person reviews and runs it.",
                 "inputSchema": {"type": "object",
                                 "properties": {"action": {"type": "string"},
                                                "server": {"type": "string"},
                                                "arguments": {"type": "object"},
                                                "rationale": {"type": "string"}},
                                 "required": ["action", "server", "rationale"]}}]
    if autonomy == "act":
        return [{"name": "run_action",
                 "description": "Run a gated action on a server directly. It is executed and "
                                "audited under this connection's name.",
                 "inputSchema": {"type": "object",
                                 "properties": {"action": {"type": "string"},
                                                "server": {"type": "string"},
                                                "arguments": {"type": "object"}},
                                 "required": ["action", "server"]}},
                _vault_build, _vault_write, _mission_pack]
    return []


def _mcp_text(obj):
    body = obj if isinstance(obj, str) else json.dumps(obj, indent=2)
    return {"content": [{"type": "text", "text": body}]}


def mcp_call_tool(conn, name, args, client):
    args = args or {}
    state, _err = load_state()
    targets = {t.get("name"): t for t in (state or {}).get("targets", [])}
    if name == "estate_health":
        return _mcp_text({"estate_result": (state or {}).get("estate_result"),
                          "checked": (state or {}).get("generated_at"),
                          "servers": [{"name": t.get("name"), "label": t.get("label"),
                                       "result": t.get("result"), "counts": t.get("counts")}
                                      for t in (state or {}).get("targets", [])]})
    if name == "server_detail":
        t = targets.get(str(args.get("name", "")))
        if not t:
            return _mcp_text("no server by that name")
        return _mcp_text({"name": t.get("name"), "result": t.get("result"),
                          "checks": t.get("checks"), "software": software_rows(t)})
    if name == "agent_context":
        return _mcp_text(load_agent_context() or "No standing context set yet - the "
                         "operator can write it on the console's AI Connections page.")
    if name == "list_actions":
        cfg = load_actions_config()
        acts = enabled_actions(cfg)
        out = {}
        for aid in acts:
            out[aid] = {"label": ACTIONS[aid]["label"],
                        "servers": [n for n, t in targets.items() if action_applies(aid, t)]}
        return _mcp_text(out)
    if name == "list_credentials":
        code, res = run_action("list-credentials", str(args.get("name", "")), {}, None, True,
                               client, passphrase_ok=True)
        return _mcp_text(res.get("credentials", res.get("message", res)))
    if name == "vault_search":
        return _mcp_text(vault_search(str(args.get("query", ""))))
    if name == "vault_read":
        rel = str(args.get("path", ""))
        pth = store_resolve(rel, "vault")
        if not pth or not os.path.isfile(pth):
            return _mcp_text("no such note")
        try:
            with open(pth) as fh:
                return _mcp_text(fh.read()[:40000])
        except Exception:
            return _mcp_text("could not read the note")
    if name == "propose_action" and conn.get("autonomy") == "propose":
        code, res = ingest_proposal({"aid": str(args.get("action", "")),
                                     "target": str(args.get("server", "")),
                                     "args": args.get("arguments") or {},
                                     "rationale": str(args.get("rationale", "")),
                                     "source": "mcp:" + conn.get("name", "agent")}, client)
        return _mcp_text(res)
    if name == "run_action" and conn.get("autonomy") == "act":
        code, res = run_action(str(args.get("action", "")), str(args.get("server", "")),
                               args.get("arguments") or {}, None, True,
                               "mcp:" + conn.get("name", "agent"), passphrase_ok=True)
        return _mcp_text(res)
    if name == "vault_build" and conn.get("autonomy") == "act":
        tree = args.get("tree")
        if not isinstance(tree, dict):
            return _mcp_text("vault_build needs a tree object")
        code, res = scaffold_ontology({"ontology": "custom", "tree": tree}, client)
        return _mcp_text(res)
    if name == "vault_write" and conn.get("autonomy") == "act":
        code, res = agent_vault_write(str(args.get("path", "")),
                                      str(args.get("content", "")),
                                      str(args.get("mode", "create")), client)
        return _mcp_text(res)
    if name == "mission_pack_build" and conn.get("autonomy") == "act":
        code, res = build_mission_pack({"folder": args.get("folder"),
                                        "name": args.get("name", "")}, client)
        return _mcp_text(res)
    return {"content": [{"type": "text", "text": "tool not available at this autonomy level "
                        "(vault writes and actions need the connection set to Act)"}],
            "isError": True}


def mcp_dispatch(conn, req, client):
    """One JSON-RPC request against the connection. Returns the response dict, or None for a
    notification (no id)."""
    rid = req.get("id")
    method = req.get("method", "")
    inst = load_instance()
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": inst["product_name"] + " console",
                                 "version": VERSION}}
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": mcp_read_tools() + mcp_write_tools(conn.get("autonomy", "observe"))}
    elif method == "tools/call":
        params = req.get("params") or {}
        audit({"action": "mcp-tool", "tool": params.get("name"),
               "name": conn.get("name"), "autonomy": conn.get("autonomy"), "client": client})
        result = mcp_call_tool(conn, str(params.get("name", "")), params.get("arguments"),
                               "mcp:" + conn.get("name", "agent"))
    else:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "method not found"}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "result": result}


# The AI's standing context - identity, standing orders, estate briefing - is CONSOLE
# configuration, not vault knowledge. It lives in the console's own dir and is edited on
# the AI Connections page, never cluttering the Knowledge Vault (which is the estate's
# knowledge, not the assistant's setup). Read by the built-in chat and MCP-connected
# agents; a resident agent has its own identity on its own box.
AGENT_CONTEXT_FILE = os.environ.get("VANTAGE_CONSOLE_AGENT_CONTEXT",
                                    "/var/lib/vantage-console/agent/context.json")
AGENT_CONTEXT_FIELDS = (
    ("identity", "Identity", "Who the assistant is: its name, tone, what it calls "
     "people, what it cares about most."),
    ("orders", "Standing orders", "Rules it must always follow - one per line works well."),
    ("estate", "About this estate", "Brief it like a new colleague: what this "
     "deployment is for, who uses it, what matters."),
)


def load_agent_context_doc():
    try:
        with open(AGENT_CONTEXT_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_agent_context_doc(data, client):
    doc = {}
    for key, _label, _help in AGENT_CONTEXT_FIELDS:
        doc[key] = str(data.get(key, ""))[:8000]
    try:
        os.makedirs(os.path.dirname(AGENT_CONTEXT_FILE), exist_ok=True)
        tmp = AGENT_CONTEXT_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, AGENT_CONTEXT_FILE)
    except Exception as e:
        return 500, {"error": f"could not save: {e}"[:150]}
    audit({"action": "agent-context-save", "result": "OK", "client": client})
    return 200, {"saved": True}


def ensure_agent_context():
    # kept as a name for callers; nothing to seed - the config file is created on save,
    # and its absence renders as an empty, editable form.
    return


def load_agent_context(cap=6000):
    """The standing context as a system-prompt block, from console config."""
    doc = load_agent_context_doc()
    out = []
    for key, label, _help in AGENT_CONTEXT_FIELDS:
        v = (doc.get(key) or "").strip()
        if v:
            out.append(f"--- {label} ---\n" + v)
    return "\n\n".join(out)[:cap]


# ---- the persistent conversation --------------------------------------------------------
AGENT_CHAT_FILE = os.environ.get("VANTAGE_CONSOLE_AGENT_CHAT",
                                 "/var/lib/vantage-console/agent/chat.json")


def load_chat():
    try:
        with open(AGENT_CHAT_FILE) as fh:
            d = json.load(fh)
        return d.get("messages", []) if isinstance(d, dict) else []
    except Exception:
        return []


def save_chat(messages):
    try:
        os.makedirs(os.path.dirname(AGENT_CHAT_FILE), exist_ok=True)
        tmp = AGENT_CHAT_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"messages": messages[-200:]}, fh)
        os.replace(tmp, AGENT_CHAT_FILE)
    except Exception:
        pass


def clear_chat(client):
    try:
        os.remove(AGENT_CHAT_FILE)
    except Exception:
        pass
    audit({"action": "agent-chat-clear", "result": "OK", "client": client})
    return 200, {"cleared": True}


def agent_chat(data, client):
    """The built-in chat: one turn against the operator's own model, with the estate tools.
    Reuses the MCP tool set and honours the stored autonomy - the same eyes-and-hands the
    socket exposes, driven here by a key the operator supplied. Runs a bounded tool-use loop
    and returns the assistant's final text plus a note of any tools it used."""
    import urllib.request as _ur
    cfg = load_agent_key()
    key = cfg.get("key")
    if not key:
        return 400, {"error": "no API key set - paste one on the Agent page first"}
    msg = str(data.get("message", "")).strip()
    if not msg:
        return 400, {"error": "empty message"}
    history = data.get("history") or []
    autonomy = cfg.get("autonomy", "propose")
    inst = load_instance()
    # a synthetic connection so the shared MCP tool layer applies the autonomy consistently
    conn = {"name": "chat", "autonomy": autonomy}
    tools = []
    for t in mcp_read_tools() + mcp_write_tools(autonomy):
        tools.append({"name": t["name"], "description": t["description"],
                      "input_schema": t.get("inputSchema", {"type": "object", "properties": {}})})
    system = (f"You are {inst['agent_name']}, the assistant built into a {inst['product_name']} "
              "console that manages a fleet of TAK servers. Use the tools to read the estate's "
              "health, servers, credentials and knowledge vault before answering. Be concise "
              "and practical. "
              + {"observe": "You can only read; never claim to have changed anything.",
                 "propose": "When something should be done, use propose_action - a human "
                            "confirms it in the console. You cannot execute directly.",
                 "act": "You may run_action to make changes directly; say what you did."}[autonomy])
    ensure_agent_context()
    ctx = load_agent_context()
    if ctx:
        system += ("\n\nThe operator maintains your identity, standing orders and estate "
                   "briefing in the Knowledge Vault folder 'Agent Context'. Follow them:\n\n"
                   + ctx)
    # one persistent conversation, held server-side - a page refresh loses nothing
    stored = load_chat()
    messages = []
    for h in stored[-12:]:
        if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": msg})

    used = []
    endpoint = cfg.get("endpoint", "https://api.anthropic.com").rstrip("/")
    for _hop in range(6):
        body = json.dumps({"model": cfg.get("model", "claude-sonnet-4-5"),
                           "max_tokens": 1024, "system": system,
                           "messages": messages, "tools": tools}).encode()
        req = _ur.Request(endpoint + "/v1/messages", data=body, method="POST",
                          headers={"content-type": "application/json",
                                   "x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            with _ur.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
        except Exception as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]  # type: ignore[attr-defined]
            except Exception:
                pass
            return 502, {"error": f"the model call failed: {e}. {detail}"[:300]}
        content = resp.get("content", [])
        messages.append({"role": "assistant", "content": content})
        tool_uses = [c for c in content if c.get("type") == "tool_use"]
        if not tool_uses:
            text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
            reply = text.strip() or "(no reply)"
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            stored.append({"role": "user", "content": msg, "ts": now})
            stored.append({"role": "assistant", "content": reply, "ts": now, "used": used})
            save_chat(stored)
            return 200, {"reply": reply, "used": used}
        results = []
        for tu in tool_uses:
            used.append(tu.get("name"))
            out = mcp_call_tool(conn, tu.get("name", ""), tu.get("input") or {},
                                "chat:apikey")
            txt = out.get("content", [{}])[0].get("text", "") if out.get("content") else ""
            results.append({"type": "tool_result", "tool_use_id": tu.get("id"),
                            "content": txt[:8000]})
        messages.append({"role": "user", "content": results})
    return 200, {"reply": "(the assistant kept calling tools without finishing; try again "
                          "with a narrower question)", "used": used}


def vault_search(query):
    """Grep the knowledge vault for a query; return matching notes with a snippet."""
    query = (query or "").strip()
    if not query:
        return []
    root = store_resolve("", "vault")
    if not root or not os.path.isdir(root):
        return []
    hits, ql = [], query.lower()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith(".") or not fn.lower().endswith((".md", ".txt")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, errors="ignore") as fh:
                    text = fh.read(200000)
            except Exception:
                continue
            i = text.lower().find(ql)
            if i >= 0:
                s = max(0, i - 60)
                hits.append({"path": os.path.relpath(full, root),
                             "snippet": text[s:s + 200].replace("\n", " ")})
            if len(hits) >= 25:
                return hits
    return hits


def load_proposals():
    try:
        with open(PROPOSALS) as fh:
            d = json.load(fh)
        return [p for p in d if isinstance(p, dict)] if isinstance(d, list) else []
    except Exception:
        return []


def save_proposals(items):
    try:
        tmp = PROPOSALS + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(items[-50:], fh, indent=2)   # cap the queue; oldest fall off
        os.replace(tmp, PROPOSALS)
        return True
    except Exception:
        return False


def ingest_proposal(body, client):
    """Accept a proposed action from the agent. Validated exactly as if it were about to run -
    a bad proposal is rejected here, never stored - but nothing executes: it joins the queue for
    a human to read and confirm. The agent cannot execute, cannot pick an unknown action, cannot
    smuggle an argument past the pattern, and cannot target a box the action does not fit."""
    if not isinstance(body, dict):
        return 400, {"error": "expected an object"}
    aid = str(body.get("aid", ""))
    target = str(body.get("target", ""))
    args = body.get("args") or {}
    cfg = load_actions_config()
    ok, err, _argv, safe_inputs = validate_action_request(aid, target, args, cfg)
    if not ok:
        return 400, {"error": err}
    # An action only makes sense on a box that runs the component it manages.
    state, _e = load_state()
    tgt = next((t for t in (state or {}).get("targets", []) if t.get("name") == target), None)
    if tgt is not None and not action_applies(aid, tgt):
        return 400, {"error": "action does not apply to that box"}
    rationale = str(body.get("rationale", "")).strip()[:600]
    source = str(body.get("source", "openclaw")).strip()[:48] or "openclaw"
    if not re.match(r"^[A-Za-z0-9 ._@:-]{1,48}$", source):
        source = "openclaw"
    rec = {"id": os.urandom(5).hex(),
           "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "aid": aid, "target": target, "args": safe_inputs,
           "rationale": rationale, "source": source, "status": "pending"}
    items = [p for p in load_proposals()
             if not (p.get("aid") == aid and p.get("target") == target
                     and p.get("args") == safe_inputs and p.get("status") == "pending")]
    items.append(rec)
    if not save_proposals(items):
        return 500, {"error": "could not store proposal"}
    audit({"action": aid, "target": target, "inputs": safe_inputs,
           "result": "PROPOSED", "source": source, "client": client})
    return 200, {"status": "stored", "id": rec["id"]}


def dismiss_proposal(pid, client):
    pid = str(pid or "")
    items = load_proposals()
    kept = [p for p in items if p.get("id") != pid]
    if len(kept) == len(items):
        return 404, {"error": "no such proposal"}
    save_proposals(kept)
    audit({"action": "propose-dismiss", "target": "", "result": "OK",
           "reason": pid, "client": client})
    return 200, {"status": "dismissed"}


# ---------- federation links the console has made (so the map can SHOW them) -------------------
# The federation map's job is to answer "what have I federated". The checker gives each box's live
# federate COUNT but not WHO, so the console remembers the outbound links it created through
# federation-connect and draws them as edges. A link is recorded only when the action succeeds.
FEDLINKS = os.environ.get("VANTAGE_CONSOLE_FEDLINKS", "/var/lib/vantage-console/agent/federation-links.json")


def load_fedlinks():
    try:
        d = json.load(open(FEDLINKS))
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
    except Exception:
        return []


def save_fedlinks(items):
    try:
        tmp = FEDLINKS + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(items[-100:], fh, indent=2)
        os.replace(tmp, FEDLINKS)
        return True
    except Exception:
        return False


def record_fedlink(source, partner, address, port):
    key = (source, address, str(port))
    items = [x for x in load_fedlinks()
             if (x.get("source"), x.get("address"), str(x.get("port"))) != key]
    items.append({"source": source, "partner": partner or address, "address": address,
                  "port": str(port),
                  "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    save_fedlinks(items)


# ---------- the group each box shares across federation (so the map can SAY what crosses) ------
# A live federation link with no shared group carries nothing: TAK creates federate entries with
# no groups, and an empty entry overrides the CA default. The map's second job, after "what have
# I federated", is "and does anything actually cross it" - so the console remembers the group it
# set through federation-groups, the same way it remembers the links it made. This is a record of
# what THIS console did, not a read of the box: a group set by hand on the server, or by another
# console, is not here, and the map says so rather than guessing.
FEDGROUPS = os.environ.get("VANTAGE_CONSOLE_FEDGROUPS",
                           "/var/lib/vantage-console/agent/federation-groups.json")


def load_fedgroups():
    try:
        with open(FEDGROUPS) as fh:
            d = json.load(fh)
        return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}
    except Exception:
        return {}


def record_fedgroup(target, group):
    """Remember the group this box now shares with its federation partners. One entry per box:
    federation-groups writes one group everywhere, so the last successful run is the truth."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(group or "")):
        return False
    groups = load_fedgroups()
    groups[str(target)] = {"group": str(group),
                           "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        os.makedirs(os.path.dirname(FEDGROUPS), exist_ok=True)
        tmp = FEDGROUPS + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(groups, fh, indent=2)
        os.replace(tmp, FEDGROUPS)
        return True
    except Exception:
        return False


def forget_fedlink(source, address, port):
    items = load_fedlinks()
    kept = [x for x in items if not (x.get("source") == source and x.get("address") == address
                                     and str(x.get("port")) == str(port))]
    if len(kept) == len(items):
        return False
    save_fedlinks(kept)
    return True


# ---------- provision jobs (1.13.0) ------------------------------------------------------------
# A provision run takes many minutes, so it cannot ride the synchronous action path: POSTing
# /api/action/provision-server starts a JOB - same validation, same artifact+sha256 push, same
# audit - whose output streams to a log file the Deploy page polls. Job state is files under
# agent/jobs (the writable subdir), so a console restart loses nothing.
JOBS_DIR = os.environ.get("VANTAGE_CONSOLE_JOBS", "/var/lib/vantage-console/agent/jobs")

# 1.26.0 deployments (card 6172): a deployment is a named server-side record, not
# browser page state. The wizard autosaves into it and a reloaded page reopens it.
# No secrets ever land here: no passphrase, no key material.
DEPLOYMENTS_FILE = os.environ.get("VANTAGE_CONSOLE_DEPLOYMENTS",
                                  "/var/lib/vantage-console/agent/deployments.json")

_DEP_FIELDS = ("name", "label", "address", "user", "profile", "deb_file", "deb_sha256",
               "fqdn", "le_email", "org", "org_unit", "country", "state", "city",
               "components", "cred_rows")


def load_deployments():
    try:
        with open(DEPLOYMENTS_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_deployment(data):
    """Upsert one deployment draft from the wizard. Validated hard: the name is the
    key, every field is length-capped text, credential rows carry user+group only."""
    name = str(data.get("name", ""))
    if not re.fullmatch(r"[a-z0-9-]{1,24}", name):
        return 400, {"error": "deployment name must be [a-z0-9-], max 24"}
    rec = {"name": name}
    for f in _DEP_FIELDS[1:-1]:
        rec[f] = str(data.get(f, ""))[:253]
    rows = data.get("cred_rows") or []
    if not isinstance(rows, list) or len(rows) > 20:
        return 400, {"error": "bad credential rows"}
    clean = []
    for r in rows:
        u = str((r or {}).get("user", ""))[:40]
        g = str((r or {}).get("group", ""))[:40]
        if u:
            clean.append({"user": u, "group": g or "MilUX"})
    rec["cred_rows"] = clean
    rec["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    deps = load_deployments()
    rec["created"] = deps.get(name, {}).get("created", rec["updated"])
    deps[name] = rec
    tmp = DEPLOYMENTS_FILE + ".tmp"
    os.makedirs(os.path.dirname(DEPLOYMENTS_FILE), exist_ok=True)
    with open(tmp, "w") as fh:
        json.dump(deps, fh, indent=1)
    os.replace(tmp, DEPLOYMENTS_FILE)
    return 200, {"saved": name}


def delete_deployment(data):
    name = str(data.get("name", ""))
    deps = load_deployments()
    if name not in deps:
        return 404, {"error": "no such deployment"}
    del deps[name]
    tmp = DEPLOYMENTS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(deps, fh, indent=1)
    os.replace(tmp, DEPLOYMENTS_FILE)
    return 200, {"deleted": name}


def _job_paths(job_id):
    return os.path.join(JOBS_DIR, job_id + ".log"), os.path.join(JOBS_DIR, job_id + ".json")


def _job_write(job_id, rec):
    _, meta = _job_paths(job_id)
    tmp = meta + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh)
    os.replace(tmp, meta)


def job_status(job_id):
    if not re.fullmatch(r"j[0-9a-f]{12}", str(job_id or "")):
        return None
    log_path, meta = _job_paths(job_id)
    try:
        with open(meta) as fh:
            rec = json.load(fh)
    except Exception:
        return None
    try:
        with open(log_path) as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            rec["log"] = fh.read()
    except Exception:
        rec["log"] = ""
    return rec


def start_upgrade_job(target, inputs, confirm, client):
    """The upgrade-server action as a two-phase job: stream the package from the Store
    shelf to the box's staging inbox (push key), then run the in-place upgrade behind
    its own key and root wrapper. The library copy is re-hashed before a byte moves."""
    aid = "upgrade-server"
    cfg = load_actions_config()
    ok, err, _argv, safe_inputs = validate_action_request(aid, target, inputs, cfg)
    if not ok:
        return (403 if err == "action not enabled" else 400), {"error": err}
    if not confirm:
        return 400, {"error": "confirmation required"}
    a = ACTIONS[aid]
    deb_file, digest = safe_inputs["deb_file"], safe_inputs["deb_sha256"]
    src = os.path.join(LIBRARY_DIR, deb_file)
    if not os.path.isfile(src):
        return 400, {"error": f"{deb_file} is not on the Store shelf"}
    h = hashlib.sha256()
    with open(src, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != digest:
        return 400, {"error": "shelf copy does not match the given sha256"}
    dest = cfg["targets"][target]
    job_id = "j" + os.urandom(6).hex()
    os.makedirs(JOBS_DIR, exist_ok=True)
    log_path, _ = _job_paths(job_id)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _job_write(job_id, {"job": job_id, "action": aid, "target": target, "status": "running",
                        "started": started, "inputs": safe_inputs})
    audit({"action": aid, "target": target, "inputs": safe_inputs, "result": "STARTED",
           "job": job_id, "client": client})
    push_key = os.path.join(ACTION_KEYS, a["push_key"])
    run_key = os.path.join(ACTION_KEYS, a["key"])

    def run():
        rc = -1
        try:
            with open(log_path, "w") as log:
                log.write(f"== UPGRADE {target}: {deb_file} ==\n-- phase 1/2: push the package --\n")
                log.flush()
                # a package already staged (an earlier run, or side-loaded past a slow
                # path) is not streamed again; the box re-hashes it before install anyway
                h = subprocess.run(
                    ["ssh", "-i", push_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     "-o", "StrictHostKeyChecking=accept-new", dest,
                     f"have-package {digest}"],
                    capture_output=True, text=True, timeout=30)
                if h.returncode == 0 and "HELD" in (h.stdout or ""):
                    log.write("package already staged on the box (hash-named); skipping the stream\n")
                    log.flush()
                    p = h
                else:
                    with open(src, "rb") as deb:
                        p = subprocess.run(
                            ["ssh", "-i", push_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                             "-o", "StrictHostKeyChecking=accept-new", dest,
                             f"push-package {digest} {deb_file}"],
                            stdin=deb, capture_output=True, text=True, timeout=1800)
                    log.write((p.stdout or "") + (p.stderr or ""))
                    log.flush()
                if p.returncode != 0:
                    raise RuntimeError("package push failed")
                log.write("-- phase 2/2: upgrade in place (backup, stop, install, migrate, start) --\n")
                log.flush()
                p2 = subprocess.Popen(
                    ["ssh", "-i", run_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     "-o", "StrictHostKeyChecking=accept-new", dest, f"upgrade-server {digest}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in p2.stdout:
                    log.write(line)
                    log.flush()
                rc = p2.wait(timeout=2400)
                if rc != 0:
                    raise RuntimeError(f"upgrade rc={rc}")
                rc = 0
        except Exception as e:
            rc = rc if rc > 0 else 1
            try:
                with open(log_path, "a") as log:
                    log.write(f"\nERR {e}\n")
            except OSError:
                pass
        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _job_write(job_id, {"job": job_id, "action": aid, "target": target,
                            "status": "done" if rc == 0 else "failed", "rc": rc,
                            "started": started, "finished": finished, "inputs": safe_inputs})
        audit({"action": aid, "target": target, "inputs": safe_inputs,
               "result": "OK" if rc == 0 else "ERROR", "job": job_id, "client": client})

    threading.Thread(target=run, daemon=True).start()
    return 200, {"status": "started", "job": job_id}


def setup_last_job():
    """The newest setup-server job, so a reloaded Deploy page can find the build it
    lost sight of (1.25.0). Metadata only; the page fetches the log by job id."""
    try:
        metas = [f for f in os.listdir(JOBS_DIR) if f.endswith(".json")]
    except Exception:
        return {"job": None}
    best, best_key = None, None
    for f in metas:
        path = os.path.join(JOBS_DIR, f)
        try:
            with open(path) as fh:
                rec = json.load(fh)
            # started is to the second; the file's mtime breaks same-second ties
            key = (str(rec.get("started", "")), os.stat(path).st_mtime)
        except Exception:
            continue
        if rec.get("action") != "setup-server":
            continue
        if best is None or key > best_key:
            best, best_key = rec, key
    if not best:
        return {"job": None}
    return {"job": best.get("job"), "status": best.get("status"),
            "target": best.get("target"), "started": best.get("started"),
            "rc": best.get("rc"), "dry": best.get("dry")}


def job_args(aid, inputs):
    """The REAL argument dict a job delivers to the box. safe_inputs is the redacted view
    for the audit and the job record; sending IT box-ward would hand the box the literal
    string "(secret)" for any secret-marked input (latent until the mesh actions - no
    shipped job action carried a secret input through this path). The box re-validates
    every field regardless. Mesh actions also get the channel URL, resolved here from the
    channel store, so the PSK never rides a page, a request or the audit trail. Returns
    (args, err)."""
    a = ACTIONS[aid]
    args = {f["name"]: str((inputs or {}).get(f["name"], "") or "") for f in a["inputs"]}
    for k, v in (a.get("fixed_args") or {}).items():
        args[k] = str(v)
    if aid in ("deploy-mesh-gateway", "apply-mesh-channel"):
        url = mesh_channel_url_for(args.get("channel", ""))
        if not url:
            return None, "unknown channel - create it on the Networks page first"
        args["channel_url_b64"] = base64.urlsafe_b64encode(url.encode()).decode()
    return args, None


def start_job(aid, target, inputs, confirm, client):
    """Start a long-running gated action as a job. Same gate as run_action: the shared
    validator, an explicit confirm, the artifact pushed with its sha256. Returns the job id
    immediately; the run streams to the job log."""
    cfg = load_actions_config()
    ok, err, _argv, safe_inputs = validate_action_request(aid, target, inputs, cfg)
    if not ok:
        return (403 if err == "action not enabled" else 400), {"error": err}
    if not confirm:
        return 400, {"error": "confirmation required"}
    a = ACTIONS[aid]
    art = resolve_artifact(a["artifact"])
    try:
        if not art:
            raise FileNotFoundError(a["artifact"])
        with open(art) as fh:
            payload = fh.read()
    except Exception:
        return 500, artifact_missing_error(a)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    args, aerr = job_args(aid, inputs)
    if aerr:
        return 400, {"error": aerr}
    argsb64 = base64.b64encode(json.dumps(args).encode()).decode()
    job_id = "j" + os.urandom(6).hex()
    os.makedirs(JOBS_DIR, exist_ok=True)
    log_path, _ = _job_paths(job_id)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _job_write(job_id, {"job": job_id, "action": aid, "target": target, "status": "running",
                        "started": started, "inputs": safe_inputs})
    audit({"action": aid, "target": target, "inputs": safe_inputs, "result": "STARTED",
           "job": job_id, "client": client})
    key = os.path.join(ACTION_KEYS, a["key"])
    cmd = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=accept-new", cfg["targets"][target],
           f"{a['verb']} {digest} {argsb64}"]

    def run():
        rc = -1
        try:
            with open(log_path, "w") as log:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
                p.stdin.write(payload)
                p.stdin.close()
                for line in p.stdout:
                    log.write(line)
                    log.flush()
                rc = p.wait(timeout=4000)
        except Exception as e:
            try:
                with open(log_path, "a") as log:
                    log.write(f"\nERR job runner: {e}\n")
            except OSError:
                pass
        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _job_write(job_id, {"job": job_id, "action": aid, "target": target,
                            "status": "done" if rc == 0 else "failed", "rc": rc,
                            "started": started, "finished": finished, "inputs": safe_inputs})
        audit({"action": aid, "target": target, "inputs": safe_inputs,
               "result": "OK" if rc == 0 else "ERROR",
               "reason": "" if rc == 0 else f"job rc={rc}", "job": job_id, "client": client})

    threading.Thread(target=run, daemon=True).start()
    return 200, {"status": "started", "job": job_id}


def start_mesh_deploy_job(target, inputs, confirm, client):
    """deploy-mesh-gateway as a two-phase job, the start_upgrade_job shape: stream the
    gateway bundle from the Store shelf to the box's staging inbox (the shared package-push
    key), then stream the installer with the validated parameters over the mesh key. The
    shelf copy is hashed here; both box gates re-hash before anything runs."""
    aid = "deploy-mesh-gateway"
    cfg = load_actions_config()
    ok, err, _argv, safe_inputs = validate_action_request(aid, target, inputs, cfg)
    if not ok:
        return (403 if err == "action not enabled" else 400), {"error": err}
    if not confirm:
        return 400, {"error": "confirmation required"}
    a = ACTIONS[aid]
    bundle_file = str((inputs or {}).get("bundle_file") or "")
    src = os.path.join(STORE_ROOT, "mesh-gateway", bundle_file)
    if not os.path.isfile(src):
        return 400, {"error": f"{bundle_file} is not on the Store shelf - upload the cut "
                              "bundle to Store > mesh-gateway first"}
    h = hashlib.sha256()
    with open(src, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    bundle_sha = h.hexdigest()
    art = resolve_artifact(a["artifact"])
    try:
        if not art:
            raise FileNotFoundError(a["artifact"])
        with open(art) as fh:
            payload = fh.read()
    except Exception:
        return 500, artifact_missing_error(a)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    args, aerr = job_args(aid, inputs)
    if aerr:
        return 400, {"error": aerr}
    args["bundle_sha256"] = bundle_sha
    argsb64 = base64.b64encode(json.dumps(args).encode()).decode()
    dest = cfg["targets"][target]
    job_id = "j" + os.urandom(6).hex()
    os.makedirs(JOBS_DIR, exist_ok=True)
    log_path, _ = _job_paths(job_id)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _job_write(job_id, {"job": job_id, "action": aid, "target": target, "status": "running",
                        "started": started, "inputs": safe_inputs})
    audit({"action": aid, "target": target, "inputs": safe_inputs, "result": "STARTED",
           "job": job_id, "client": client})
    push_key = os.path.join(ACTION_KEYS, a["push_key"])
    run_key = os.path.join(ACTION_KEYS, a["key"])

    def run():
        rc = -1
        try:
            with open(log_path, "w") as log:
                log.write(f"== MESH GATEWAY {target}: {bundle_file} ==\n"
                          "-- phase 1/2: push the bundle --\n")
                log.flush()
                hq = subprocess.run(
                    ["ssh", "-i", push_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     "-o", "StrictHostKeyChecking=accept-new", dest,
                     f"have-package {bundle_sha}"],
                    capture_output=True, text=True, timeout=30)
                if hq.returncode == 0 and "HELD" in (hq.stdout or ""):
                    log.write("bundle already staged on the box (hash-named); skipping the stream\n")
                    log.flush()
                    p = hq
                else:
                    with open(src, "rb") as tgz:
                        p = subprocess.run(
                            ["ssh", "-i", push_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                             "-o", "StrictHostKeyChecking=accept-new", dest,
                             f"push-package {bundle_sha} {bundle_file}"],
                            stdin=tgz, capture_output=True, text=True, timeout=1800)
                    log.write((p.stdout or "") + (p.stderr or ""))
                    log.flush()
                if p.returncode != 0:
                    raise RuntimeError("bundle push failed")
                log.write("-- phase 2/2: install (venv from the bundle, radio, TAK input, unit) --\n")
                log.flush()
                p2 = subprocess.Popen(
                    ["ssh", "-i", run_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     "-o", "StrictHostKeyChecking=accept-new", dest,
                     f"deploy-mesh-gateway {digest} {argsb64}"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1)
                p2.stdin.write(payload)
                p2.stdin.close()
                for line in p2.stdout:
                    log.write(line)
                    log.flush()
                rc = p2.wait(timeout=1800)
        except Exception as e:
            try:
                with open(log_path, "a") as log:
                    log.write(f"\nERR job runner: {e}\n")
            except OSError:
                pass
        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _job_write(job_id, {"job": job_id, "action": aid, "target": target,
                            "status": "done" if rc == 0 else "failed", "rc": rc,
                            "started": started, "finished": finished, "inputs": safe_inputs})
        audit({"action": aid, "target": target, "inputs": safe_inputs,
               "result": "OK" if rc == 0 else "ERROR",
               "reason": "" if rc == 0 else f"job rc={rc}", "job": job_id, "client": client})

    threading.Thread(target=run, daemon=True).start()
    return 200, {"status": "started", "job": job_id}


# ---------- server setup wizard (1.16.0) -------------------------------------------------------
# Browser-driven setup of a NEW box, end to end: bootstrap access, the .deb streamed up and
# pushed on, enrolment, provisioning, first credentials, and the bootstrap key shredded. The
# root work happens in console-setup-priv, the console's one sudo path on this box (approved
# 26 Aug 2026) - a five-operation catalogue with hard validation, never a shell. The end state
# matches a hand enrolment exactly: the console keeps only its scoped action keys.
SETUP_DIR = os.environ.get("VANTAGE_CONSOLE_SETUP", "/var/lib/vantage-console/agent/setup")
# The store: the filestore on the console box for everything that gets deployed when building
# and running a TAK server - install packages (.deb), mission packs, map packs, and whatever
# comes next. A folder tree the operator browses, uploads to, downloads from, moves within and
# deletes. Packages get a sha256/arch sidecar in a manifest (the deploy push re-verifies the
# hash); other files are described from the filesystem. The tak-server folder is the package
# library the setup wizard and the deploy push read from - kept as its own name for that path.
STORE_ROOT = os.environ.get("VANTAGE_CONSOLE_STORE", "/var/lib/vantage-console/agent/store")
LIBRARY_DIR = os.environ.get("VANTAGE_CONSOLE_LIBRARY", os.path.join(STORE_ROOT, "tak-server"))
# The link to a box's Vantage Deployed sync surface (the EUD enrolment and pack seam,
# vantage-deployed/docs/adr/003): base URL plus the box-minted admin token. Set from the
# Deployed page; lives in the console's own writable state, never inside a QR.
DEPLOYED_LINK_FILE = os.environ.get("VANTAGE_CONSOLE_DEPLOYED",
                                    os.path.join(os.path.dirname(STORE_ROOT), "deployed.json"))


def load_deployed_link():
    try:
        with open(DEPLOYED_LINK_FILE, encoding="utf-8") as f:
            d = json.load(f)
        url = str(d.get("url") or "").rstrip("/")
        tok = str(d.get("token") or "")
        return (url, tok) if url and tok else ("", "")
    except Exception:
        return ("", "")


def deployed_call(method, subpath, body=None, timeout=8):
    """One call to the linked box's admin surface. Returns (status, bytes); 0 on no link
    or transport failure, with the reason in the bytes."""
    url, tok = load_deployed_link()
    if not url:
        return 0, b'{"error": "no deployed link configured"}'
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + subpath, data=body, method=method,
                                 headers={"X-VD-Admin": tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e2:
        return e2.code, e2.read()
    except Exception as e2:
        return 0, json.dumps({"error": f"unreachable: {e2.__class__.__name__}"}).encode()

STORE_FOLDERS = ["tak-server", "mission-packs", "map-packs", "software"]   # seeded categories
# The knowledge vault: the Deployed vault on this box, managed read-write from the console for
# pre-deployment prep and close-down. The console writes as a member of the vaultsync group,
# always via write-temp-then-replace with group-writable modes, so Syncthing sees an ordinary
# change and carries it to the forward box and the EUD exactly as if it were authored here by
# hand - which, in effect, it is. The sync itself is never touched.
VAULT_ROOT = os.environ.get("VANTAGE_CONSOLE_VAULT", "/srv/vault/Deployed")
VAULT_TEXT_EXT = (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".xml", ".html", ".conf")
VAULT_TEXT_MAX = 2 * 1024 * 1024


def area_root(area):
    return VAULT_ROOT if area == "vault" else STORE_ROOT
STORE_NAME_RE = r"[A-Za-z0-9][A-Za-z0-9._ -]{0,79}"
STORE_FILE_RE = r"[A-Za-z0-9][A-Za-z0-9._ -]{0,119}\.[A-Za-z0-9]{1,10}"
SETUP_HELPER = os.environ.get("VANTAGE_CONSOLE_SETUP_HELPER", "/usr/local/bin/console-setup-priv")
DEB_MAX = int(os.environ.get("VANTAGE_CONSOLE_DEB_MAX", str(2 * 1024 * 1024 * 1024)))
# Docker image tarballs (CloudTAK/MediaMTX, for offline deploy) are larger than a .deb.
IMG_MAX = int(os.environ.get("VANTAGE_CONSOLE_IMG_MAX", str(6 * 1024 * 1024 * 1024)))


def store_ensure():
    os.makedirs(STORE_ROOT, exist_ok=True)
    for f in STORE_FOLDERS:
        try:
            os.makedirs(os.path.join(STORE_ROOT, f), exist_ok=True)
        except OSError:
            pass


def store_resolve(rel, area="store"):
    """A relative path to an absolute path GUARANTEED inside the chosen area's root (the store
    or the knowledge vault), or None. Every segment is name-validated and the realpath is
    checked to stay under the root, so no '..', symlink or absolute path can escape."""
    rel = (rel or "").strip().strip("/")
    root = os.path.realpath(area_root(area))
    if rel == "":
        return root
    parts = rel.split("/")
    for p in parts:
        if p in ("", ".", "..") or not re.fullmatch(STORE_NAME_RE + r"|" + STORE_FILE_RE, p):
            return None
    cand = os.path.realpath(os.path.join(root, *parts))
    return cand if cand == root or cand.startswith(root + os.sep) else None


def store_list(rel, area="store"):
    if area == "store":
        store_ensure()
    base = store_resolve(rel, area)
    if base is None or not os.path.isdir(base):
        return None
    relclean = (rel or "").strip().strip("/")
    in_pkgs = area == "store" and relclean == "tak-server"
    man = {e.get("file"): e for e in library_manifest()} if in_pkgs else {}
    folders, files = [], []
    for name in sorted(os.listdir(base), key=str.lower):
        if name.startswith(".") or name in ("library.json", "library.json.tmp"):
            continue
        full = os.path.join(base, name)
        if os.path.isdir(full):
            try:
                n = len([x for x in os.listdir(full) if not x.startswith(".")])
            except OSError:
                n = 0
            folders.append({"name": name, "items": n})
        elif os.path.isfile(full):
            st = os.stat(full)
            e = {"name": name, "bytes": st.st_size,
                 "path": (relclean + "/" + name).strip("/"),
                 "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ")}
            m = man.get(name)
            if m:
                e["arch"] = m.get("arch")
                e["sha256"] = m.get("sha256")
            files.append(e)
    return {"path": relclean, "crumb": relclean.split("/") if relclean else [],
            "folders": folders, "files": files}


def store_mkdir(rel, name, client, area="store"):
    if not re.fullmatch(STORE_NAME_RE, str(name or "")):
        return 400, {"error": "folder name must be letters, numbers, space, dot, dash, underscore"}
    parent = store_resolve(rel, area)
    if parent is None or not os.path.isdir(parent):
        return 404, {"error": "no such folder"}
    target = os.path.join(parent, name)
    if os.path.exists(target):
        return 400, {"error": "a folder with that name is already there"}
    os.makedirs(target)
    if area == "vault":
        os.chmod(target, 0o2775)
    audit({"action": f"{area}-mkdir", "target": f"{rel}/{name}".strip("/"), "result": "OK",
           "client": client})
    return 200, {"status": "created"}


def vault_trash(p, rel, root):
    """Soft-delete for the vault: the note moves into .trash with an index entry, so a
    slip of the finger is a ten-second undo, not a loss that syncs outward."""
    tdir = os.path.join(root, ".trash")
    os.makedirs(tdir, exist_ok=True)
    tname = f"{int(__import__('time').time())}-{os.path.basename(p)}"
    os.replace(p, os.path.join(tdir, tname))
    idx_path = os.path.join(tdir, "index.json")
    try:
        idx = json.load(open(idx_path))
    except Exception:
        idx = []
    idx.append({"file": tname, "orig": (rel or "").strip("/")})
    json.dump(idx[-50:], open(idx_path, "w"))
    return tname


def vault_restore(data, client):
    """Undo one vault deletion: the trashed note goes back where it was."""
    tname = str(data.get("file", ""))
    if not re.fullmatch(r"[0-9]+-(" + STORE_FILE_RE + r"|" + STORE_NAME_RE + r")", tname):
        return 400, {"error": "bad trash reference"}
    root = store_resolve("", "vault")
    tdir = os.path.join(root, ".trash")
    idx_path = os.path.join(tdir, "index.json")
    try:
        idx = json.load(open(idx_path))
    except Exception:
        idx = []
    hit = next((x for x in idx if x.get("file") == tname), None)
    src = os.path.join(tdir, tname)
    if not hit or not os.path.exists(src):
        return 404, {"error": "nothing to restore"}
    dst = store_resolve(hit["orig"], "vault")
    if dst is None:
        return 400, {"error": "bad original path"}
    if os.path.exists(dst):
        return 400, {"error": "a note already exists at the original path"}
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)
    json.dump([x for x in idx if x.get("file") != tname], open(idx_path, "w"))
    audit({"action": "vault-restore", "target": hit["orig"], "result": "OK",
           "client": client})
    return 200, {"restored": hit["orig"]}


def store_delete(rel, client, area="store"):
    p = store_resolve(rel, area)
    root = os.path.realpath(area_root(area))
    if p is None or p == root:
        return 400, {"error": "bad path"}
    if os.path.isfile(p):
        if area == "vault":
            tname = vault_trash(p, rel, root)
            audit({"action": "vault-delete", "target": (rel or "").strip("/"),
                   "result": "OK", "trash": tname, "client": client})
            return 200, {"status": "trashed", "trash": tname}
        os.remove(p)
        # drop any package manifest entry for this basename
        man = [e for e in library_manifest() if e.get("file") != os.path.basename(p)]
        _write_manifest(man)
    elif os.path.isdir(p):
        if area == "store" and p.rsplit("/", 1)[-1] in STORE_FOLDERS:
            return 400, {"error": "that is a top-level category and cannot be removed"}
        if area == "vault":
            n_notes = sum(len([f for f in fs if not f.startswith(".")])
                          for _, _, fs in os.walk(p))
            tname = vault_trash(p, rel, root)
            audit({"action": "vault-delete", "target": (rel or "").strip("/"),
                   "result": "OK", "trash": tname, "folder": True,
                   "notes": n_notes, "client": client})
            return 200, {"status": "trashed", "trash": tname, "folder": True,
                         "notes": n_notes}
        if any(not x.startswith(".") for x in os.listdir(p)):
            return 400, {"error": "the folder is not empty"}
        os.rmdir(p)
    else:
        return 404, {"error": "no such item"}
    audit({"action": f"{area}-delete", "target": (rel or "").strip("/"), "result": "OK",
           "client": client})
    return 200, {"status": "deleted"}


def store_move(src_rel, dst_folder_rel, client, area="store"):
    src = store_resolve(src_rel, area)
    if src is None or not os.path.exists(src):
        return 404, {"error": "no such note or folder"}
    is_dir = os.path.isdir(src)
    # top-level destination is the vault/store root; a named folder must exist
    dst_dir = area_root(area) if (dst_folder_rel or "") == "" \
        else store_resolve(dst_folder_rel, area)
    if dst_dir is None or not os.path.isdir(dst_dir):
        return 404, {"error": "no such destination folder"}
    src_real, dst_real = os.path.realpath(src), os.path.realpath(dst_dir)
    if is_dir and (dst_real == src_real or dst_real.startswith(src_real + os.sep)):
        return 400, {"error": "cannot move a folder into itself"}
    if os.path.dirname(src_real) == dst_real:
        return 400, {"error": "it is already there"}
    dst = os.path.join(dst_dir, os.path.basename(src))
    if os.path.exists(dst):
        return 400, {"error": "something with that name is already in that folder"}
    os.rename(src, dst)
    audit({"action": f"{area}-move", "target": f"{src_rel} -> {dst_folder_rel or '(top)'}",
           "kind": "folder" if is_dir else "note", "result": "OK", "client": client})
    return 200, {"status": "moved"}


VAULT_SKIP_DIRS = {".obsidian", ".git", ".trash", ".smart-env", ".stfolder", ".stversions",
                   "node_modules"}


def vault_tree():
    """The whole vault as one recursive tree - folders and files with paths - for the viewer's
    sidebar. The vault is markdown-note sized, so one call carries it all."""
    root = os.path.realpath(VAULT_ROOT)

    def walk(d, rel):
        folders, files = [], []
        try:
            names = sorted(os.listdir(d), key=str.lower)
        except OSError:
            return {"folders": [], "files": []}
        for name in names:
            if name.startswith(".") or name in VAULT_SKIP_DIRS \
               or name.endswith(".console-tmp"):
                continue
            full = os.path.join(d, name)
            r = (rel + "/" + name).strip("/")
            if os.path.isdir(full):
                sub = walk(full, r)
                folders.append({"name": name, "path": r,
                                "folders": sub["folders"], "files": sub["files"]})
            elif os.path.isfile(full):
                files.append({"name": name, "path": r,
                              "bytes": os.path.getsize(full)})
        return {"folders": folders, "files": files}

    return walk(root, "")


def vault_read(rel):
    """One text file's content, for the editor. Text extensions only, size-capped."""
    p = store_resolve(rel, "vault")
    if p is None or not os.path.isfile(p):
        return 404, {"error": "no such file"}
    if not p.lower().endswith(VAULT_TEXT_EXT):
        return 400, {"error": "not an editable text file - download it instead"}
    if os.path.getsize(p) > VAULT_TEXT_MAX:
        return 400, {"error": "too large for the editor - download it instead"}
    with open(p, encoding="utf-8", errors="replace") as fh:
        return 200, {"path": (rel or "").strip("/"), "content": fh.read()}


# ---- vault ontologies (1.49.0) --------------------------------------------------------------
# A knowledge vault is only as useful as its shape. An ontology is a starting shape: folders
# and seed notes an operator picks so the vault is ready to fill - and, filled, ready for an
# agent to pull down over vault_search / vault_read. The deployment pack is the worked
# example; operators can also paste their own ontology as JSON.
def _seed(title, body):
    return f"---\ntitle: {title}\n---\n\n{body}\n"


VAULT_ONTOLOGIES = {
    "deployment": {
        "name": "Deployment pack",
        "desc": "A ready-to-fill pack for a deployment: orders, locations, people, "
                "equipment, reports and reference. The shape an agent reads to answer "
                "'what do I need to know here?'.",
        "tree": {
            "00 Orders": {"Mission Overview.md": _seed("Mission Overview",
                "## Situation\n\n## Mission\n\n## Execution\n\n## Command & signal\n"),
                          "Comms Plan.md": _seed("Comms Plan",
                "| Net | Purpose | Primary | Alternate |\n|---|---|---|---|\n|  |  |  |  |\n")},
            "01 Locations": {"Named Areas.md": _seed("Named Areas",
                "Named areas of interest, with grids. One heading per location - the agent "
                "reads these to answer questions about the ground.\n")},
            "02 People": {"Key Contacts.md": _seed("Key Contacts",
                "| Name | Role | Call sign | Contact |\n|---|---|---|---|\n|  |  |  |  |\n")},
            "03 Equipment": {},
            "04 Reports": {"SITREP template.md": _seed("SITREP template",
                "## Line 1 - Situation\n## Line 2 - Own actions\n## Line 3 - Next\n")},
            "05 Reference": {"Glossary.md": _seed("Glossary",
                "Terms and abbreviations used in this pack.\n")},
        },
    },
    "exercise": {
        "name": "Exercise pack",
        "desc": "For running or supporting an exercise: serials, injects, EXCON and a "
                "lessons log.",
        "tree": {
            "Serials": {"Master Events List.md": _seed("Master Events List",
                "| Serial | Time | Event | DTG |\n|---|---|---|---|\n|  |  |  |  |\n")},
            "Injects": {}, "EXCON": {},
            "Lessons": {"Lessons Log.md": _seed("Lessons Log",
                "| Observation | Insight | Recommendation |\n|---|---|---|\n|  |  |  |\n")},
        },
    },
    "blank": {
        "name": "Blank with guidance",
        "desc": "One README explaining how the vault feeds an agent. Build the rest yourself.",
        "tree": {"README.md": _seed("This vault",
            "Notes you add here become the estate agent's knowledge. An agent connected to "
            "the console can search and read these notes (vault_search / vault_read), so the "
            "clearer they are, the better it answers. Use folders for topics, one note per "
            "thing.\n")},
    },
}


def scaffold_ontology(data, client):
    key = str(data.get("ontology", ""))
    onto = VAULT_ONTOLOGIES.get(key)
    if not onto and key != "custom":
        return 400, {"error": "unknown ontology"}
    if key == "custom":
        tree = data.get("tree")
        if not isinstance(tree, dict):
            return 400, {"error": "custom ontology needs a tree object"}
    else:
        tree = onto["tree"]
    # a named deployment/exercise: the template lands inside a folder of that name, not
    # dumped into the vault root
    prefix = str(data.get("prefix", "")).strip().strip("/")
    if prefix:
        for seg in prefix.split("/"):
            if not re.fullmatch(STORE_NAME_RE, seg):
                return 400, {"error": "name: letters, numbers and simple punctuation"}
        tree = {prefix: tree}
    # the vault root may not exist yet on a fresh console: create it, group-writable like
    # every other vault write, so the box's own vault tooling keeps working
    root = area_root("vault")
    try:
        os.makedirs(root, exist_ok=True)
        os.chmod(root, 0o775)
    except Exception:
        ok, _txt = setup_helper("vaultdir", "vault")
        if not ok or not os.path.isdir(root):
            return 500, {"error": "the vault folder does not exist and could not be "
                                  "created - re-run the installer on this box"}
    created, skipped = [], []

    def walk(node, rel):
        for name, child in node.items():
            if isinstance(child, dict):
                if not re.fullmatch(STORE_NAME_RE, name):
                    skipped.append(name + "/ (bad name)"); continue
                folder = store_resolve((rel + "/" + name).strip("/"), "vault")
                if folder is None:
                    skipped.append(name + "/ (rejected)"); continue
                existed = os.path.isdir(folder)
                os.makedirs(folder, exist_ok=True)
                try:
                    os.chmod(folder, 0o775)
                except Exception:
                    pass
                (skipped if existed else created).append(
                    (rel + "/" + name).strip("/") + "/")
                walk(child, (rel + "/" + name).strip("/"))
            else:
                if not re.fullmatch(STORE_FILE_RE, name) \
                   or not name.lower().endswith(VAULT_TEXT_EXT):
                    skipped.append(name + " (bad name)"); continue
                path = (rel + "/" + name).strip("/")
                full = store_resolve(path, "vault")
                if full is None:
                    skipped.append(name + " (rejected)"); continue
                if os.path.exists(full):
                    skipped.append(path + " (exists)"); continue
                code, res = vault_save(path, str(child)[:VAULT_TEXT_MAX], client)
                (created if code == 200 else skipped).append(path)

    walk(tree, "")
    audit({"action": "vault-scaffold", "ontology": key, "created": len(created),
           "result": "OK", "client": client})
    return 200, {"created": created, "skipped": skipped, "ontology": key}


def vault_graph(scope=""):
    """The Knowledge Vault as a graph, scoped: pass a folder and the graph is that
    deployment's shape alone - the whole vault at once is a hairball the moment the
    vault gets real."""
    scope = (scope or "").strip().strip("/")
    root = store_resolve(scope, "vault")
    nodes, edges = [], []
    if not root or not os.path.isdir(root):
        return {"nodes": [], "edges": [], "scope": scope}
    vroot = store_resolve("", "vault")
    by_base = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, vroot)
        rel_dir = "" if rel_dir == "." else rel_dir
        for d in dirnames:
            rel = (rel_dir + "/" + d).strip("/")
            nodes.append({"id": rel, "label": d, "kind": "folder"})
            edges.append({"a": rel_dir, "b": rel, "kind": "in"})
        for fn in sorted(filenames):
            if fn.startswith(".") or not fn.lower().endswith((".md", ".txt")):
                continue
            rel = (rel_dir + "/" + fn).strip("/")
            base = os.path.splitext(fn)[0].lower()
            nodes.append({"id": rel, "label": os.path.splitext(fn)[0], "kind": "note"})
            edges.append({"a": rel_dir, "b": rel, "kind": "in"})
            by_base.setdefault(base, rel)
    # wikilinks: [[Target]] or [[Target|shown]] resolving to any note by basename
    for n in nodes:
        if n["kind"] != "note":
            continue
        full = store_resolve(n["id"], "vault")
        try:
            with open(full, errors="ignore") as fh:
                text = fh.read(120000)
        except Exception:
            continue
        for m in re.finditer(r"\[\[([^\]|#]+)", text):
            tgt = by_base.get(m.group(1).strip().lower())
            if tgt and tgt != n["id"]:
                edges.append({"a": n["id"], "b": tgt, "kind": "link"})
    # the root of this view: the scope folder, or the vault itself
    nodes.insert(0, {"id": scope, "label": (scope.rsplit("/", 1)[-1] if scope
                                            else "vault"), "kind": "root"})
    return {"nodes": nodes, "edges": edges, "scope": scope}


def agent_vault_write(rel, content, mode, client):
    """One note written by an agent. create refuses to overwrite; update replaces;
    append adds to the end. Same path safety and group-writable modes as vault_save."""
    rel = (rel or "").strip().strip("/")
    base = os.path.basename(rel)
    if not rel or not re.fullmatch(STORE_FILE_RE, base) \
       or not base.lower().endswith(VAULT_TEXT_EXT):
        return 400, {"error": "path must end in a text filename, e.g. Orders.md"}
    if not isinstance(content, str) or len(content.encode()) > VAULT_TEXT_MAX:
        return 400, {"error": "content missing or too large"}
    full = store_resolve(rel, "vault")
    if full is None:
        return 400, {"error": "path escapes the vault"}
    exists = os.path.exists(full)
    if mode == "create" and exists:
        return 400, {"error": "that note exists - use mode update or append"}
    if mode == "append" and exists:
        try:
            with open(full, errors="ignore") as fh:
                content = fh.read() + ("\n\n" if not fh else "") + content
        except Exception:
            pass
    parent = os.path.dirname(full)
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
        try:
            os.chmod(parent, 0o775)
        except Exception:
            pass
    tmp = os.path.join(parent, f".{base}.agent-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o664)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    os.chmod(tmp, 0o664)
    os.replace(tmp, full)
    audit({"action": "vault-write", "target": rel, "mode": mode,
           "result": "OK", "client": client})
    return 200, {"path": rel, "mode": ("updated" if exists else "created")}


def vault_rename(from_rel, to_name, client):
    """Rename one vault note in place. The new name is a basename (no path); the extension
    is kept if the caller omits one. Refuses to overwrite."""
    src = store_resolve(from_rel, "vault")
    if not src or not os.path.exists(src):
        return 404, {"error": "no such note or folder"}
    is_dir = os.path.isdir(src)
    to_name = (to_name or "").strip().strip("/")
    if "/" in to_name:
        return 400, {"error": "just the new name - drag onto a folder to move"}
    if is_dir:
        if not re.fullmatch(STORE_NAME_RE, to_name):
            return 400, {"error": "letters, numbers and simple punctuation"}
    else:
        if not re.search(r"[.][A-Za-z0-9]{1,10}$", to_name):
            to_name += os.path.splitext(src)[1] or ".md"
        if not re.fullmatch(STORE_FILE_RE, to_name) or \
           not to_name.lower().endswith(VAULT_TEXT_EXT):
            return 400, {"error": "letters, numbers and simple punctuation, "
                                  "ending .md or .txt"}
    dst = os.path.join(os.path.dirname(src), to_name)
    if os.path.exists(dst):
        return 400, {"error": "a note with that name already exists here"}
    os.rename(src, dst)
    new_rel = os.path.relpath(dst, store_resolve("", "vault"))
    audit({"action": "vault-rename", "target": from_rel, "to": new_rel,
           "result": "OK", "client": client})
    return 200, {"path": new_rel}


def vault_save(rel, content, client):
    """Create or update one text file. Always write-temp-then-replace with group-writable
    modes: the replace is what Syncthing sees as an ordinary change, and the group mode is what
    lets the vault's own tools keep working on the file afterwards."""
    rel = (rel or "").strip().strip("/")
    base = os.path.basename(rel)
    if not rel or not re.fullmatch(STORE_FILE_RE, base) \
       or not base.lower().endswith(VAULT_TEXT_EXT):
        return 400, {"error": "path must end in a text filename, e.g. note.md"}
    if not isinstance(content, str) or len(content.encode()) > VAULT_TEXT_MAX:
        return 400, {"error": "content missing or too large"}
    parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    parent = store_resolve(parent_rel, "vault")
    if parent is None or not os.path.isdir(parent):
        return 404, {"error": "no such folder"}
    target = os.path.join(parent, base)
    tmp = os.path.join(parent, f".{base}.console-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o664)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    os.chmod(tmp, 0o664)
    created = not os.path.exists(target)
    os.replace(tmp, target)
    audit({"action": "vault-save", "target": rel,
           "result": "OK", "reason": "created" if created else "updated", "client": client})
    return 200, {"status": "created" if created else "updated", "path": rel}


def _write_manifest(man):
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    tmp = os.path.join(LIBRARY_DIR, "library.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(man, fh, indent=1)
    os.replace(tmp, os.path.join(LIBRARY_DIR, "library.json"))


def library_manifest():
    try:
        with open(os.path.join(LIBRARY_DIR, "library.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def library_record(entry):
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    man = [e for e in library_manifest() if e.get("file") != entry["file"]]
    man.append(entry)
    tmp = os.path.join(LIBRARY_DIR, "library.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(man, fh, indent=1)
    os.replace(tmp, os.path.join(LIBRARY_DIR, "library.json"))


def library_list():
    """The manifest, kept honest: entries whose file has gone are dropped on read."""
    out = []
    for e in library_manifest():
        p = os.path.join(LIBRARY_DIR, str(e.get("file", "")))
        if os.path.isfile(p):
            out.append(e)
    return out


def library_usage():
    """Finder-style status line: package count and total bytes on the shelf."""
    lst = library_list()
    return {"count": len(lst), "bytes": sum(int(e.get("bytes", 0) or 0) for e in lst)}


def library_delete(fname, client):
    """Remove one package and its manifest entry. The file is the console's own upload copy;
    nothing on any box is touched."""
    if not re.fullmatch(RE_DEBNAME, str(fname or "")):
        return 400, {"error": "bad filename"}
    p = os.path.join(LIBRARY_DIR, fname)
    if not os.path.isfile(p):
        return 404, {"error": "no such package"}
    os.remove(p)
    man = [e for e in library_manifest() if e.get("file") != fname]
    tmp = os.path.join(LIBRARY_DIR, "library.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(man, fh, indent=1)
    os.replace(tmp, os.path.join(LIBRARY_DIR, "library.json"))
    audit({"action": "library-delete", "target": fname, "result": "OK", "client": client})
    return 200, {"status": "deleted"}


def start_library_fetch(data, client):
    """Download a release straight into the library as a job - the console box usually has far
    better bandwidth than the operator's browser uplink, so this is how a 600MB TAK release
    should arrive. Plain https only; the ar magic and a size cap still gate what lands, and the
    sha256 is computed here and recorded, so what the wizard later pushes is exactly what was
    fetched."""
    import urllib.request as _rq
    url = str(data.get("url", ""))
    fname = str(data.get("file", ""))
    arch = str(data.get("arch", "amd64"))
    if not re.fullmatch(r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{5,500}", url):
        return 400, {"error": "url must be https"}
    if not fname:
        fname = url.split("?")[0].rstrip("/").split("/")[-1]
    if not re.fullmatch(RE_DEBNAME, fname):
        return 400, {"error": "filename must be [A-Za-z0-9._-] ending .deb (set one explicitly "
                              "if the URL does not end in the name)"}
    if not re.fullmatch(r"[a-z0-9]{1,16}", arch):
        return 400, {"error": "bad arch"}
    job_id = "j" + os.urandom(6).hex()
    os.makedirs(JOBS_DIR, exist_ok=True)
    log_path, _ = _job_paths(job_id)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _job_write(job_id, {"job": job_id, "action": "library-fetch", "target": fname,
                        "status": "running", "started": started})
    audit({"action": "library-fetch", "target": url[:120], "result": "STARTED",
           "job": job_id, "client": client})

    def run():
        rc = 1
        with open(log_path, "w") as log:
            def say(m):
                log.write(m.rstrip() + "\n")
                log.flush()
            os.makedirs(LIBRARY_DIR, exist_ok=True)
            dest = os.path.join(LIBRARY_DIR, fname)
            try:
                say(f"fetching {url}")
                req = _rq.Request(url, headers={"User-Agent": "vantage-console/" + VERSION})
                sha = hashlib.sha256()
                got, first = 0, b""
                with _rq.urlopen(req, timeout=60) as r:
                    total = int(r.headers.get("Content-Length", "0") or 0)
                    if total > DEB_MAX:
                        raise RuntimeError(f"{total} bytes is over the {DEB_MAX} cap")
                    with open(dest, "wb") as fh:
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            if not first:
                                first = chunk[:8]
                                if not first.startswith(b"!<arch>"):
                                    raise RuntimeError("that is not a Debian package "
                                                       "(the URL may need a login - download "
                                                       "by hand and upload instead)")
                            sha.update(chunk)
                            fh.write(chunk)
                            got += len(chunk)
                            if got > DEB_MAX:
                                raise RuntimeError("download exceeded the size cap")
                            if got % (64 << 20) < (1 << 20):
                                say(f"{got >> 20} MB" + (f" of {total >> 20} MB" if total else ""))
                if total and got != total:
                    raise RuntimeError(f"short download: {got} of {total} bytes")
                entry = {"file": fname, "sha256": sha.hexdigest(), "bytes": got, "arch": arch,
                         "uploaded": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "source": url[:200]}
                library_record(entry)
                say(f"OK {fname} in the library: {got >> 20} MB, sha256 {sha.hexdigest()}")
                rc = 0
            except Exception as e:
                say(f"ERR {e}")
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                except OSError:
                    pass
            finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _job_write(job_id, {"job": job_id, "action": "library-fetch", "target": fname,
                                "status": "done" if rc == 0 else "failed", "rc": rc,
                                "started": started, "finished": finished})
            audit({"action": "library-fetch", "target": fname,
                   "result": "OK" if rc == 0 else "ERROR", "job": job_id, "client": client})

    threading.Thread(target=run, daemon=True).start()
    return 200, {"status": "started", "job": job_id}

RE_KEYNAME = r"[a-z0-9-]{1,24}"
RE_DEST = r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+"
RE_DEBNAME = r"[A-Za-z0-9._-]{1,80}\.deb"
RE_IMGNAME = r"[A-Za-z0-9._-]{1,80}\.tar(\.gz)?"


def setup_helper(*args, timeout=60):
    """One call to the root helper. Returns (ok, text). The helper validates everything again;
    this side validates first so bad input never even reaches sudo."""
    try:
        p = subprocess.run(["sudo", "-n", SETUP_HELPER, *args],
                           capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return False, f"setup helper failed: {e}"
    text = ((p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")).strip()
    return p.returncode == 0, text


def setup_api(path, data, client):
    """The small, synchronous setup calls (keygen / userkey / test). The long run is a job."""
    # baseline carries no key name, so it must answer BEFORE the key-name gate below
    if path == "/api/setup/baseline":
        comps = data.get("components") or {}
        if not isinstance(comps, dict):
            return 400, {"error": "components must be an object"}
        allowed = {"tak-health", "takserver", "cloudtak", "mediamtx"}
        clean = {k: str(v).strip() for k, v in comps.items()
                 if k in allowed and str(v).strip()}
        blob = base64.b64encode(json.dumps({"components": clean}).encode()).decode()
        # the helper validates arg 2 as a key name; pass a dummy, blob is arg 3 (as instance does)
        ok, out = setup_helper("baseline", "baseline", blob)
        audit({"action": "set-baseline", "result": "OK" if ok else "ERROR",
               "detail": ",".join(f"{k}={v}" for k, v in clean.items())[:200], "client": client})
        return (200, {"components": clean}) if ok else (400, {"error": out[:200]})
    name = str(data.get("name", ""))
    if not re.fullmatch(RE_KEYNAME, name):
        return 400, {"error": "key name must be [a-z0-9-], max 24"}
    if path == "/api/setup/keygen":
        ok, out = setup_helper("keygen", name)
        audit({"action": "setup-keygen", "target": name, "result": "OK" if ok else "ERROR",
               "client": client})
        return (200, {"pubkey": out}) if ok else (502, {"error": out[:300]})
    if path == "/api/setup/userkey":
        ok, out = setup_helper("userkey", name)
        audit({"action": "setup-userkey", "target": name, "result": "OK" if ok else "ERROR",
               "client": client})
        return (200, {"status": "adopted"}) if ok else (400, {"error": out[:300]})
    if path == "/api/setup/forget-host":
        # a rebuilt box presents a new SSH host key, which the console must refuse by
        # default; forgetting the old key is a deliberate operator act, not automatic
        host = str(data.get("host", ""))
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
            return 400, {"error": "bad host"}
        ok, out = setup_helper("forget-host", name, host)
        return (200, {"forgot": host}) if ok else (400, {"error": out[:200]})
    if path == "/api/setup/self-target":
        # "Deploy on this box": the console's own box becomes the target. The root helper
        # mints and self-authorises the bootstrap key; nothing is pasted, and the same
        # shred-on-success lifecycle applies. Everything downstream is the normal path,
        # aimed at root@127.0.0.1.
        ok, out = setup_helper("self-authorize", name)
        audit({"action": "self-target", "target": name, "result": "OK" if ok else "ERROR",
               "client": client})
        if not ok:
            return 400, {"error": out[:200]}
        host = (out.strip().splitlines() or ["this-box"])[-1].strip()
        return 200, {"dest": "root@127.0.0.1", "host": host}
    if path == "/api/setup/password":
        # set or change the operator sign-in password. When one exists, the request only
        # arrives here with a valid session (the gate), and we still require the current
        # password - a walked-past-the-screen change should not be possible.
        new = str(data.get("new", ""))
        if len(new) < 12:
            return 400, {"error": "use at least 12 characters"}
        if auth_configured() and not verify_operator_password(str(data.get("current", ""))):
            audit({"action": "set-password", "result": "DENIED", "client": client})
            return 403, {"error": "current password is wrong"}
        blob = base64.b64encode(json.dumps(hash_password(new)).encode()).decode()
        # ONE operator password: the same secret signs you in and confirms privileged
        # actions. The helper writes both stores (auth.json, and the passphrase record
        # in actions.json) so a fresh install is never locked out of its own actions.
        psalt = secrets.token_hex(8)
        pblob = base64.b64encode(json.dumps(
            {"salt": psalt,
             "sha256": hashlib.sha256((psalt + new).encode()).hexdigest()}).encode()).decode()
        ok, out = setup_helper("set-password", "instance", blob, pblob)
        audit({"action": "set-password", "result": "OK" if ok else "ERROR", "client": client})
        return (200, {"set": True}) if ok else (400, {"error": out[:200]})
    if path == "/api/setup/unenrol":
        # remove a server that was deleted, destroyed or reset: console-side only, so it
        # works whether or not the box still exists. Passphrase-gated - it stops the box
        # being monitored and unbinds its actions.
        if not verify_passphrase(load_actions_config(), data.get("passphrase")):
            return 403, {"error": "operator passphrase required to remove a server"}
        ok, out = setup_helper("unenrol", name)
        audit({"action": "unenrol", "target": name, "result": "OK" if ok else "ERROR",
               "client": client})
        return (200, {"removed": name}) if ok else (400, {"error": out[:200]})
    if path == "/api/setup/rename":
        # operator renames a server's display label; the root helper rewrites
        # targets.json under the enrol lock and triggers a poll so it shows at once
        label = str(data.get("label", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9()., _-]{1,40}", label):
            return 400, {"error": "label must be 1-40 chars: letters, digits, ()., _ -"}
        ok, out = setup_helper("rename", name,
                               base64.b64encode(label.encode()).decode())
        return (200, {"renamed": name, "label": label}) if ok else (400, {"error": out[:200]})
    if path == "/api/setup/have":
        # read-only: does a bootstrap key already exist under this name? A reloaded
        # wizard page uses this to rediscover a key adopted before the reload.
        ok, out = setup_helper("have", name)
        return 200, {"held": ok and out.strip() == "HELD"}
    if path == "/api/setup/test":
        dest = str(data.get("dest", ""))
        if not re.fullmatch(RE_DEST, dest):
            return 400, {"error": "destination must look like user@host"}
        ok, out = setup_helper("test", name, dest, timeout=30)
        audit({"action": "setup-test", "target": dest, "result": "OK" if ok else "ERROR",
               "client": client})
        if not ok:
            return 502, {"error": out[:300]}
        info = {}
        for ln in out.splitlines():
            k, _, v = ln.partition(" ")
            info[k.lower()] = v
        return 200, {"host": info.get("host", "?"), "user": info.get("user", "?"),
                     "os": info.get("os", "?"), "addr": info.get("addr", "")}
    return 404, {"error": "not found"}


def save_upload(handler, path):
    """Stream a request body to the setup dir. /api/setup/upload takes the .deb (validated by
    name and ar magic); /api/setup/upload-key takes a private key the operator chose to provide,
    saved as <name>.userkey for the root helper to adopt and shred."""
    n = int(handler.headers.get("Content-Length", "0") or 0)
    os.makedirs(SETUP_DIR, exist_ok=True)
    if path == "/api/library/upload":
        fname = str(handler.headers.get("X-Filename", ""))
        arch = str(handler.headers.get("X-Arch", "amd64"))
        if not re.fullmatch(RE_DEBNAME, fname):
            return 400, {"error": "filename must be [A-Za-z0-9._-] ending .deb"}
        if not re.fullmatch(r"[a-z0-9]{1,16}", arch):
            return 400, {"error": "arch must be a short lowercase token, e.g. amd64 or arm64"}
        if not (0 < n <= DEB_MAX):
            return 400, {"error": f"size must be 1..{DEB_MAX} bytes"}
        os.makedirs(LIBRARY_DIR, exist_ok=True)
        dest = os.path.join(LIBRARY_DIR, fname)
        sha = hashlib.sha256()
        first = b""
        with open(dest, "wb") as fh:
            remaining = n
            while remaining > 0:
                chunk = handler.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                if not first:
                    first = chunk[:8]
                sha.update(chunk)
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining != 0:
            os.remove(dest)
            return 400, {"error": "upload ended early"}
        if not first.startswith(b"!<arch>"):
            os.remove(dest)
            return 400, {"error": "that is not a Debian package (.deb)"}
        entry = {"file": fname, "sha256": sha.hexdigest(), "bytes": n, "arch": arch,
                 "uploaded": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        library_record(entry)
        return 200, entry
    if path == "/api/library/upload-image":
        # a docker-save image tarball for a module (cloudtak/mediamtx), held for OFFLINE
        # deploy. Larger than a .deb; validated as a tar/gzip by magic and re-checked on the
        # box before docker load. Version is what the baseline names.
        fname = str(handler.headers.get("X-Filename", ""))
        module = str(handler.headers.get("X-Module", ""))
        version = str(handler.headers.get("X-Version", "")).strip()
        if not re.fullmatch(RE_IMGNAME, fname):
            return 400, {"error": "filename must be [A-Za-z0-9._-] ending .tar or .tar.gz"}
        if module not in ("cloudtak", "mediamtx"):
            return 400, {"error": "module must be cloudtak or mediamtx"}
        if version and not re.fullmatch(r"[A-Za-z0-9._+-]{1,40}", version):
            return 400, {"error": "version must be [A-Za-z0-9._+-], max 40"}
        if not (0 < n <= IMG_MAX):
            return 400, {"error": f"size must be 1..{IMG_MAX} bytes"}
        os.makedirs(LIBRARY_DIR, exist_ok=True)
        dest = os.path.join(LIBRARY_DIR, fname)
        sha, head = hashlib.sha256(), b""
        with open(dest, "wb") as fh:
            remaining = n
            while remaining > 0:
                chunk = handler.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                if len(head) < 512:
                    head += chunk[:512 - len(head)]
                sha.update(chunk)
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining != 0:
            os.remove(dest)
            return 400, {"error": "upload ended early"}
        is_gzip = head[:2] == b"\x1f\x8b"
        is_tar = len(head) >= 262 and head[257:262] == b"ustar"
        if not (is_gzip or is_tar):
            os.remove(dest)
            return 400, {"error": "that is not a tar or gzip archive"}
        entry = {"file": fname, "sha256": sha.hexdigest(), "bytes": n, "kind": "image",
                 "module": module, "version": version,
                 "uploaded": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        library_record(entry)
        return 200, entry
    if path == "/api/store/upload":
        # any file, into any store or vault folder. A .deb in the store still gets the ar-magic
        # check and a manifest sidecar (so it stays deploy-pushable); everything else just lands.
        folder = str(handler.headers.get("X-Path", ""))
        fname = str(handler.headers.get("X-Filename", ""))
        arch = str(handler.headers.get("X-Arch", "amd64"))
        area = "vault" if handler.headers.get("X-Area") == "vault" else "store"
        base = store_resolve(folder, area)
        if base is None or not os.path.isdir(base):
            return 400, {"error": "no such folder"}
        if not re.fullmatch(STORE_FILE_RE, fname):
            return 400, {"error": "filename must be a name with an extension, e.g. pack.zip"}
        if not (0 < n <= DEB_MAX):
            return 400, {"error": f"size must be 1..{DEB_MAX} bytes"}
        is_deb = area == "store" and fname.lower().endswith(".deb")
        dest = os.path.join(base, fname)
        if os.path.exists(dest):
            return 400, {"error": "a file with that name is already there"}
        sha = hashlib.sha256()
        first = b""
        with open(dest, "wb") as fh:
            remaining = n
            while remaining > 0:
                chunk = handler.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                if not first:
                    first = chunk[:8]
                sha.update(chunk)
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining != 0:
            os.remove(dest)
            return 400, {"error": "upload ended early"}
        if is_deb and not first.startswith(b"!<arch>"):
            os.remove(dest)
            return 400, {"error": "named .deb but not a Debian package"}
        if area == "vault":
            os.chmod(dest, 0o664)
        entry = {"name": fname, "bytes": n, "path": (folder.strip("/") + "/" + fname).strip("/")}
        if is_deb and os.path.realpath(base) == os.path.realpath(LIBRARY_DIR):
            library_record({"file": fname, "sha256": sha.hexdigest(), "bytes": n,
                            "arch": arch if re.fullmatch(r"[a-z0-9]{1,16}", arch) else "amd64",
                            "uploaded": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        return 200, entry
    if path == "/api/setup/upload-key":
        name = str(handler.headers.get("X-Keyname", ""))
        if not re.fullmatch(RE_KEYNAME, name):
            return 400, {"error": "key name must be [a-z0-9-], max 24"}
        if not (0 < n <= 65536):
            return 400, {"error": "that does not look like a private key"}
        body = handler.rfile.read(n)
        dest = os.path.join(SETUP_DIR, name + ".userkey")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, body)
        os.close(fd)
        return 200, {"status": "uploaded"}
    return 404, {"error": "not found"}


def save_instance(data, client):
    """Persist the white-label instance identity through the root helper (the file lives
    in /etc/vantage-console, which the serve process cannot write). Every value is validated
    here before it is handed over, and again in the helper."""
    # merge semantics: only keys PRESENT in the request change; everything else keeps its
    # stored value. The trimmed Customize form sends just colours/font/mode/build defaults,
    # and a partial save can never blank a field it did not carry (the old all-or-nothing
    # validation rejected any save missing a colour - accent_warm was never sent, so every
    # save failed).
    inst = dict(load_instance())
    for k in _INSTANCE_TEXT:
        if k not in data:
            continue
        v = str(data.get(k, "")).strip()
        if k == "product_name" and not v:
            return 400, {"error": "the product needs a name"}
        if v and not re.fullmatch(r"[A-Za-z0-9 ()&.,_'-]{0,40}", v):
            return 400, {"error": f"{k}: letters, numbers and simple punctuation only"}
        inst[k] = v
    for k in _INSTANCE_COLOR:
        if k not in data:
            continue
        v = str(data.get(k, "")).strip()
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            return 400, {"error": f"{k} must be a #rrggbb colour"}
        inst[k] = v
    if "country" in data:
        c = str(data.get("country", "")).strip().upper()
        inst["country"] = c if re.fullmatch(r"[A-Z]{2}", c) else ""
    if "agent_enabled" in data:
        inst["agent_enabled"] = bool(data.get("agent_enabled"))
    if "font" in data:
        inst["font"] = data["font"] if data.get("font") in FONT_STACKS else "standard"
    if "maps_key" in data:
        mk = str(data.get("maps_key", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.~-]{0,64}", mk):
            return 400, {"error": "maps key: letters, digits, - _ . ~ only"}
        inst["maps_key"] = mk
    if "console_mode" in data:
        new_mode = data.get("console_mode") if data.get("console_mode") in (
            "admin", "client") else "admin"
        # a mode change is guarded: turning the (potentially only) admin into a client
        # would leave the estate with no controller. When an operator password exists,
        # re-check it before applying any change; an open console has no password to ask.
        if new_mode != inst.get("console_mode", "admin") and auth_configured():
            if not verify_operator_password(data.get("mode_passphrase", "")):
                return 403, {"error": "changing console mode needs your operator password"}
        inst["console_mode"] = new_mode
    blob = base64.b64encode(json.dumps(inst).encode()).decode()
    if len(blob) > 4096:
        return 400, {"error": "instance config too large"}
    ok, out = setup_helper("instance", "instance", blob)
    audit({"action": "save-instance", "result": "OK" if ok else "ERROR", "client": client})
    return (200, {"saved": True}) if ok else (400, {"error": out[:200]})


def client_mode_block():
    """Client mode is a workflow guard: this console watches and works its own boxes but
    does not reshape the estate. The flick lives in Customize; the hard boundary remains
    key material - a console can only ever act where its keys are enrolled."""
    if load_instance().get("console_mode") == "client":
        return 403, {"error": "this console is in client mode - estate-shaping actions "
                              "are done on an admin console (or flick the mode in "
                              "Customize)"}
    return None


def start_setup_job(data, client, authed=False):
    blocked = client_mode_block()
    if blocked:
        return blocked
    """The whole setup as one streamed job: enrol -> push deb -> provision -> first credentials
    -> shred the bootstrap key. Each phase's output lands in the job log; the credentials land
    in the job record for the wizard to offer as downloads (they are also in the box's own
    credential store, so the vault can re-serve them later)."""
    name = str(data.get("name", ""))
    dest = str(data.get("dest", ""))
    ename = str(data.get("estate_name", ""))
    profile = str(data.get("profile", "cloud"))
    label = str(data.get("label", "") or ename)[:40]
    deb_file = str(data.get("deb_file", ""))
    deb_sha = str(data.get("deb_sha256", ""))
    push_deb = bool(deb_file)
    prov = data.get("provision") or {}
    creds = data.get("creds") or []
    passphrase = data.get("passphrase")
    # enrol_only: the box already runs TAK (or is someone else's build) - enrol it for
    # monitoring and gated management and stop. No deb, no provisioner, no credentials.
    enrol_only = str(data.get("enrol_only", "")) == "1"
    # a new box gets its own self-manage console unless the operator opts out; a plain
    # bind default keeps it local to the box (reachable by whoever logs into that box)
    with_console = str(data.get("with_console", "1")) == "1" and not enrol_only
    console_bind = str(data.get("console_bind", "") or "127.0.0.1:8090")
    if not re.fullmatch(r"[A-Za-z0-9.:_-]{1,64}", console_bind):
        console_bind = "127.0.0.1:8090"
    if not data.get("confirm"):
        return 400, {"error": "confirmation required"}
    if not re.fullmatch(RE_KEYNAME, name):
        return 400, {"error": "bad key name"}
    if not re.fullmatch(RE_DEST, dest):
        return 400, {"error": "bad destination"}
    if not re.fullmatch(r"[a-z0-9-]{1,24}", ename):
        return 400, {"error": "estate name must be [a-z0-9-], max 24"}
    if profile not in ("cloud", "firmbase", "deployed", "nuc"):   # nuc = old name for firmbase
        return 400, {"error": "bad profile"}
    if push_deb and not (re.fullmatch(RE_DEBNAME, deb_file) and re.fullmatch(r"[0-9a-f]{64}", deb_sha)):
        return 400, {"error": "bad deb reference"}
    a = ACTIONS["provision-server"]
    prov = {f["name"]: str(prov.get(f["name"], "")) for f in a["inputs"]}
    if push_deb:
        prov["deb"] = f"/root/{deb_file}"
    if not enrol_only:
        for f in a["inputs"]:
            if not re.match(f["pattern"], prov.get(f["name"], "")):
                return 400, {"error": f"invalid {f['name']}"}
    if len(creds) > 20:
        return 400, {"error": "at most 20 first-run credentials"}
    for c in creds:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", str(c.get("user", ""))) \
           or str(c.get("user", "")).startswith("-") \
           or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(c.get("group", ""))):
            return 400, {"error": "bad credential row"}
        if not re.fullmatch(r"[A-Za-z0-9+/=]{0,88}", str(c.get("password_b64", "") or "")):
            return 400, {"error": "bad credential password encoding"}
    # a signed-in session already proved the operator password - re-typing it seconds
    # after setting it is ceremony, not security. The passphrase prompt survives only
    # where sessions do not exist (a deliberately-open console), where it is the one
    # gate there is.
    if creds and ACTIONS["enrol-device"]["needs_passphrase"] and not authed \
       and not verify_passphrase(load_actions_config(), passphrase):
        return 400, {"error": "first-run credentials need the operator passphrase"}

    job_id = "j" + os.urandom(6).hex()
    os.makedirs(JOBS_DIR, exist_ok=True)
    log_path, _ = _job_paths(job_id)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _job_write(job_id, {"job": job_id, "action": "setup-server", "target": ename,
                        "status": "running", "started": started})
    audit({"action": "setup-server", "target": f"{ename} ({dest})", "result": "STARTED",
           "job": job_id, "client": client})

    def say(log, msg):
        log.write(msg.rstrip() + "\n")
        log.flush()

    def run():
        rc, cred_out = 1, []
        with open(log_path, "w") as log:
            try:
                say(log, f"== SETUP {ename} on {dest} ==")
                say(log, "-- phase 1/5: enrol to the estate --")
                # Spec 002: seed the box's declared loadout from what this build
                # will actually install - takserver plus the chosen components.
                # Honest to the build only: nothing the build did not install
                # (tailscale included) is declared here; the loadout action adds
                # extras the operator fits later. Enrol-only adopts a box built
                # by someone else, so it declares NOTHING - the profile's full
                # expectations stand until the operator says otherwise.
                declared = []
                if not enrol_only:
                    declared = ["takserver"]
                    for c in (prov.get("components") or "").split(","):
                        c = c.strip()
                        if c and re.fullmatch(r"[a-z]+", c) and c not in declared:
                            declared.append(c)
                ok, out = setup_helper("enrol", name, dest, ename, profile,
                                       base64.b64encode(label.encode()).decode(),
                                       prov.get("fqdn", ""), ",".join(declared),
                                       timeout=300)
                say(log, out)
                if not ok:
                    raise RuntimeError("enrolment failed")
                if enrol_only:
                    say(log, "-- enrol-only: this box is now monitored and managed; "
                             "no software was installed --")
                    cfg = load_actions_config()
                    if ename not in (cfg.get("targets") or {}):
                        raise RuntimeError("enrolment did not register the target")
                    rc = 0
                    return
                if push_deb:
                    say(log, "-- phase 2/5: push the TAK .deb --")
                    ok, out = setup_helper("push-deb", name, dest, deb_file, deb_sha, timeout=960)
                    say(log, out)
                    if not ok:
                        raise RuntimeError("deb push failed")
                    # the provisioner is told the path the push actually used - the
                    # browser's copy of it went stale on a reopened deployment and
                    # pointed at /root while the deb sat in /home/<user> (failed
                    # live, 30 Aug 2026); the server is the authority on this path
                    duser = dest.split("@", 1)[0]
                    prov["deb"] = ("/root/" if duser == "root" else f"/home/{duser}/") + deb_file
                else:
                    say(log, "-- phase 2/5: skipped (deb already on the box) --")
                say(log, "-- phase 3/5: provision TAK Server (this is the long part) --")
                cfg = load_actions_config()
                if ename not in (cfg.get("targets") or {}):
                    raise RuntimeError("enrolment did not register the target")
                art = resolve_artifact(a["artifact"])
                if not art:
                    raise RuntimeError(
                        f"the provisioner {a['artifact']} is missing from this console - "
                        "re-run the console installer on this box to restore it")
                with open(art) as fh:
                    payload = fh.read()
                digest = hashlib.sha256(payload.encode()).hexdigest()
                argsb64 = base64.b64encode(json.dumps(prov).encode()).decode()
                key = os.path.join(ACTION_KEYS, a["key"])
                cmd = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                       "-o", "StrictHostKeyChecking=accept-new", cfg["targets"][ename],
                       f"provision {digest} {argsb64}"]
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
                p.stdin.write(payload)
                p.stdin.close()
                for line in p.stdout:
                    log.write(line)
                    log.flush()
                prc = p.wait(timeout=4000)
                if prc != 0:
                    raise RuntimeError(f"provision rc={prc}")
                if prov.get("dry_run") == "1":
                    say(log, "-- dry run: stopping before credentials; run again unticked --")
                    rc = 0
                    return
                say(log, "-- phase 4/5: first credentials --")
                for c in creds:
                    code, res = run_action("enrol-device", ename,
                                           {"user": c["user"], "group": c["group"],
                                            "password_b64": c.get("password_b64", "")},
                                           passphrase, True, client,
                                           passphrase_ok=authed)
                    if code == 200:
                        say(log, f"credential ready: {c['user']} ({c['group']})")
                        cred_out.append({"user": c["user"], "group": c["group"],
                                         "password": res.get("password", ""),
                                         "url": res.get("url", ""), "itak": res.get("itak", ""),
                                         "png": res.get("png", "")})
                    else:
                        say(log, f"credential FAILED: {c['user']}: {res.get('error') or res.get('message')}")
                if with_console:
                    say(log, "-- phase 4b/5: install a self-manage console on this box --")
                    ccode, cres = run_action("deploy-console", ename,
                                             {"bind": console_bind}, passphrase, True,
                                             client, passphrase_ok=authed)
                    if ccode == 200:
                        say(log, cres.get("message", "console installed"))
                        say(log, "   this box can now run its own Vantage console; the "
                                 "operator on the box logs in to manage it locally.")
                    else:
                        say(log, "console install did not complete: "
                                 + str(cres.get("error") or cres.get("message")))
                        say(log, "   TAK is up regardless; install a console later from "
                                 "Operations > Install a console on this box.")
                rc = 0
            except Exception as e:
                say(log, f"ERR {e}")
                rc = 1
            finally:
                # 1.25.0: the key is destroyed only when a LIVE run completes. A dry run
                # holds it for the live run that follows; a failure holds it for the
                # retry. Shred-always cost the operator a fresh paste on every cycle.
                dry = prov.get("dry_run") == "1" and not enrol_only
                if rc == 0 and not dry:
                    say(log, "-- phase 5/5: shred the bootstrap key --")
                    ok, out = setup_helper("shred", name)
                    say(log, out if ok else f"shred FAILED - destroy key '{name}' by hand: {out}")
                elif rc == 0:
                    say(log, "-- phase 5/5: bootstrap key kept - the dry run holds it for the live run --")
                else:
                    say(log, "-- phase 5/5: bootstrap key kept for retry - a successful live run destroys it --")
                finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                _job_write(job_id, {"job": job_id, "action": "setup-server", "target": ename,
                                    "status": "done" if rc == 0 else "failed", "rc": rc,
                                    "dry": dry, "started": started, "finished": finished,
                                    "creds": cred_out})
                audit({"action": "setup-server", "target": f"{ename} ({dest})",
                       "result": "OK" if rc == 0 else "ERROR", "job": job_id, "client": client})

    threading.Thread(target=run, daemon=True).start()
    return 200, {"status": "started", "job": job_id}


# ---------- federation map positions (1.12.0) --------------------------------------------------
def load_fedpos():
    try:
        with open(FEDPOS) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_fedpos(name, x, y):
    """Persist one node's position on the federation map. Positions are estate state, not a
    browser preference: stored server-side so a refresh, another browser, or another operator
    sees the same arrangement."""
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", str(name or "")):
        return False
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return False
    if not (-20000 <= x <= 20000 and -20000 <= y <= 20000):
        return False
    pos = load_fedpos()
    pos[str(name)] = [round(x), round(y)]
    os.makedirs(os.path.dirname(FEDPOS), exist_ok=True)
    tmp = FEDPOS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(pos, fh)
    os.replace(tmp, FEDPOS)
    return True


_ADDR_CACHE = {}


def _resolves_to(host):
    """The IPs a managed box's address resolves to (plus the literal itself), cached. Used to
    match the checker's socket-level peer IPs back to boxes we know."""
    host = str(host or "").lower()
    if not host:
        return set()
    if host not in _ADDR_CACHE:
        ips = {host}
        try:
            for info in socket.getaddrinfo(host, None):
                ips.add(info[4][0])
        except OSError:
            pass
        _ADDR_CACHE[host] = ips
    return _ADDR_CACHE[host]


# ---------- native chat with Sam (the console as a paired OpenClaw client) ---------------------
def sam_ready():
    """Is the console paired with Sam? Both the device identity and the token must be present."""
    try:
        return os.path.exists(SAM_IDENTITY) and os.path.getsize(SAM_TOKEN_FILE) > 0
    except OSError:
        return False


def sam_chat(text, client):
    """Relay one message to Sam over the loopback gateway and return his reply. The console is a
    paired device; nothing here re-pairs. A clear message is returned if Sam is not yet paired."""
    text = str(text or "").strip()
    if not text:
        return 400, {"error": "empty message"}
    if len(text) > 8000:
        return 400, {"error": "message too long"}
    if not sam_ready():
        return 503, {"error": "not-paired",
                     "message": f"No agent is connected to this console yet. The "
                                f"{load_instance()['agent_name']} tab is where the "
                                "connection is set up."}
    try:
        import sam_client
    except Exception as e:
        return 500, {"error": f"Sam client unavailable: {e}"[:200]}
    try:
        token = open(SAM_TOKEN_FILE).read().strip()
        c = sam_client.SamClient(url=SAM_GATEWAY_URL, bootstrap_token=token, identity_path=SAM_IDENTITY)
        status, info = c.connect(timeout=15)
        if status != "ok":
            c.close()
            if status == "pairing":
                return 503, {"error": "not-approved",
                             "message": f"The console's device is waiting for approval (pairing "
                                        f"{info}). Approve it with bring-sam-online.sh."}
            return 502, {"error": "connect-failed", "message": f"Could not reach Sam: {str(info)[:180]}"}
        res = c.chat(text, timeout=75)
        c.close()
        if res.get("ok"):
            audit({"action": "sam-chat", "target": "sam", "result": "OK", "client": client})
            return 200, {"reply": res.get("text", "").strip() or "(Sam gave no reply)"}
        return 502, {"error": "chat-failed", "message": str(res.get("error"))[:200]}
    except Exception as e:
        return 502, {"error": "chat-failed", "message": f"{e}"[:200]}


# ---------- rendering --------------------------------------------------------------------------
CSS = """
@import url("https://fonts.googleapis.com/css2?family=Bai+Jamjuree:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&family=Roboto+Mono:wght@400;500&display=swap");
*{box-sizing:border-box}
:root{
 --forest:#113308;--forest-deep:#0A1F05;--forest-soft:#1F4214;
 --gold:#B5B171;--gold-warm:#C9C589;--gold-deep:#8E8A55;--gold-light:#D2C78D;
 --blue:#586F7C;--clay:#A35C17;
 --paper:#F7F6EB;--paper2:#E5E2C9;--rule:#DDDAC4;--rule2:#C0BCA2;
 --ink:#1A1A1A;--ink2:#4A4A47;--ink3:#6A6A63;
 --bg:var(--paper);--card:#fff;--fg:var(--ink);--fg2:var(--ink2);--mute:var(--ink3);--line:var(--rule);
 --hdr-fg:var(--paper);--hdr-mute:rgba(247,246,235,.62);
 --ok:#2f6b2a;--ok-b:#2f6b2a17;--warn:#A35C17;--warn-b:#A35C1717;
 --fail:#98311f;--fail-b:#98311f17;--off:#586F7C;--off-b:#586F7C17;
 --acc:#6E6A3E;--bh:var(--paper2);--hover:var(--paper2);--code-bg:var(--paper2);--code-fg:var(--forest);--chip-fg:#fff;--focus:#113308;
 --r-card:6px;--r-sm:4px;--r-pill:999px;
 --shadow:0 1px 2px rgba(17,51,8,.05),0 1px 1px rgba(17,51,8,.03);
 --font-display:"Manrope","Helvetica Neue",Helvetica,Arial,sans-serif;
 --font-sans:"Bai Jamjuree","Helvetica Neue",Helvetica,Arial,sans-serif;
 --font-mono:"Roboto Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
 --bg:#0d1108;--card:#141a0e;--fg:#ece9d6;--fg2:#c6c5b1;--mute:#8f9682;--line:#2a301f;
 --gold:#c9c589;--acc:#c9c589;--bh:#10160b;--hover:#1a2113;--code-bg:#1a2113;--code-fg:#c9c589;--chip-fg:#0A1F05;--focus:#c9c589;
 --ok:#7fa96f;--ok-b:#7fa96f1f;--warn:#cf913f;--warn-b:#cf913f1f;
 --fail:#d97a63;--fail-b:#d97a631f;--off:#7d8c85;--off-b:#7d8c851f;--shadow:none}}
:root[data-theme=dark]{
 --bg:#0d1108;--card:#141a0e;--fg:#ece9d6;--fg2:#c6c5b1;--mute:#8f9682;--line:#2a301f;
 --gold:#c9c589;--acc:#c9c589;--bh:#10160b;--hover:#1a2113;--code-bg:#1a2113;--code-fg:#c9c589;--chip-fg:#0A1F05;--focus:#c9c589;
 --ok:#7fa96f;--ok-b:#7fa96f1f;--warn:#cf913f;--warn-b:#cf913f1f;
 --fail:#d97a63;--fail-b:#d97a631f;--off:#7d8c85;--off-b:#7d8c851f;--shadow:none}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font-sans);
 font-size:15px;line-height:1.55;font-weight:400;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.wrap{max-width:1180px;margin:0 auto;padding:0 22px 56px}
.mono{font-family:var(--font-mono)}
/* forest header, the signature MilUX surface, with the website's gold cross-hatch:
   faint horizontal + vertical gold lines on a square grid (--hatch-cell), NOT diagonal */
header{position:sticky;top:0;z-index:5;background:var(--forest);color:var(--hdr-fg);
 border-bottom:3px solid var(--gold);overflow:hidden;--hatch-cell:32px}
header:before{content:"";position:absolute;inset:0;pointer-events:none;
 background-image:linear-gradient(rgba(181,177,113,.10) 1px,transparent 1px),
  linear-gradient(90deg,rgba(181,177,113,.10) 1px,transparent 1px);
 background-size:var(--hatch-cell) var(--hatch-cell)}
.head{position:relative;max-width:1180px;margin:0 auto;padding:15px 22px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.mark{height:36px;width:auto;display:block;flex:0 0 auto;margin:-4px 0}
.eyebrow-h{font-family:var(--font-display);font-weight:600;font-size:11px;letter-spacing:.16em;
 text-transform:uppercase;color:var(--gold);opacity:.92}
.spacer{flex:1}
.checked{font-family:var(--font-mono);color:var(--hdr-mute);font-size:11.5px;text-align:right;line-height:1.5}
.checked b{color:var(--hdr-fg);font-weight:500}
.verdict{display:inline-flex;align-items:center;gap:8px;font-family:var(--font-display);font-weight:700;
 font-size:12px;letter-spacing:.1em;padding:6px 13px;border-radius:var(--r-pill);color:var(--chip-fg)}
.verdict .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.v-OK{background:var(--ok)}.v-WARN{background:var(--warn)}.v-FAIL,.v-UNREACHABLE{background:var(--fail)}
.v-OFFLINE,.v-UNKNOWN{background:var(--off)}
/* banners */
.banner{margin:18px 0 0;padding:12px 15px;border-radius:var(--r-card);font-size:13.5px;display:flex;gap:10px;align-items:flex-start}
.banner b{font-family:var(--font-display);font-weight:700;letter-spacing:.03em;text-transform:uppercase;font-size:12px}
.banner.stale{background:var(--fail-b);border:1px solid var(--fail);color:var(--fail)}
.banner.drift{background:var(--warn-b);border:1px solid var(--warn);color:var(--warn)}
/* section eyebrow */
.sec-eye{font-family:var(--font-display);font-size:11px;font-weight:600;letter-spacing:.16em;
 text-transform:uppercase;color:var(--acc);margin:24px 0 10px}
/* board */
/* tiles are separate rounded cards now, each expandable to its on-box software vs baseline */
.board{display:grid;gap:12px;align-items:start;
 grid-template-columns:repeat(auto-fit,minmax(250px,1fr))}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;
 border-top:3px solid var(--off);color:inherit;display:block}
.tile.OK{border-top-color:var(--ok)}.tile.WARN{border-top-color:var(--warn)}
.tile.FAIL,.tile.UNREACHABLE{border-top-color:var(--fail)}.tile.OFFLINE{border-top-color:var(--off)}
.tile.BUILD{border-top-color:var(--gold)}
.peer-tile{padding:15px 16px}
.tile>summary{list-style:none;cursor:pointer;padding:15px 16px;display:block}
.tile>summary::-webkit-details-marker{display:none}
details.tile>summary:before,details.tile[open]>summary:before{content:none}
.tile>summary:hover{background:var(--hover)}
.tile>summary:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.tile .t1{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.tile .nm{font-family:var(--font-display);font-weight:600;font-size:14px;letter-spacing:.01em}
.tile .sub{font-family:var(--font-mono);color:var(--mute);font-size:11px;margin-top:2px}
.tile-toggle{display:flex;align-items:center;gap:7px;margin-top:11px;color:var(--mute);
 font:600 10px var(--font-display);letter-spacing:.08em;text-transform:uppercase}
.tile-toggle .chev{width:6px;height:6px;border-right:2px solid var(--mute);
 border-bottom:2px solid var(--mute);transform:rotate(-45deg);transition:transform .15s;flex:0 0 auto}
details.tile[open]>summary .tile-toggle .chev{transform:rotate(45deg)}
.tile-body{padding:0 16px 16px;border-top:1px solid var(--line)}
.tinv{width:100%;border-collapse:collapse;font-size:11.5px;margin-top:12px}
.tinv th{text-align:left;font:600 10px var(--font-display);letter-spacing:.06em;
 text-transform:uppercase;color:var(--mute);padding:5px 8px;border-bottom:1px solid var(--line)}
.tinv td{padding:5px 8px;border-bottom:1px solid var(--line);font-family:var(--font-mono)}
.tinv tr:last-child td{border-bottom:none}
.tinv th:first-child,.tinv td:first-child{font-family:var(--font-display);font-weight:600}
.tinv .b-ok{color:var(--ok)}.tinv .b-drift{color:var(--warn)}.tinv .b-abs{color:var(--mute)}
.tile-open{display:inline-block;margin-top:12px;font:600 12px var(--font-display);
 color:var(--acc);text-decoration:none}
.tile-open:hover{text-decoration:underline}
/* issues surfaced on the tile face, so a problem shows without expanding */
.tissues{list-style:none;margin:11px 0 0;padding:0;display:flex;flex-direction:column;gap:5px}
.tissues .iss{display:flex;gap:7px;align-items:baseline;font-size:11.5px;line-height:1.35}
.tissues .iss-st{font:700 9px var(--font-display);letter-spacing:.04em;padding:1px 5px;
 border-radius:var(--r-sm);flex:0 0 auto;color:#fff}
.tissues .FAIL .iss-st{background:var(--fail)}.tissues .WARN .iss-st{background:var(--warn)}
.tissues .iss-nm{font-family:var(--font-display);font-weight:600;flex:0 0 auto}
.tissues .iss-dt{color:var(--mute);font-family:var(--font-mono);font-size:10.5px;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tissues .iss-more{color:var(--mute);font-size:10.5px;font-family:var(--font-mono)}
.chip{font-family:var(--font-display);font-size:10.5px;font-weight:700;letter-spacing:.06em;
 padding:3px 8px;border-radius:var(--r-sm);color:var(--chip-fg);text-transform:uppercase}
.c-OK{background:var(--ok)}.c-WARN{background:var(--warn)}.c-FAIL,.c-UNREACHABLE{background:var(--fail)}
.c-BUILD{background:var(--gold);color:var(--forest-deep)}
.c-OFFLINE,.c-UNKNOWN{background:var(--off)}
.counts{display:flex;gap:13px;margin-top:11px;font-size:11.5px;font-family:var(--font-mono)}
.counts span{color:var(--mute)}.counts b{color:var(--fg)}
.counts .k-ok b{color:var(--ok)}.counts .k-warn b{color:var(--warn)}.counts .k-fail b{color:var(--fail)}
.strip{display:flex;gap:2px;height:20px;margin-top:12px}
.strip i{flex:1;border-radius:1px;background:var(--off-b);min-width:2px}
.strip i.OK{background:var(--ok)}.strip i.WARN{background:var(--warn)}
.strip i.FAIL,.strip i.UNREACHABLE{background:var(--fail)}.strip i.OFFLINE{background:var(--off)}
.strip.mini{height:7px;margin-top:9px}
.striplab{display:flex;justify-content:space-between;color:var(--mute);font-size:10px;
 font-family:var(--font-mono);letter-spacing:.05em;margin-top:5px;text-transform:uppercase}
/* box sections */
section.box{background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);
 box-shadow:var(--shadow);margin-top:16px;overflow:hidden}
.box>.bh{padding:14px 18px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
 border-bottom:1px solid var(--line);background:var(--bh)}
.box .title{font-family:var(--font-display);font-weight:600;font-size:15px;letter-spacing:.01em;text-transform:uppercase;margin:0}
.box .flip{font-family:var(--font-mono);font-size:10.5px;font-weight:500;color:var(--warn);
 border:1px solid var(--warn);padding:1px 6px;border-radius:var(--r-sm)}
.box .meta{font-family:var(--font-mono);color:var(--mute);font-size:11px}
summary.bh{cursor:pointer;list-style:none}summary.bh::-webkit-details-marker{display:none}
summary.bh:hover{background:var(--hover)}
summary.bh:after{content:"";width:7px;height:7px;border-right:2px solid var(--mute);border-bottom:2px solid var(--mute);
 transform:rotate(-45deg);transition:transform .15s;flex:0 0 auto;margin-left:4px}
details[open]>summary.bh:after{transform:rotate(45deg)}
summary.bh:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.tile:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
a.skip{position:absolute;left:-9999px;top:0;z-index:10;background:var(--forest);color:var(--paper);
 padding:8px 14px;border-radius:0 0 var(--r-sm) 0;font-family:var(--font-display);font-weight:600}
a.skip:focus{left:0}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.bbody{padding:16px 18px}
.err{background:var(--fail-b);border:1px solid var(--fail);color:var(--fail);border-radius:var(--r-sm);
 padding:9px 12px;font:12px/1.5 var(--font-mono);word-break:break-word;margin-bottom:12px}
.h3{font-family:var(--font-display);font-size:11px;font-weight:600;letter-spacing:.14em;
 text-transform:uppercase;color:var(--acc);margin:2px 0 10px}
.rows{display:flex;flex-direction:column;gap:6px;margin-bottom:6px}
.row{display:grid;grid-template-columns:58px 1fr;gap:10px;align-items:baseline;padding:8px 11px;
 border-radius:var(--r-sm);background:var(--off-b);border-left:3px solid var(--off)}
.row.FAIL{background:var(--fail-b);border-left-color:var(--fail)}
.row.WARN{background:var(--warn-b);border-left-color:var(--warn)}
.row .st{font-family:var(--font-display);font-size:10px;font-weight:700;letter-spacing:.05em}
.row.FAIL .st{color:var(--fail)}.row.WARN .st{color:var(--warn)}
.row .nm{font-weight:600;font-size:13px}.row .dt{font-family:var(--font-mono);color:var(--mute);font-size:12px;grid-column:2}
.metrics{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(205px,1fr));margin:6px 0 4px}
.metric{border:1px solid var(--line);border-radius:var(--r-sm);padding:10px 12px}
.metric .ml{color:var(--mute);font-size:11px;font-family:var(--font-mono)}
.metric .mv{font-family:var(--font-display);font-size:22px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}
.metric .mv u{font-family:var(--font-mono);font-size:10.5px;font-weight:400;color:var(--mute);text-decoration:none;margin-left:4px}
.metric .mv .ar{font-size:12px;margin-left:5px}.ar.up{color:var(--warn)}.ar.dn{color:var(--ok)}
.metric svg{display:block;margin-top:7px;width:100%;height:26px}
details{margin-top:12px}
details>summary{cursor:pointer;color:var(--mute);font-size:12px;list-style:none;user-select:none;font-family:var(--font-mono)}
details>summary::-webkit-details-marker{display:none}
details>summary:before{content:"+ ";color:var(--acc);font-weight:700}
details[open]>summary:before{content:"− "}
.cat{font-family:var(--font-display);margin:13px 0 3px;font-size:10.5px;font-weight:600;color:var(--acc);letter-spacing:.1em;text-transform:uppercase}
.checkrow{display:grid;grid-template-columns:18px 1fr auto;gap:9px;align-items:baseline;padding:4px 0;font-size:12.5px;border-top:1px solid var(--line)}
.checkrow .d{width:8px;height:8px;border-radius:50%;background:var(--off);margin-top:5px}
.checkrow .d.OK{background:var(--ok)}.checkrow .d.WARN{background:var(--warn)}
.checkrow .d.FAIL{background:var(--fail)}.checkrow .d.SKIP{background:var(--off)}
.checkrow .cn{font-weight:550}.checkrow .cd{font-family:var(--font-mono);color:var(--mute);text-align:right;max-width:54%;font-size:11.5px}
/* software inventory (1.1.0) */
.swlist{display:flex;flex-direction:column}
.swrow{display:grid;grid-template-columns:minmax(120px,1.2fr) minmax(90px,1fr) minmax(70px,1fr) auto;
 gap:9px;align-items:baseline;padding:5px 0;font-size:12.5px;border-top:1px solid var(--line)}
.swrow .sw-n{font-weight:600}
.swrow .sw-v,.swrow .sw-s{font-family:var(--font-mono);font-size:11.5px;color:var(--mute);overflow-wrap:anywhere}
.swrow .sw-a{display:flex;gap:8px;align-items:center;justify-content:flex-end}
.sw-drift{font-family:var(--font-mono);font-size:11.5px;color:var(--warn);font-weight:700;white-space:nowrap}
.sw-cur{font-family:var(--font-mono);font-size:10.5px;color:var(--ok)}
a.sw-up{font:700 10.5px var(--font-display);letter-spacing:.05em;text-transform:uppercase;
 color:var(--paper);background:var(--forest);text-decoration:none;padding:4px 10px;border-radius:var(--r-sm);white-space:nowrap}
a.sw-up:hover{background:var(--forest-soft)}
a.sw-up:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
@media(max-width:560px){.swrow{grid-template-columns:1fr auto;grid-auto-flow:dense}
 .swrow .sw-s{grid-column:1}.swrow .sw-a{grid-column:2}}
/* portal nav (1.2.0) - lives inside the sticky header, below the brand row */
.topnav{position:relative;display:flex;gap:2px;max-width:1180px;margin:0 auto;padding:0 22px;
 overflow-x:auto;scrollbar-width:none}
.topnav::-webkit-scrollbar{display:none}
.topnav a{font:600 11.5px var(--font-display);letter-spacing:.08em;text-transform:uppercase;
 color:var(--hdr-mute);padding:9px 12px 10px;text-decoration:none;border-bottom:2px solid transparent;white-space:nowrap}
.topnav a:hover{color:var(--hdr-fg)}
.topnav a.on{color:var(--gold-warm);border-bottom-color:var(--gold)}
.topnav a:focus-visible{outline:2px solid var(--gold-warm);outline-offset:-2px}
/* data tables: baseline + audit (1.2.0) */
.baseline-actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:12px}
.bl-inp{font-family:var(--font-mono);font-size:11.5px;width:150px;box-sizing:border-box;
 padding:3px 7px;border:1px solid var(--rule2);border-radius:var(--r-sm);background:var(--bg);color:var(--fg)}
/* the baseline table stays shallow: the servers above are the page's real subject */
.baseline-form .dtable th{padding:6px 10px}
.baseline-form .dtable td{padding:4px 10px}
.dtable.baseline-only{max-width:460px}
.bl-inp:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
.bl-msg{font-size:12.5px;color:var(--mute)}.bl-msg.ok{color:var(--ok)}.bl-msg.err{color:var(--fail)}
/* bulk enrolment */
.bulk-form{margin-top:4px}
.bulk-fields{display:flex;gap:12px;flex-wrap:wrap}
.bulk-fields .fl{flex:1 1 130px;min-width:120px}
.bulk-form input,.bulk-list{font-size:13px;padding:6px 9px;border:1px solid var(--rule2);
 border-radius:var(--r-sm);background:var(--bg);color:var(--fg);box-sizing:border-box}
.bulk-form input{width:100%}
.bulk-names{margin:10px 0}.bulk-names summary{cursor:pointer;font-size:12.5px;color:var(--mute)}
.bulk-list{width:100%;font-family:var(--font-mono);margin-top:8px;resize:vertical}
.bulk-actions{display:flex;gap:12px;align-items:center;margin-top:12px;flex-wrap:wrap}
.bulk-msg{font-size:12.5px;color:var(--mute)}.bulk-msg.ok{color:var(--ok)}.bulk-msg.err{color:var(--fail)}
.bulk-out{margin-top:16px}
.bulk-dl{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.bulk-dl a.a-go,.bulk-dl a.cred-refresh{text-decoration:none;display:inline-block}
.bulk-errs{font-size:12.5px;color:var(--fail);margin-bottom:12px}
.bulk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(116px,1fr));gap:14px}
.bulk-cell{margin:0;text-align:center;background:#fff;border:1px solid var(--line);
 border-radius:var(--r-sm);padding:8px}
.bulk-cell img{width:100%;height:auto;image-rendering:pixelated;display:block}
.bulk-cell figcaption{font-family:var(--font-mono);font-size:11px;color:#222;margin-top:5px;word-break:break-all}
/* offline module deploy */
.mod-offline{margin-top:8px;font-size:12px}
.mod-offline summary{cursor:pointer;color:var(--mute);font-size:11px;letter-spacing:.04em;text-transform:uppercase}
.mo-body{margin-top:8px;display:flex;flex-direction:column;gap:8px}
.mo-status{font-family:var(--font-mono);font-size:11.5px;color:var(--fg)}
.mo-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.mo-deploy{background:var(--forest);color:var(--gold-light);border:0;border-radius:var(--r-sm);
 font-family:var(--font-display);font-weight:700;font-size:11.5px;padding:6px 12px;cursor:pointer}
.mo-deploy:hover{background:var(--forest-soft)}.mo-deploy[disabled]{opacity:.55;cursor:default}
.mo-upload,.mo-ver{font-size:11.5px;color:var(--mute);display:inline-flex;gap:6px;align-items:center;cursor:pointer}
.mo-verinp{font-family:var(--font-mono);font-size:11.5px;width:120px;padding:4px 7px;border:1px solid var(--rule2);border-radius:var(--r-sm);background:var(--bg);color:var(--fg);cursor:text}
.mo-prog{font-size:11.5px;color:var(--mute)}.mo-prog.ok{color:var(--ok)}.mo-prog.err{color:var(--fail)}
.mo-log{font-family:var(--font-mono);font-size:11px;white-space:pre-wrap;max-height:220px;overflow:auto;color:var(--fg)}
.mo-log:empty{display:none}
/* import software from USB */
.usb-import{margin:12px 0}
.usb-import summary{cursor:pointer;color:var(--mute);font-size:12px;letter-spacing:.04em}
.ui-body{margin-top:10px;display:flex;flex-direction:column;gap:10px}
.ui-actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.ui-scan{background:var(--forest);color:var(--gold-light);border:0;border-radius:var(--r-sm);
 font-family:var(--font-display);font-weight:700;font-size:11.5px;padding:6px 12px;cursor:pointer}
.ui-scan:hover{background:var(--forest-soft)}.ui-scan[disabled]{opacity:.55;cursor:default}
.ui-msg{font-size:12px;color:var(--mute)}.ui-msg.ok{color:var(--ok)}.ui-msg.err{color:var(--fail)}
.ui-list{display:flex;flex-direction:column;gap:6px}
.ui-row{display:flex;gap:12px;align-items:center;padding:6px 10px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--card)}
.ui-row.done{opacity:.6}
.ui-name{font-family:var(--font-mono);font-size:12px;flex:1;word-break:break-all}
.ui-sz{font-family:var(--font-mono);font-size:11px;color:var(--mute)}
.ui-imp{background:var(--bh);color:var(--fg);border:1px solid var(--rule2);border-radius:var(--r-sm);font-size:11px;padding:4px 10px;cursor:pointer}
.ui-imp[disabled]{opacity:.6;cursor:default}
/* device apps - current version marker */
.cur-apps{margin:12px 0}
.cur-apps summary{cursor:pointer;color:var(--mute);font-size:12px;letter-spacing:.04em}
.ca-list{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.ca-row{display:flex;gap:12px;align-items:center;padding:6px 10px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--card)}
.ca-name{font-family:var(--font-mono);font-size:12px;flex:1;word-break:break-all}
.ca-sz{font-family:var(--font-mono);font-size:11px;color:var(--mute)}
.ca-toggle{background:var(--bh);color:var(--fg);border:1px solid var(--rule2);border-radius:var(--r-sm);font-size:11px;padding:4px 10px;cursor:pointer}
.ca-toggle.on{background:var(--forest);color:var(--gold-light);border-color:var(--forest)}
.ca-toggle[disabled]{opacity:.6;cursor:default}
/* deploy page: top-of-page flow launchers */
.dflow-tabs{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 18px}
.dtab{background:var(--card);color:var(--fg);border:1px solid var(--rule2);border-radius:var(--r-sm);
 font-family:var(--font-display);font-weight:700;font-size:12.5px;padding:9px 16px;cursor:pointer}
.dtab:hover{border-color:var(--gold)}
.dtab.active{background:var(--forest);color:var(--gold-light);border-color:var(--forest)}
.dflow[hidden]{display:none}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r-card);background:var(--card);margin-top:2px}
.dtable{width:100%;border-collapse:collapse;font-size:12.5px;min-width:560px}
.dtable th{font:600 10.5px var(--font-display);letter-spacing:.08em;text-transform:uppercase;
 color:var(--mute);text-align:left;padding:9px 12px;border-bottom:2px solid var(--line)}
.dtable td{padding:8px 12px;border-bottom:1px solid var(--line);font-family:var(--font-mono);
 font-size:11.5px;vertical-align:top;overflow-wrap:anywhere}
.dtable tr:last-child td{border-bottom:none}
.dtable td.b-ok{color:var(--ok)}.dtable td.b-abs{color:var(--mute)}
.dtable td.b-drift a{color:var(--warn);font-weight:700;text-decoration:underline}
.dtable td.r-OK{color:var(--ok);font-weight:700}
.dtable td.r-FAIL,.dtable td.r-ERROR,.dtable td.r-DENIED{color:var(--fail);font-weight:700}
a.driftlink{color:inherit;font-weight:700;text-decoration:underline}
a.fixlink{display:inline-block;margin-left:10px;font:700 10.5px var(--font-display);letter-spacing:.05em;
 text-transform:uppercase;color:var(--paper);background:var(--warn);text-decoration:none;padding:3px 9px;border-radius:var(--r-sm);white-space:nowrap}
a.fixlink:hover{filter:brightness(1.08)}
a.fixlink:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.navjump{margin-left:auto;display:flex;align-items:center}
.navjump select{font:600 11.5px var(--font-display);letter-spacing:.05em;text-transform:uppercase;
 color:var(--gold-warm);background:transparent;border:1px solid rgba(181,177,113,.4);
 border-radius:var(--r-sm);padding:4px 8px;margin:4px 0}
.navjump select:focus-visible{outline:2px solid var(--gold-warm);outline-offset:1px}
p.doct{max-width:70ch;color:var(--fg2);font-size:13.5px}
/* service links (1.5.0) */
.links{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr))}
a.svc{display:block;text-decoration:none;border:1px solid var(--line);border-left:3px solid var(--gold);
 border-radius:var(--r-sm);padding:11px 13px;background:var(--card);color:inherit}
a.svc:hover{background:var(--hover);border-left-color:var(--forest)}
a.svc:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.svc-l{display:block;font:600 13.5px var(--font-display);letter-spacing:.01em}
.svc-u{display:block;font:500 11.5px var(--font-mono);color:var(--blue);margin-top:2px;overflow-wrap:anywhere}
.svc-n{display:block;font-size:11px;color:var(--mute);margin-top:3px}
.ag-t{font-family:var(--font-display);font-size:11px;font-weight:600;letter-spacing:.14em;
 text-transform:uppercase;color:var(--acc);margin:16px 18px 0;padding-top:4px}
.actions .agrid{padding-top:12px}
.dangerzone{margin:22px 0 0;border:1px solid var(--fail);border-radius:var(--r-card);
 padding:0 16px}
.dangerzone summary{cursor:pointer;color:var(--fail);font-weight:600;padding:12px 2px;list-style:none}
.dangerzone summary::-webkit-details-marker{display:none}
.dangerzone[open]{padding-bottom:16px}
.dangerzone .cred-pass{display:flex;flex-direction:column;gap:4px;max-width:280px;margin:8px 0}
.dangerzone .cred-pass input{font-size:13px;padding:6px 9px;border:1px solid var(--rule2);
 border-radius:var(--r-sm);background:var(--bg);color:var(--fg)}
.destroy-word{font-family:var(--font-mono);letter-spacing:.1em}
.destroy-log{max-height:240px;overflow:auto;margin-top:10px}.destroy-log:empty{display:none}
.ag-group{border:1px solid var(--line);border-radius:var(--r-card);margin:10px 0;
 background:var(--card)}
.ag-sum{display:flex;align-items:center;gap:12px;cursor:pointer;list-style:none;
 padding:14px 16px;user-select:none}
.ag-sum::-webkit-details-marker{display:none}
.ag-sum:hover{background:var(--bh)}
.ag-chev{color:var(--gold);font-size:11px;transition:transform .15s;
 display:inline-block;width:10px;flex:0 0 auto}
details[open] .ag-sum .ag-chev{transform:rotate(90deg)}
.ag-sum .ag-t{margin:0}
.ag-count{font:600 11px/1 var(--font-mono);color:var(--mute);
 background:var(--bh);border-radius:var(--r-pill);padding:3px 9px}
.ag-hint{margin-left:auto;font-size:12px;color:var(--gold)}
.proposals-h{background:var(--bh);border:1px solid var(--gold);border-radius:var(--r-card);
 padding:12px 16px;margin:16px 0 4px}
a#gearbtn{display:inline-flex;align-items:center;justify-content:center;text-decoration:none}
details[open] .ag-sum .ag-hint{display:none}
.a-res img.qr{display:block;background:#fff;padding:10px;border-radius:var(--r-sm);
 margin:10px 0 6px;width:210px;height:210px;image-rendering:pixelated}
/* actions */
section.actions{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--gold);
 border-radius:var(--r-card);box-shadow:var(--shadow);margin-top:26px;overflow:hidden}
.ah{padding:15px 18px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;background:var(--bh)}
.ah .title{font-family:var(--font-display);font-weight:600;font-size:15px;text-transform:uppercase;letter-spacing:.02em}
.ah .meta{color:var(--mute);font-size:12.5px}
.agrid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(min(100%,290px),1fr));padding:16px 18px}
form.action{border:1px solid var(--line);border-radius:var(--r-sm);padding:14px;display:flex;flex-direction:column;gap:9px}
.a-h{display:flex;justify-content:space-between;align-items:center;gap:8px}
.a-t{font-family:var(--font-display);font-weight:600;font-size:13.5px;text-transform:uppercase;letter-spacing:.01em;margin:0}
.a-tags{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.a-tag{font-family:var(--font-display);font-size:9.5px;font-weight:700;letter-spacing:.06em;
 border:1px solid currentColor;padding:1px 6px;border-radius:var(--r-sm);text-transform:uppercase;white-space:nowrap}
.a-tag.read{color:var(--ok)}.a-tag.write{color:var(--warn)}.a-tag.destructive{color:var(--fail)}
.a-tag.pass{color:var(--blue)}
.a-d{color:var(--mute);font-size:12.5px;margin-top:-3px}
.a-more{margin-top:4px;padding:8px 10px;background:var(--bh);border-radius:var(--r-sm);line-height:1.55}
.a-more a{color:var(--fg);font-weight:600}
.fl{display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:var(--font-display);
 letter-spacing:.03em;text-transform:uppercase;color:var(--mute)}
.fl input,.fl select{font:400 13.5px/1.4 var(--font-sans);text-transform:none;letter-spacing:0;
 color:var(--fg);background:var(--bg);border:1px solid var(--rule2);border-radius:var(--r-sm);padding:8px 10px}
.fl input:focus,.fl select:focus{outline:2px solid var(--focus);outline-offset:1px}
.fl input::placeholder{color:var(--mute);opacity:1}
.hint{font-family:var(--font-mono);font-size:10.5px;color:var(--mute);text-transform:none;letter-spacing:0;margin-top:1px}
.a-go{align-self:flex-start;font-family:var(--font-display);font-weight:700;font-size:12px;letter-spacing:.04em;
 text-transform:uppercase;color:var(--paper);background:var(--forest);border:0;border-radius:var(--r-sm);padding:9px 18px;cursor:pointer}
.a-go:hover{background:var(--forest-soft)}.a-go.confirm{background:var(--fail);color:var(--chip-fg)}
.a-go:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.a-act{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.a-cancel{font:700 12px var(--font-display);letter-spacing:.04em;text-transform:uppercase;color:var(--fg2);
 background:transparent;border:1px solid var(--rule2);border-radius:var(--r-sm);padding:9px 14px;cursor:pointer}
.a-cancel:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.a-go[disabled]{opacity:.55;cursor:default}
.a-confirm{background:var(--warn-b);border:1px solid var(--warn);color:var(--warn);border-radius:var(--r-sm);padding:9px 11px;font-size:12.5px}
.a-res{font-size:12.5px;border-radius:var(--r-sm);padding:9px 11px;white-space:pre-wrap;word-break:break-word;font-family:var(--font-mono)}
.a-res.ok{background:var(--ok-b);border:1px solid var(--ok);color:var(--ok)}
.a-res.error{background:var(--fail-b);border:1px solid var(--fail);color:var(--fail)}
.a-res a.dl{font-weight:700;text-decoration:underline;color:inherit}
.a-res:empty{display:none}
.vband{padding:11px 22px;font-family:var(--font-display);font-weight:600;font-size:14px;
 display:flex;align-items:center;gap:10px;color:var(--chip-fg)}
.vband .vdot{width:9px;height:9px;border-radius:50%;background:currentColor;flex:0 0 auto}
.vband.OK{background:var(--ok)}.vband.WARN{background:var(--warn)}
.vband.FAIL,.vband.UNREACHABLE{background:var(--fail)}.vband.OFFLINE,.vband.UNKNOWN{background:var(--off)}
footer{color:var(--mute);font-size:12px;margin-top:28px;padding-top:15px;border-top:1px solid var(--line);
 font-family:var(--font-mono);display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
.foot-logo{background:#113308;border-radius:6px;padding:6px 10px;display:inline-flex;
 align-items:center;flex:0 0 auto}
.foot-logo .mark{height:20px;width:auto;margin:0;display:block}
/* tile decorations: service icons sit on a contrasting chip (not the tile texture) */
.svcrow{display:flex;gap:5px;margin-top:8px;flex-wrap:wrap}
.meshline{font:600 10.5px var(--font-display);letter-spacing:.05em;text-transform:uppercase;margin-top:6px}
.meshline.m-ok{color:var(--ok)}
.meshline.m-quiet{color:var(--mute)}
.meshchan{display:flex;gap:18px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);padding:14px;margin:10px 0}
.mc-meta{display:flex;flex-direction:column;gap:6px;min-width:180px}
.mc-qr{width:180px;height:180px;image-rendering:pixelated;background:#fff;padding:8px;border-radius:var(--r-sm)}
.mc-new{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin-top:12px}
.mc-h{font:600 13px var(--font-display);letter-spacing:.06em;text-transform:uppercase;margin:16px 0 6px}
.mc-list{margin:6px 0 10px 20px;line-height:1.55;font-size:13.5px}
.mc-rotate{background:var(--card);color:var(--fg);border:1px solid var(--rule2);border-radius:var(--r-sm);padding:5px 10px;cursor:pointer;font:600 11px var(--font-display)}
.mc-rotate:hover{border-color:var(--gold)}
/* the mesh map (Spec 003): a row of the mesh table opens onto its own network view */
.mesh-mapcell{width:1%;white-space:nowrap}
.mesh-toggle{display:inline-flex;align-items:center;gap:7px;background:var(--card);color:var(--fg);
 border:1px solid var(--rule2);border-radius:var(--r-sm);padding:4px 10px;cursor:pointer;
 font:600 11px var(--font-display);letter-spacing:.04em}
.mesh-toggle:hover{border-color:var(--gold)}
.mesh-toggle:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.mesh-toggle .chev{width:6px;height:6px;border-right:2px solid var(--mute);
 border-bottom:2px solid var(--mute);transform:rotate(-45deg);transition:transform .15s;flex:0 0 auto}
.mesh-toggle[aria-expanded=true] .chev{transform:rotate(45deg)}
.mesh-maprow>td{padding:0!important;background:var(--bh)}
.meshmap-panel{padding:14px 12px}
.meshmap-head{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:baseline;margin-bottom:10px}
.meshmap-head h3{font:600 13px var(--font-display);letter-spacing:.06em;text-transform:uppercase;margin:0}
.meshmap-note{color:var(--mute);font-size:11.5px;font-family:var(--font-mono)}
.meshmap-canvas{position:relative;height:380px;border:1px solid var(--line);
 border-radius:var(--r-card);overflow:hidden;background:var(--card)}
.meshmap-canvas.plan{background:var(--code-bg)}
.meshmap-empty{display:flex;align-items:center;justify-content:center;height:100%;padding:24px;
 text-align:center;color:var(--mute);font-size:12.5px;line-height:1.6}
.meshmap-legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:9px;
 font-family:var(--font-mono);font-size:11px;color:var(--mute)}
.meshmap-legend span{display:inline-flex;align-items:center;gap:6px}
.meshmap-legend i{width:18px;height:0;border-top:2px solid var(--mute);flex:0 0 auto}
.meshmap-legend i.direct{border-top-color:var(--ok)}
.meshmap-legend i.hop{border-top-style:dashed;border-top-color:var(--warn)}
.meshmap-legend i.unk{border-top-style:dotted}
.meshmap-legend i.gw,.meshmap-legend i.node{width:9px;height:9px;border:0;border-radius:50%;
 background:var(--acc)}
.meshmap-legend i.node{background:var(--ok)}
.meshmap-legend i.stale{background:var(--mute)}
.meshmap-place{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-top:10px}
.meshmap-place input{width:120px}
.meshmap-nodes{margin-top:10px;font-size:12px}
.meshmap-nodes .nolo{color:var(--mute);font-family:var(--font-mono);font-size:11.5px;line-height:1.6}
.gm-pop{font-family:var(--font-mono);font-size:11.5px;line-height:1.6;color:#1A1A1A}
.gm-pop b{font-family:var(--font-display);font-size:12.5px}
.svc{display:inline-flex;background:var(--forest);color:var(--gold-light);
 border-radius:5px;padding:3px 4px;line-height:0}
.svc svg{width:14px;height:14px;display:block}
.chip-admin{display:inline-block;margin-left:8px;vertical-align:middle;background:var(--gold);
 color:#113308;font:700 9.5px var(--font-display);letter-spacing:.08em;text-transform:uppercase;
 padding:2px 7px;border-radius:var(--r-pill)}
/* the key under the board: names each icon, since a hover tooltip is easy to miss */
.svc-key{display:flex;gap:8px 18px;flex-wrap:wrap;align-items:center;
 margin:12px 2px 0;font:500 11.5px var(--font-display);color:var(--mute)}
.svc-key .ki{display:inline-flex;align-items:center;gap:6px}
.svc-key .svc{padding:2px 3px}
.svc-key .chip-admin{margin:0}
.svc-key .kk{font:700 10px var(--font-display);letter-spacing:.08em;text-transform:uppercase;color:var(--fg2)}
footer code{background:var(--code-bg);padding:1px 5px;border-radius:var(--r-sm);color:var(--code-fg)}
/* 1.7.0 agent queue */
.topnav .navbadge{display:inline-block;min-width:17px;margin-left:6px;padding:0 5px;
 background:var(--gold);color:var(--forest-deep);border-radius:var(--r-pill);
 font-family:var(--font-mono);font-size:11px;font-weight:600;text-align:center;line-height:17px}
.prop{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--gold);
 border-radius:var(--r-card);padding:14px 16px;margin:0 0 14px;box-shadow:var(--shadow)}
.prop-h{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.prop-src{font-family:var(--font-display);font-weight:600;font-size:11px;letter-spacing:.12em;
 text-transform:uppercase;color:var(--acc)}
.prop-when{font-family:var(--font-mono);font-size:11.5px;color:var(--mute)}
.p-dismiss{margin-left:auto;background:transparent;border:1px solid var(--line);color:var(--fg2);
 border-radius:var(--r-sm);padding:4px 10px;font-family:var(--font-sans);font-size:12.5px;cursor:pointer}
.p-dismiss:hover{background:var(--hover);border-color:var(--rule2)}
.p-dismiss:disabled{opacity:.5;cursor:default}
.prop-sum{font-size:14.5px;margin-bottom:6px}
.prop-why{background:var(--bh);border-radius:var(--r-sm);padding:8px 11px;margin-bottom:11px;
 font-size:13px;color:var(--fg2)}
.prop form.action{margin:0}
/* 1.9.0 chat with Sam */
.samcard{margin:16px 0 6px;padding:22px;background:var(--card);border:1px solid var(--line);
 border-left:3px solid var(--gold);border-radius:var(--r-card);box-shadow:var(--shadow)}
.samgo{display:inline-block;background:var(--forest);color:var(--gold-light);font-family:var(--font-display);
 font-weight:700;font-size:15px;text-decoration:none;padding:11px 22px;border-radius:var(--r-sm);
 border:1px solid var(--forest-deep)}
.samgo:hover{background:var(--forest-soft);color:#fff}
.samgo:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.samcard .meta{margin-top:12px}
/* 1.11.0 native chat with Sam */
.schat{margin:16px 0 6px;background:var(--card);border:1px solid var(--line);
 border-left:3px solid var(--gold);border-radius:var(--r-card);box-shadow:var(--shadow);overflow:hidden}
.scthread{min-height:140px;max-height:440px;overflow-y:auto;padding:16px;
 display:flex;flex-direction:column;gap:9px}
.scmsg{max-width:78%;padding:9px 13px;border-radius:10px;font-size:14.5px;line-height:1.5;
 white-space:pre-wrap;overflow-wrap:break-word}
.scmsg.me{align-self:flex-end;background:var(--forest);color:var(--paper);
 border-bottom-right-radius:3px}
.scmsg.sam{align-self:flex-start;background:var(--bh);border:1px solid var(--line);
 border-bottom-left-radius:3px}
.scmsg.wait{align-self:flex-start;color:var(--mute);font-style:italic;background:none;
 border:none;padding:2px 13px}
.scmsg.err{align-self:flex-start;background:var(--fail-b);border:1px solid var(--fail);
 color:var(--fg)}
.scempty{color:var(--mute);font-size:13.5px;padding:4px 2px}
.scbar{display:flex;gap:9px;padding:11px;border-top:1px solid var(--line);background:var(--bh)}
.scbar input{flex:1;font-family:var(--font-sans);font-size:14.5px;color:var(--fg);
 background:var(--card);border:1px solid var(--rule2);border-radius:var(--r-sm);padding:9px 12px}
.scbar input:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
.scbar button{background:var(--forest);color:var(--gold-light);font-family:var(--font-display);
 font-weight:700;font-size:14px;border:1px solid var(--forest-deep);border-radius:var(--r-sm);
 padding:9px 20px;cursor:pointer}
.scbar button:hover{background:var(--forest-soft);color:#fff}
.scbar button:disabled{opacity:.55;cursor:default}
.scbar button:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.scnote{padding:9px 16px;font-size:13px;color:var(--fg2);border-top:1px solid var(--line)}
.scnote.unpaired{background:var(--warn-b);border-left:3px solid var(--warn)}
/* 1.15.0 credentials */
.creds{margin:16px 0 6px}
.cred-tools{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:12px}
.cred-refresh{background:var(--forest);color:var(--gold-light);font-family:var(--font-display);
 font-weight:700;font-size:14px;border:1px solid var(--forest-deep);border-radius:var(--r-sm);
 padding:9px 18px;cursor:pointer}
.cred-refresh:hover{background:var(--forest-soft);color:#fff}
.cred-pass{display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:var(--font-display);
 font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--mute)}
.cred-pass input{font:400 13.5px/1.4 var(--font-sans);color:var(--fg);background:var(--card);
 border:1px solid var(--rule2);border-radius:var(--r-sm);padding:8px 11px;min-width:160px}
.cred-list{display:flex;flex-direction:column;gap:6px}
.cred-row{display:flex;align-items:center;gap:12px;padding:10px 14px;background:var(--card);
 border:1px solid var(--line);border-radius:var(--r-sm)}
.cred-name{font-family:var(--font-mono);font-weight:600;font-size:14px;color:var(--fg)}
.cred-meta{color:var(--mute);font-size:12.5px;flex:1}
.cred-dl{background:var(--bh);color:var(--fg);border:1px solid var(--rule2);border-radius:var(--r-sm);
 font-size:13px;padding:6px 14px;cursor:pointer}
.cred-dl:hover{background:var(--hover)}
.cred-empty{color:var(--mute);font-size:13.5px;padding:4px 2px}
.cred-view{margin-top:12px}
.cred-enrol{background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);padding:16px;
 display:flex;flex-direction:column;gap:10px;align-items:flex-start}
.cred-qr{width:220px;height:220px;image-rendering:pixelated;background:#fff;padding:8px;border-radius:var(--r-sm)}
.cred-lines code{font-family:var(--font-mono);font-size:12px;color:var(--fg2);word-break:break-all}
.cred-btns{display:flex;gap:10px}
.cred-btns button{background:var(--bh);color:var(--fg);border:1px solid var(--rule2);
 border-radius:var(--r-sm);font-size:13px;padding:6px 14px;cursor:pointer}
/* 1.13.0 deploy */
.depcard{margin:16px 0 6px;padding:22px;background:var(--card);border:1px solid var(--line);
 border-left:3px solid var(--gold);border-radius:var(--r-card);box-shadow:var(--shadow);
 display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px 18px}
.depcard .fedpop-note,.depcard .fedpop-act,.depdry{grid-column:1/-1}
.depdry{font-size:13.5px;color:var(--fg2);display:flex;gap:8px;align-items:center}
.depstatus{margin:12px 0 0;font-family:var(--font-mono);font-size:13px;min-height:20px}
.depstatus.run{color:var(--fg2)}
.depstatus.ok{color:var(--ok)}
.depstatus.err{color:var(--fail)}
.deplog{margin:8px 0 6px;background:var(--forest-deep);color:var(--paper2);border-radius:var(--r-card);
 padding:14px 16px;font:400 12.5px/1.55 var(--font-mono);max-height:560px;overflow:auto;
 min-height:60px;white-space:pre-wrap;overflow-wrap:break-word}
.deplog:empty{display:none}
/* 1.25.1: results and the build log span the whole card - they were living in one
   ~240px grid column each, which made a long install log unreadable */
#wizard .a-res,#wizard .deplog,#wz-credout{grid-column:1/-1}
/* 1.16.0 setup wizard */
#wizard{display:flex;flex-direction:column;gap:14px}
.rename-row{margin:6px 0 14px}
.rename-row form{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.rename-row .fl{margin:0}
.firstrun{background:var(--card);border:1px solid var(--gold);border-radius:var(--r-card);
 padding:26px 28px;margin:18px 0}
.firstrun .title{margin:0 0 6px}
.fr-steps{margin:10px 0 18px 20px;display:flex;flex-direction:column;gap:8px;
 font-size:14.5px;color:var(--fg)}
.fr-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:22px;
 margin:14px 0 18px}
.fr-col{display:flex;flex-direction:column;gap:10px}
.fr-pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fr-devices{display:flex;flex-direction:column;gap:10px;margin:6px 0 16px;
 padding-top:14px;border-top:1px solid var(--line)}
.fr-devices .wz-credrow{max-width:720px}
.fr-devices #fr-passwrap{max-width:340px}
.fr-acts{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.fr-acts a.a-go{text-decoration:none;display:inline-block}
.fr-alt{font-size:13.5px;color:var(--gold)}
/* 1.47.0 the File store, Finder-shaped */
.st-finder{border:1px solid var(--line);border-radius:var(--r-card);overflow:hidden;
 border-collapse:separate;border-spacing:0;width:100%}
.st-finder thead th{background:var(--bh);cursor:pointer;user-select:none;position:sticky;top:0;
 font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);
 padding:9px 12px;text-align:left}
.st-finder thead th:hover{color:var(--fg)}
.st-row{cursor:default}
.st-row td{padding:7px 12px;border-top:1px solid var(--line);font-size:13.5px}
.st-row:nth-child(even) td{background:color-mix(in srgb, var(--bh) 35%, transparent)}
.st-row:hover td{background:var(--hover)}
.st-row.sel td{background:var(--gold);color:#113308}
.st-row.sel .st-kind,.st-row.sel .lib-num,.st-row.sel .lib-date{color:#1c2a12}
.st-ico{width:16px;height:16px;vertical-align:-3px;margin-right:7px}
.st-name{font-weight:500}
.st-kind,.lib-date{color:var(--mute);font-size:12.5px}
.st-folder.on{border-color:var(--gold);background:var(--bh);box-shadow:inset 0 -3px 0 var(--gold)}
.st-toolbar{display:flex;align-items:center;gap:10px;margin:10px 0 8px;padding:8px 12px;
 background:var(--card);border:1px solid var(--gold);border-radius:var(--r-sm)}
.st-toolbar[hidden]{display:none}
.st-selname{font-weight:600;font-size:13.5px}
.st-tb-dl{text-decoration:none}

.cap-matrix{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin:8px 0 16px}
.cap-row{background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);padding:14px 16px}
.cap-h{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:6px}
.cap-where{font-size:12px;color:var(--mute);margin-bottom:6px}
.cap-what{font-size:13px}
.conns{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin:6px 0 14px}
.conn{background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);padding:14px 16px}
.conn-h{display:flex;justify-content:space-between;align-items:center;gap:8px}
.conn-route{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mute);
 background:var(--bh);border-radius:var(--r-pill);padding:3px 9px}
.conn-meta{font-size:12px;color:var(--mute);margin:6px 0 10px}
.conn-acts{display:flex;align-items:center;gap:10px;justify-content:space-between}
.conn-au{display:flex;flex-direction:column;font-size:11px;color:var(--mute);gap:2px}
.onto-wrap{margin:10px 0 6px;border:1px solid var(--line);border-radius:var(--r-card);padding:0 16px}
.onto-sum{cursor:pointer;padding:12px 2px;font-weight:600;list-style:none}
.onto-sum::-webkit-details-marker{display:none}
.ontos{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;padding-bottom:14px}
.onto{background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);padding:14px 16px;
 display:flex;flex-direction:column;gap:8px}
.onto-d{font-size:12.5px;color:var(--mute);flex:1}
.onto-custom{margin:6px 0 12px}
form.action.flash{outline:2px solid var(--gold);outline-offset:3px}
.a-danger,.st-tb-del,.vv-del,.lib-del,.cn-revoke{background:transparent;border:1px solid var(--fail);
 color:var(--fail);border-radius:var(--r-sm);padding:6px 14px;cursor:pointer}
.a-danger:hover,.st-tb-del:hover,.vv-del:hover,.lib-del:hover,.cn-revoke:hover{background:var(--fail);color:#fff}
.kv-sync{margin:8px 0;border:1px solid var(--line);border-radius:var(--r-card);padding:0 14px 10px}
.sync-cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:900px){.sync-cols{grid-template-columns:1fr}}
.sync-h{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);margin:14px 0 8px}
.sync-peer{background:var(--card);border:1px solid var(--line);border-radius:var(--r-sm);padding:10px 12px;margin-bottom:8px}
.sync-peer-h{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.sync-peer-acts{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center}
.sync-map{width:100%;background:var(--card);border:1px solid var(--line);border-radius:var(--r-sm);margin:4px 0 8px}
.sync-add,.sync-mint{margin-top:8px}
.vv-tree .row.dragover{outline:2px solid var(--gold);outline-offset:-2px;border-radius:6px}
.vv-panes.dropready{outline:2px dashed var(--gold);outline-offset:-4px;border-radius:8px}
.vv-tree .row[draggable=true]{cursor:grab}
.kv-graph{margin:8px 0;border:1px solid var(--line);border-radius:var(--r-card);padding:0 14px}
.kv-canvas{width:100%;display:block;background:var(--card);border-radius:var(--r-sm);margin:4px 0 8px;cursor:pointer}
.vv-renwrap{display:flex;align-items:flex-end;gap:10px;margin:8px 0;padding:10px 12px;
 background:var(--card);border:1px solid var(--gold);border-radius:var(--r-sm)}
.vv-renwrap[hidden]{display:none}
.onto-custom summary{cursor:pointer;font-size:13px;color:var(--gold);padding:6px 0}
.onto-json{width:100%;min-height:140px;font-family:var(--font-mono);font-size:12.5px;
 padding:10px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--bg);
 color:var(--fg);box-sizing:border-box}
.route-tabs{display:flex;gap:6px;margin:4px 0 12px}
.route-tab{background:var(--card);border:1px solid var(--line);border-radius:var(--r-pill);
 padding:8px 16px;cursor:pointer;font-size:13px;color:var(--fg)}
.route-tab.on{border-color:var(--gold);background:var(--bh);font-weight:600}
.route-panel{margin-bottom:10px}
.peer-tile{opacity:.85;border-style:dashed;cursor:default}
.prop-cell{display:flex;flex-direction:column;gap:4px}
.prop-why-top{font-size:12.5px;color:var(--mute);padding:2px 4px}
.conn-new{background:var(--card);border:1px solid var(--gold);border-radius:var(--r-card);padding:16px}
.cn-field{display:flex;gap:10px;align-items:baseline;margin:6px 0}
.cn-field span{width:80px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute)}
.cn-field code{font-size:12.5px;word-break:break-all}
.cn-tok{color:var(--gold);font-weight:600}
.act-feed{border:1px solid var(--line);border-radius:var(--r-card);overflow:hidden}
.act-row{display:grid;grid-template-columns:70px 1fr 140px 70px;gap:8px;padding:7px 12px;
 border-top:1px solid var(--line);font-size:12.5px;align-items:center}
.act-row:first-child{border-top:0}
.act-t{color:var(--mute);font-family:var(--font-mono)}
.act-w{color:var(--mute);font-family:var(--font-mono);font-size:11.5px;overflow:hidden;text-overflow:ellipsis}
.act-r{text-align:right;color:var(--ok);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.mcp-private{margin:6px 0 12px}
.mcp-private summary{cursor:pointer;font-size:13px;color:var(--gold);padding:4px 0}
.chatbox{border:1px solid var(--line);border-radius:var(--r-card);margin-top:12px;overflow:hidden}
.chat-head{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--line);font-size:13.5px}
.chatlog{max-height:360px;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.chat-u{align-self:flex-end;background:var(--gold);color:#113308;padding:8px 12px;border-radius:12px 12px 2px 12px;max-width:80%;font-size:13.5px}
.chat-a{align-self:flex-start;background:var(--card);border:1px solid var(--line);padding:8px 12px;border-radius:12px 12px 12px 2px;max-width:85%;font-size:13.5px;white-space:pre-wrap}
.chat-err{align-self:flex-start;color:var(--fail);font-size:13px}
.chat-used{align-self:flex-start;font-size:11px;color:var(--mute);font-family:var(--font-mono)}
.chatbar{display:flex;gap:8px;padding:10px;border-top:1px solid var(--line)}
.chatbar input{flex:1;padding:9px 12px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--bg);color:var(--fg)}
.chatbar button{padding:9px 18px;border:0;border-radius:var(--r-sm);background:var(--deep-green,#113308);color:#F7F6EB;cursor:pointer}
.cn-cfg{background:var(--code-bg);color:var(--code-fg);padding:12px;border-radius:var(--r-sm);
 overflow-x:auto;font-size:12px}
.fr-timenote{margin:12px 0 8px;padding:10px 14px;background:var(--bh);
 border-left:3px solid var(--gold);border-radius:var(--r-sm);font-size:13px;color:var(--fg)}
.fr-credout{margin:14px 0;background:var(--card);border:1px solid var(--ok);
 border-radius:var(--r-card);padding:14px 16px}
.fr-dry{background:none;border:1px solid var(--line);color:var(--mute);border-radius:var(--r-sm);
 padding:10px 14px;font-size:13px;cursor:pointer}
.fr-dry:hover{border-color:var(--gold);color:var(--fg)}
.fr-dry:disabled{opacity:.45;cursor:default}
.wz-selfrow{display:flex;align-items:center;gap:14px;margin:0 0 14px;padding-bottom:14px;
 border-bottom:1px solid var(--line);flex-wrap:wrap}
.secgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.seccard{background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);
 padding:14px 16px;display:flex;flex-direction:column;gap:8px}
.sec-h{display:flex;justify-content:space-between;align-items:center}
.sec-badge{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;padding:3px 8px;
 border-radius:var(--r-pill);font-weight:700;background:var(--bh);color:var(--mute)}
.sec-badge.ok{background:var(--ok-b);color:var(--ok)}
.sec-badge.warn{background:#4a3d10;color:var(--warn)}
.sec-body{font-size:12.5px;color:var(--mute)}
.sec-acts a{font-size:13px;font-weight:600;color:var(--gold)}
.credrows{margin-top:8px;font-family:var(--font-mono);font-size:12.5px;
 white-space:pre-wrap;overflow-wrap:anywhere;display:flex;flex-direction:column;gap:3px}
.cz-colwrap{display:flex;align-items:center;gap:10px}
.cz-colwrap input[type=color]{width:46px;height:34px;padding:0;border:1px solid var(--line);
 border-radius:var(--r-sm);background:none;cursor:pointer}
.cz-colwrap code{font-size:12.5px;color:var(--mute)}
.mods{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
/* the loadout editor: tick what a box carries */
.lo-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;margin:12px 0}
.lo-item{display:flex;align-items:center;gap:8px;font:500 13px var(--font-display);
 padding:9px 11px;border:1px solid var(--line);border-radius:var(--r-sm);cursor:pointer;background:var(--card)}
.lo-item:hover{background:var(--hover)}
.lo-item input{margin:0;flex:0 0 auto;accent-color:var(--forest)}
.lo-item input:disabled{opacity:.6}
.lo-req{margin-left:auto;font:600 9px var(--font-mono);letter-spacing:.06em;text-transform:uppercase;color:var(--mute)}
.mod{background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);
 padding:14px 16px;display:flex;flex-direction:column;gap:8px}
.mod-h{display:flex;justify-content:space-between;align-items:center;gap:8px}
.mod-b{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;padding:3px 8px;
 border-radius:var(--r-pill);font-weight:700}
.mod-b.ok{background:var(--ok-b);color:var(--ok)}
.mod-b.drift{background:#4a3d10;color:var(--warn)}
.mod-b.abs{background:var(--bh);color:var(--mute)}
.mod-d{font-size:12.5px;color:var(--mute)}
.mod-act{font-size:13px;font-weight:600;color:var(--gold)}
.mod form.action{margin:0;padding:0;border:0;background:none}
.eud-strip{display:flex;gap:18px;align-items:center;justify-content:space-between;
 background:var(--card);border:1px solid var(--line);border-left:3px solid var(--gold);
 border-radius:var(--r-card);padding:14px 18px;margin:0 0 14px}
.eud-qr{width:110px;height:110px;background:#fff;padding:6px;border-radius:6px;flex:0 0 auto}
.wz-comp-t{display:block;font-size:12px;letter-spacing:.08em;text-transform:uppercase;
 color:var(--mute);margin-bottom:7px}
.wz-comps{display:flex;flex-direction:column;gap:8px}
.wz-comp{display:flex;gap:10px;align-items:flex-start;padding:9px 12px;background:var(--card);
 border:1px solid var(--line);border-radius:var(--r-sm);cursor:pointer;font-size:13.5px}
.wz-comp:has(input:checked){border-color:var(--gold);background:var(--bh)}
.wz-comp small{display:block;color:var(--mute);font-size:12px;margin-top:2px}
.wz-deps{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 14px}
.wz-dep{display:flex;align-items:center;gap:10px;padding:9px 14px;background:var(--card);
 border:1px solid var(--line);border-left:3px solid var(--gold);border-radius:var(--r-sm);
 cursor:pointer;font-size:13px}
.wz-dep:hover{background:var(--bh)}
.wz-dep .meta{font-size:11.5px;color:var(--mute)}
.wz-dep .wzd-del{background:none;border:none;color:var(--mute);cursor:pointer;font-size:15px}
.cred-pass .req{color:var(--warn);font-size:11px;margin-left:6px}
.wz-step{border:1px solid var(--line);border-radius:var(--r-card);padding:18px 22px;margin:0}
.wz-step legend{font-family:var(--font-display);font-weight:700;font-size:14px;color:var(--fg);
 padding:0 8px}
.wz-step.locked{opacity:.45;pointer-events:none}
.wz-access{grid-column:1/-1;display:flex;flex-direction:column;gap:10px;font-size:13.5px;
 color:var(--fg2);padding:12px;background:var(--bh);border-radius:var(--r-sm)}
.wz-acc-row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.wz-pub{display:block;font-family:var(--font-mono);font-size:11.5px;word-break:break-all;
 background:var(--code-bg);color:var(--code-fg);padding:10px 12px;border-radius:var(--r-sm);
 user-select:all}
.wz-keytext{width:100%;min-height:120px;font-family:var(--font-mono);font-size:11.5px;
 background:var(--code-bg);color:var(--code-fg);border:1px solid var(--line);
 border-radius:var(--r-sm);padding:10px 12px;resize:vertical}
.wz-lib{display:flex;flex-direction:column;gap:6px;grid-column:1/-1}
.wz-pkg{display:flex;gap:10px;align-items:center;padding:9px 12px;background:var(--card);
 border:1px solid var(--line);border-radius:var(--r-sm);cursor:pointer}
.wz-pkg:has(input:checked){border-color:var(--gold);background:var(--bh)}
.wz-credrow{display:flex;gap:10px;margin-bottom:8px}
.wz-credrow input{font:400 13.5px/1.4 var(--font-sans);color:var(--fg);background:var(--card);
 border:1px solid var(--rule2);border-radius:var(--r-sm);padding:8px 11px}
.wzc-del{background:var(--fail-b);border:1px solid var(--fail);color:var(--fg);
 border-radius:var(--r-sm);padding:0 12px;cursor:pointer}
#wz-arch{font:400 13.5px/1.4 var(--font-sans);color:var(--fg);background:var(--card);
 border:1px solid var(--rule2);border-radius:var(--r-sm);padding:8px 11px}
label.cred-dl{cursor:pointer}
/* 1.18.0 the Library page (Finder-like filestore) */
.brand{font-family:var(--font-display);font-weight:800;font-size:19px;letter-spacing:-.01em;
 color:var(--hdr-fg);margin-left:2px}
.themebtn{background:transparent;border:1px solid var(--hdr-mute);color:var(--hdr-fg);
 border-radius:var(--r-pill);width:32px;height:32px;font-size:15px;cursor:pointer;
 margin-left:14px;line-height:1;flex:0 0 auto;display:inline-flex;
 align-items:center;justify-content:center;text-decoration:none}
.themebtn:hover{background:rgba(247,246,235,.12)}
.themebtn:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.lib-status{color:var(--mute);font-weight:500}
.lib-drop{margin:12px 0;border:2px dashed var(--rule2);border-radius:var(--r-card);
 background:var(--bh);padding:26px 18px;text-align:center;transition:border-color .12s,background .12s}
.lib-drop.over{border-color:var(--gold);background:var(--hover)}
.lib-drop-in{font-size:14.5px;color:var(--fg2);display:flex;gap:14px;align-items:center;
 justify-content:center;flex-wrap:wrap}
.lib-link{color:var(--forest);font-weight:700;text-decoration:underline;cursor:pointer}
:root:not([data-theme=light]) .lib-link{color:var(--gold)}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]) .lib-link{color:var(--gold)}}
.lib-archwrap{font-size:12.5px;color:var(--mute)}
.lib-arch-sel{font:400 13px/1.4 var(--font-sans);color:var(--fg);background:var(--card);
 border:1px solid var(--rule2);border-radius:var(--r-sm);padding:6px 9px;margin-left:6px}
.lib-bar{width:100%;margin-top:12px}
.lib-table{width:100%;border-collapse:collapse;margin:6px 0;font-size:14px}
.lib-table th{text-align:left;font-family:var(--font-display);font-weight:700;font-size:11.5px;
 letter-spacing:.05em;text-transform:uppercase;color:var(--mute);padding:8px 12px;
 border-bottom:1px solid var(--line)}
.lib-sortable{cursor:pointer;user-select:none}
.lib-sortable:hover{color:var(--fg)}
.lib-table td{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
.lib-name{font-family:var(--font-mono);font-weight:600;color:var(--fg)}
.lib-ico{margin-right:9px;font-size:16px}
.lib-arch{font-family:var(--font-mono);font-size:11.5px;background:var(--bh);
 border:1px solid var(--line);border-radius:var(--r-pill);padding:2px 10px;color:var(--fg2)}
.lib-num,.lib-date{font-family:var(--font-mono);font-size:12.5px;color:var(--fg2);white-space:nowrap}
.lib-acts{display:flex;gap:8px;justify-content:flex-end}
.lib-dl{background:var(--forest);color:var(--gold-light);text-decoration:none;font-family:var(--font-display);
 font-weight:700;font-size:12.5px;padding:6px 14px;border-radius:var(--r-sm);border:1px solid var(--forest-deep)}
.lib-dl:hover{background:var(--forest-soft);color:#fff}
.lib-del{background:var(--bh);color:var(--fg);border:1px solid var(--rule2);border-radius:var(--r-sm);
 font-size:12.5px;padding:6px 12px;cursor:pointer}
.lib-del:hover{background:var(--fail-b);border-color:var(--fail)}
.lib-fetchrow{margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
.lib-log:empty{display:none}
/* 1.19.0 store file manager */
.st-crumb{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:6px 0 10px;
 font-family:var(--font-mono);font-size:13px}
.st-crumb a{color:var(--forest);text-decoration:none;font-weight:600}
:root:not([data-theme=light]) .st-crumb a{color:var(--gold)}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]) .st-crumb a{color:var(--gold)}}
.st-crumb a:hover{text-decoration:underline}
.st-sep{color:var(--mute)}
.st-folders{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px}
.st-folder{display:flex;align-items:center;gap:8px;background:var(--card);border:1px solid var(--line);
 border-radius:var(--r-card);padding:12px 16px;cursor:pointer;font-size:14px;color:var(--fg)}
.st-folder:hover{background:var(--hover);border-color:var(--rule2)}
.st-fname{font-family:var(--font-display);font-weight:700}
.st-fmeta{color:var(--mute);font-size:12px;font-family:var(--font-mono)}
.st-drop{margin:0 0 12px;border:2px dashed var(--rule2);border-radius:var(--r-card);
 background:var(--bh);padding:20px 18px;text-align:center;transition:border-color .12s,background .12s}
.st-drop.over{border-color:var(--gold);background:var(--hover)}
.st-arch{font:400 13px/1.4 var(--font-sans);color:var(--fg);background:var(--card);
 border:1px solid var(--rule2);border-radius:var(--r-sm);padding:6px 9px}
.st-name{font-family:var(--font-mono);font-weight:600;color:var(--fg)}
.st-move{font:400 12.5px/1.4 var(--font-sans);color:var(--fg);background:var(--card);
 border:1px solid var(--rule2);border-radius:var(--r-sm);padding:5px 8px}
.st-newfolder{margin-left:6px}
.st-bar{width:100%;margin-top:12px}
.st-log:empty{display:none}
.cred-del{background:var(--bh);color:var(--fg);border:1px solid var(--rule2);border-radius:var(--r-sm);
 font-size:13px;padding:6px 14px;cursor:pointer}
/* 1.20.0 vault editor */
.st-edit{background:var(--forest);color:var(--gold-light);border:1px solid var(--forest-deep);
 border-radius:var(--r-sm);font-size:12.5px;padding:6px 12px;cursor:pointer;font-weight:700;
 font-family:var(--font-display)}
.st-edit:hover{background:var(--forest-soft);color:#fff}
.st-editor{margin:14px 0;background:var(--card);border:1px solid var(--line);
 border-left:3px solid var(--gold);border-radius:var(--r-card);box-shadow:var(--shadow)}
.st-ed-head{display:flex;gap:10px;align-items:center;padding:10px 14px;
 border-bottom:1px solid var(--line);background:var(--bh)}
.st-ed-head .spacer{flex:1}
.st-ed-text{display:block;width:100%;box-sizing:border-box;min-height:380px;border:0;resize:vertical;
 background:var(--card);color:var(--fg);font:400 13.5px/1.6 var(--font-mono);padding:14px 16px}
.st-ed-text:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
/* 1.21.0 vault viewer (in the image of Vault Viewer v1.1.0) */
.vv-wrap{max-width:1480px}
.vv-bar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:10px 0}
.vv-locwrap.dragover{outline:2px solid var(--gold);border-radius:6px}
.vv-locwrap{font-size:12.5px;color:var(--mute)}
.vv-loc{font-family:var(--font-mono);font-weight:600;color:var(--fg2)}
.vv-panes{display:grid;grid-template-columns:290px 1fr;gap:0;border:1px solid var(--line);
 border-radius:var(--r-card);overflow:hidden;background:var(--card);box-shadow:var(--shadow);
 min-height:560px}
@media(max-width:900px){.vv-panes{grid-template-columns:1fr}.vv-tree{max-height:260px}}
.vv-tree{background:var(--bh);border-right:1px solid var(--line);overflow:auto;padding:10px 6px;
 max-height:78vh}
.tree ul{list-style:none;margin:0;padding-left:14px}
.vv-tree>ul{padding-left:4px}
.tree li{margin:1px 0}
.tree .folder>.row,.tree .file>.row{display:flex;align-items:center;gap:5px;padding:3px 7px;
 border-radius:6px;cursor:pointer;font-size:13px;color:var(--fg);white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
.tree .row:hover{background:var(--hover)}
.tree .file.active>.row{background:var(--hover);color:var(--forest);font-weight:600}
:root:not([data-theme=light]) .tree .file.active>.row{color:var(--gold)}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]) .tree .file.active>.row{color:var(--gold)}}
.tree .row.selfolder{outline:1px dashed var(--gold-deep)}
.tree .twirl{width:12px;flex:0 0 12px;color:var(--mute);transition:transform .12s;font-size:10px}
.tree .folder.collapsed>ul{display:none}
.tree .folder.collapsed>.row .twirl{transform:rotate(-90deg)}
.tree .ico{flex:0 0 14px;opacity:.65;font-size:12px}
.vv-main{overflow:auto;padding:20px 26px;max-height:78vh}
.vv-welcome{color:var(--mute);font-size:14px;padding:30px 10px}
.vv-crumb{font-family:var(--font-mono);font-size:11.5px;color:var(--mute);margin-bottom:12px}
.md{max-width:820px;font-size:14.5px;line-height:1.65}
.md h1,.md h2,.md h3,.md h4,.md h5,.md h6{font-family:var(--font-display);line-height:1.2;
 margin:1.4em 0 .5em;font-weight:700;color:var(--forest)}
:root:not([data-theme=light]) .md h1,:root:not([data-theme=light]) .md h2,
:root:not([data-theme=light]) .md h3,:root:not([data-theme=light]) .md h4{color:var(--gold)}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]) .md h1,
 :root:not([data-theme=light]) .md h2,:root:not([data-theme=light]) .md h3,
 :root:not([data-theme=light]) .md h4{color:var(--gold)}}
.md h1{font-size:1.8em;margin-top:0}
.md h2{font-size:1.4em;border-bottom:1px solid var(--line);padding-bottom:.2em}
.md h3{font-size:1.2em}
.md p{margin:.7em 0}
.md a.wikilink{color:var(--forest);cursor:pointer;text-decoration:underline;
 text-decoration-style:dashed;text-underline-offset:2px}
:root:not([data-theme=light]) .md a.wikilink{color:var(--gold)}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]) .md a.wikilink{color:var(--gold)}}
.md a.wikilink.unresolved{color:var(--mute);text-decoration-style:dotted;cursor:default}
.md code{font-family:var(--font-mono);background:var(--code-bg);color:var(--code-fg);
 padding:.12em .4em;border-radius:4px;font-size:.88em}
.md pre{background:var(--code-bg);padding:14px 16px;border-radius:8px;overflow:auto;margin:1em 0}
.md pre code{background:none;padding:0;font-size:.86em;line-height:1.5}
.md blockquote{margin:1em 0;padding:.3em 1em;border-left:3px solid var(--gold);
 color:var(--fg2);background:var(--bh);border-radius:0 6px 6px 0}
.md ul,.md ol{margin:.6em 0;padding-left:1.6em}
.md li{margin:.2em 0}
.md li.task{list-style:none;margin-left:-1.3em}
.md li.task input{margin-right:6px}
.md table{border-collapse:collapse;margin:1em 0;font-size:.92em;display:block;overflow-x:auto}
.md th,.md td{border:1px solid var(--line);padding:6px 11px;text-align:left}
.md th{background:var(--bh);font-family:var(--font-display);font-weight:700;font-size:.85em}
.md img{max-width:100%;border-radius:6px}
.md hr{border:0;border-top:1px solid var(--line);margin:1.6em 0}
.md mark{background:var(--gold-light);color:var(--ink);padding:0 3px;border-radius:3px}
.vv-tag{font-family:var(--font-mono);font-size:.85em;background:var(--bh);
 border:1px solid var(--line);border-radius:var(--r-pill);padding:1px 8px;color:var(--fg2)}
.vv-fm{margin:0 0 14px;border:1px solid var(--line);border-radius:var(--r-sm);
 background:var(--bh);font-size:12.5px}
.vv-fm summary{padding:6px 10px;cursor:pointer;color:var(--mute);
 font-family:var(--font-display);font-weight:600}
.vv-fm table{margin:4px 10px 8px;border-collapse:collapse}
.vv-fm td{padding:2px 10px 2px 0;color:var(--fg2);vertical-align:top}
.vv-fm td:first-child{font-family:var(--font-mono);color:var(--mute);white-space:nowrap}
.vv-embed{border:1px solid var(--line);border-left:3px solid var(--gold-deep);
 border-radius:var(--r-sm);padding:8px 14px;margin:.8em 0;background:var(--bh)}
.vv-embed-t{font-family:var(--font-display);font-weight:700;font-size:12px;color:var(--mute);
 margin-bottom:4px}
.vv-missing{color:var(--warn);font-size:.9em}
.vv-ta{display:block;width:100%;box-sizing:border-box;min-height:60vh;border:1px solid var(--line);
 border-radius:var(--r-sm);resize:vertical;background:var(--card);color:var(--fg);
 font:400 13.5px/1.6 var(--font-mono);padding:14px 16px}
.vv-edbar{display:flex;gap:10px;margin-top:10px}
.vv-uplab{display:inline-block}
/* 1.8.0 federation map */
.fedmap-wrap{margin:14px 0 6px;background:var(--card);border:1px solid var(--line);
 border-radius:var(--r-card);overflow-x:auto;box-shadow:var(--shadow)}
#fedmap{display:block;width:100%;min-width:760px;touch-action:none}
.fnode{cursor:default}
.fnode.managed{cursor:grab}
.fnode.managed:active{cursor:grabbing}
.fnode-c{fill:var(--card);stroke:var(--rule2);stroke-width:2}
.fnode-c.on{stroke:var(--ok);stroke-width:3}
.fnode.off .fnode-c{stroke-dasharray:4 3;opacity:.7}
.fnode:focus{outline:none}
.fnode:focus .fnode-c{stroke:var(--focus);stroke-width:3}
.fnode-t{fill:var(--fg);font-family:var(--font-display);font-weight:700;font-size:13px;pointer-events:none}
.fnode-s{fill:var(--mute);font-family:var(--font-mono);font-size:10.5px;pointer-events:none}
.fedlink{stroke:var(--gold);stroke-width:2}
.fedlink.drag{stroke-dasharray:6 4}
.fedlink.faint{stroke:var(--gold-deep);stroke-width:1;opacity:.5}
.fedpeer{fill:var(--gold-deep);opacity:.7}
.fedpop{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;padding:20px}
.fedpop[hidden]{display:none}
.fedpop-back{position:absolute;inset:0;background:rgba(10,31,5,.45)}
.fedpop-card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);
 box-shadow:0 12px 40px rgba(17,51,8,.25);padding:18px 20px;width:100%;max-width:430px}
.fedpop-h{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.fedpop-h h3{margin:0;font-family:var(--font-display);font-size:16px;font-weight:700;flex:1}
.fedpop-x{background:transparent;border:0;color:var(--mute);font-size:22px;line-height:1;cursor:pointer;padding:0 4px}
.fedpop-x:hover{color:var(--fg)}
.fedpop-b{font-size:13.5px;color:var(--fg2);margin-bottom:12px}
.fedpop-act{margin-top:12px}
.fedpop-res{margin-top:10px}
/* 1.10.0 federation rework: legend, edges, partners, link list */
.fedlegend{display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 2px;font-family:var(--font-mono);
 font-size:11.5px;color:var(--fg2)}
.fl-i{display:inline-flex;align-items:center;gap:6px}
.fl-dot{width:12px;height:12px;border-radius:50%;border:2px solid var(--rule2);background:var(--card);flex:0 0 auto}
.fl-dot.on{border-color:var(--ok)}.fl-dot.off{border-style:dashed}
.fl-dot.ext{border-color:var(--blue);border-style:dotted}
.fl-dot.unrec{border-color:var(--warn);border-style:dashed}
.fedsteps{display:flex;flex-wrap:wrap;gap:6px 18px;margin:10px 0 2px;padding:10px 14px;
 background:var(--bh);border-radius:var(--r-sm);font-size:13px;color:var(--fg2)}
.fedsteps b{color:var(--fg)}
.fedhandle-c{fill:var(--gold);stroke:var(--forest-deep);stroke-width:1;cursor:crosshair}
.fedhandle-t{fill:var(--forest-deep);font-size:11px;pointer-events:none}
.fedhandle:hover .fedhandle-c{fill:var(--gold-warm)}
.fnode.managed .fnode-c{cursor:move}
.fnode.unrec .fnode-c{stroke:var(--warn);stroke-dasharray:5 3}
.fl-edge{width:20px;height:0;border-top:2px dashed var(--gold);flex:0 0 auto}
.fl-edge.live{border-top-style:solid}
.fedge{stroke:var(--gold);stroke-width:2;stroke-dasharray:6 4}
.fedge.live{stroke-dasharray:none;stroke-width:2.5}
.fedge.drag{stroke:var(--gold);stroke-width:2;stroke-dasharray:6 4;opacity:.8}
.fa-h{fill:var(--gold)}
.fedge-t{fill:var(--fg2);font-family:var(--font-mono);font-size:10.5px;pointer-events:none}
.fnode-c.ext{fill:var(--bh);stroke:var(--blue);stroke-dasharray:3 3}
.fedlinks-h{font-family:var(--font-display);font-weight:700;font-size:12px;letter-spacing:.04em;
 text-transform:uppercase;color:var(--mute);margin:4px 0 6px}
.fedlinks{display:flex;flex-direction:column;gap:6px;margin-bottom:6px}
.fedlink-row{display:flex;align-items:center;gap:10px;font-size:13px;padding:7px 10px;
 background:var(--bh);border-radius:var(--r-sm)}
.fedlink-row>span{flex:1}
.fedlink-row .mono{font-family:var(--font-mono);font-size:11.5px;color:var(--mute)}
.fl-live{color:var(--ok);font-weight:600;font-size:11px}
.fl-cfg{color:var(--warn);font-weight:600;font-size:11px}
.fedforget{background:transparent;border:1px solid var(--line);color:var(--fg2);border-radius:var(--r-sm);
 padding:3px 9px;font-size:12px;cursor:pointer}
.fedforget:hover{background:var(--hover);border-color:var(--rule2)}
.fedpop-note{font-size:11.5px;color:var(--mute);margin-top:8px}
.fedpop-known{background:var(--ok-b);border:1px solid var(--ok);color:var(--ok);border-radius:var(--r-sm);
 padding:8px 11px;margin-bottom:12px;font-size:12.5px}
.fedpop-card .fl{margin-top:10px}
.fedpop-card .hint{display:block;margin-top:3px;font-size:11.5px;color:var(--mute)}
/* 2.10.0 federation clarity: the name under the circle, one label per pair, and the group
   said out loud everywhere a link is shown. */
.fnode-n{fill:var(--fg);font-family:var(--font-display);font-weight:700;font-size:22px;pointer-events:none}
.fnode-w{fill:var(--mute);font-family:var(--font-mono);font-size:11px;pointer-events:none}
.fnode-t,.fnode-s{paint-order:stroke;stroke:var(--card);stroke-width:4px;stroke-linejoin:round;
 pointer-events:none}
.fnode-t{fill:var(--fg);font-family:var(--font-display);font-weight:700;font-size:13px}
.fnode-s{fill:var(--mute);font-family:var(--font-mono);font-size:10.5px}
.fnode-g{fill:var(--ok);font-weight:600}
.fnode-g.none{fill:var(--fail)}
/* the label is knocked out of whatever it crosses, so a line never eats a word */
.fedge-t{paint-order:stroke;stroke:var(--card);stroke-width:4px;stroke-linejoin:round;
 font-family:var(--font-mono);font-size:10.5px;pointer-events:none}
.fedge-st{fill:var(--fg2)}
.fedge-g{font-weight:600}
.fedge-g.ok{fill:var(--ok)}.fedge-g.warn{fill:var(--warn)}.fedge-g.bad{fill:var(--fail)}
/* a federate only seen on the wire: real, but nobody here knows which side dialled */
.fedge.wire{stroke:var(--blue);stroke-dasharray:2 4}
/* how-to, in two columns: the link, then what crosses it - the order operators get wrong */
.fedhow{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin:12px 0 2px}
.fedhow-c{background:var(--bh);border-radius:var(--r-sm);padding:12px 16px}
.fedhow-c h3{margin:0 0 6px;font-family:var(--font-display);font-size:12px;font-weight:700;
 letter-spacing:.06em;text-transform:uppercase;color:var(--mute)}
.fedhow-c ol{margin:0;padding-left:18px;font-size:13px;color:var(--fg2);line-height:1.55}
.fedhow-c li+li{margin-top:4px}
.fedhow-c b{color:var(--fg)}
.feddir{margin:10px 0 2px;padding:10px 14px;font-size:13px;color:var(--fg2);line-height:1.6;
 background:var(--card);border:1px solid var(--line);border-left:3px solid var(--gold);
 border-radius:var(--r-sm)}
.feddir b{color:var(--fg)}
.fl-g{font-family:var(--font-display);font-weight:700;font-size:10.5px;letter-spacing:.1em;
 text-transform:uppercase;color:var(--mute);min-width:56px}
.fl-edge.arr{border-top-style:solid;position:relative}
.fl-edge.arr::after{content:"▸";position:absolute;right:-7px;top:-9px;color:var(--gold);font-size:11px}
.fl-edge.both{border-top-style:solid;position:relative}
.fl-edge.both::after{content:"▸";position:absolute;right:-7px;top:-9px;color:var(--gold);font-size:11px}
.fl-edge.both::before{content:"◂";position:absolute;left:-7px;top:-9px;color:var(--gold);font-size:11px}
.fl-chip{display:inline-block;padding:1px 7px;border-radius:999px;font-family:var(--font-mono);
 font-size:11px;font-weight:600;background:var(--bh);color:var(--fg2);border:1px solid var(--line)}
.fl-chip.ok{background:var(--ok-b);color:var(--ok);border-color:var(--ok)}
.fl-chip.warn{background:var(--warn-b);color:var(--warn);border-color:var(--warn)}
.fl-chip.bad{background:var(--fail-b);color:var(--fail);border-color:var(--fail)}
.fl-num{display:inline-flex;align-items:center;justify-content:center;width:19px;height:19px;
 border-radius:50%;border:2px solid var(--ok);font-family:var(--font-display);font-weight:700;
 font-size:11px;color:var(--fg);flex:0 0 auto}
.fedtbl td:first-child{font-family:var(--font-display);font-size:12.5px}
.fedtbl-ar{color:var(--gold-deep);font-weight:700;padding:0 2px}
.fedtbl-m{color:var(--mute)}
.fedlink-row .fl-chip{margin-left:4px}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

FIRSTRUN_JS = r"""
(function(){
  var fr=document.getElementById('firstrun'); if(!fr) return;
  var enrolLocal=fr.getAttribute('data-mode')==='enrol-local';
  var go=document.getElementById('fr-go'), res=document.getElementById('fr-res'),
      log=document.getElementById('fr-log');
  function $(s){return document.getElementById(s);}
  var pkg=null, dry=$('fr-dry');
  function say(cls,t){res.className='lib-status '+(cls||'');res.textContent=t;}
  function J(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(b)}).then(function(r){return r.json().then(function(j){
      return {code:r.status,j:j};});});}

  // ---- the release: pick from the library, or upload with a progress readout
  var lib=$('fr-lib');
  function ready(){ if(!enrolLocal){go.disabled=!pkg; if(dry)dry.disabled=!pkg;} }
  if(lib&&lib.options.length){
    var o=lib.options[lib.selectedIndex];
    pkg={file:o.value,sha:o.getAttribute('data-sha')};
    lib.addEventListener('change',function(){
      var o2=lib.options[lib.selectedIndex];
      pkg={file:o2.value,sha:o2.getAttribute('data-sha')};
      $('fr-upstat').textContent='using '+o2.value; ready();
    });
  }
  ready();
  var fi=$('fr-file');
  if(fi)fi.addEventListener('change',function(){
    var f=fi.files[0]; if(!f)return;
    var st=$('fr-upstat'); st.textContent='uploading 0%';
    var x=new XMLHttpRequest();
    x.open('POST','/api/library/upload');
    x.setRequestHeader('X-Filename',f.name);
    x.upload.onprogress=function(ev){ if(ev.lengthComputable)
      st.textContent='uploading '+Math.round(100*ev.loaded/ev.total)+'% of '+f.name; };
    x.onload=function(){
      try{var j=JSON.parse(x.responseText);}catch(e){st.textContent='upload failed';return;}
      if(x.status===200){pkg={file:j.file,sha:j.sha256};
        st.textContent='uploaded '+j.file; ready();}
      else st.textContent=j.error||'upload failed';
    };
    x.onerror=function(){st.textContent='upload failed - is the console reachable?';};
    x.send(f);
  });

  // ---- deploy (or enrol), watching the job inline
  var phase='dry';
  var STAGES={
    harden:'securing and updating the operating system (the slowest stage on a fresh box)',
    deps:'installing the foundations - Java, the PostgreSQL database, the nginx web server',
    install:'installing TAK Server itself',
    database:'creating TAK Server\u2019s database',
    certs:'creating the certificate authority that will identify your server and devices',
    coreconfig:'writing TAK Server\u2019s configuration',
    letsencrypt:'getting a trusted TLS certificate for your public name',
    start:'starting TAK Server for the first time - it sets up its own tables, and can be quiet for up to 15 minutes; this is normal',
    components:'installing the extra components you chose'};
  var lastStage='';
  function narrate(txt){
    var m, re=/== STAGE (\d)\/(\d) ([a-z-]+)/g, last=null;
    while((m=re.exec(txt))) last=m;
    if(last){var d=STAGES[last[3]]||last[3];
      lastStage='Stage '+last[1]+' of '+last[2]+': '+d;}
    // the log window slides; a marker scrolling out of it must not degrade the message
    return lastStage||'Working\u2026';
  }
  function watch(job){
    var misses=0;
    if(!$('fr-timenote')){
      log.insertAdjacentHTML('beforebegin',
        '<div class=fr-timenote id=fr-timenote><b>Expect 20 to 30 minutes.</b> '
        +'Stage 1 (system update) and the final start of TAK Server are the long, '
        +'quiet ones - several silent minutes are normal. Refreshing is safe: the '
        +'build continues on the server, and the Overview shows a link back to it.</div>');
    }
    var t=setInterval(function(){
      fetch('/api/job/'+job).then(function(r){return r.json();}).then(function(j){
        misses=0;
        log.hidden=false; log.textContent=j.log||''; log.scrollTop=log.scrollHeight;
        if(j.status==='running'){say('',narrate(j.log||''));return;}
        clearInterval(t); go.disabled=false; if(dry)dry.disabled=false;
        if(j.status!=='done'){
          var lg=(j.log||'');
          if(lg.indexOf('letsencrypt')>=0&&(lg.lastIndexOf('certbot')>lg.length-4000||lg.indexOf('Certbot failed')>=0)){
            say('err','Failed at the certificate stage. Two usual causes: the DNS name does not '
              +'point at this server, or the provider\u2019s own firewall blocks port 80. Fix the '
              +'cause and press Install again - completed stages are skipped.');
          }else{
            say('err','Failed - the log above says why. Fix the cause and press Install again; '
              +'completed stages are skipped.');
          }
          return;}
        if(enrolLocal){say('ok','Enrolled. Loading the estate…');
          setTimeout(function(){location.href='/';},1800);return;}
        if(phase==='dry'){
          say('ok','Dry run clean. Read the log, then Install TAK Server.');
          if(dry)dry.disabled=false;
        } else {
          if(showFirstCreds(j)){say('ok','Built - and your first devices are below.');}
          else{say('ok','Built. Loading the estate…');
            setTimeout(function(){location.href='/';},2500);}}
      }).catch(function(){
        // NEVER freeze silently: after ~20s of failed polls, say so - and keep trying,
        // because the build carries on server-side whatever happens to this page
        misses++;
        if(misses===8) say('err','Lost contact with the console - the build continues '
          +'on the box regardless. This page keeps retrying; if it does not recover, '
          +'refresh, and check the console port is open in the firewall.');
      });
    },2500);
  }
  var fqf=$('fr-fqdn');
  if(fqf){
    var pf=document.createElement('span');pf.className='hint';pf.id='fr-pf';
    fqf.insertAdjacentElement('afterend',pf);
    fqf.addEventListener('blur',function(){
      var v=fqf.value.trim().toLowerCase(); if(!v){pf.textContent='';return;}
      pf.textContent='checking DNS\u2026';
      fetch('/api/setup/preflight?fqdn='+encodeURIComponent(v)).then(function(r){return r.json();})
      .then(function(j){
        if(!j.addrs||!j.addrs.length){
          pf.textContent='\u26a0 '+v+' does not resolve yet. The build will fail at the '
            +'certificate stage (about 20 minutes in) - point the DNS name at this server first.';
          pf.style.color='var(--fail)';return;}
        var here=location.hostname;
        if(j.addrs.indexOf(here)>=0){
          pf.textContent='\u2713 resolves to this server ('+here+')';pf.style.color='var(--ok)';}
        else{
          pf.textContent='\u26a0 resolves to '+j.addrs.join(', ')+' - this console is at '+here
            +'. If that is not this box, the certificate stage will fail.';
          pf.style.color='var(--fail)';}
      }).catch(function(){pf.textContent='';});
    });
  }

  function launch(asDry){
    phase=asDry?'dry':'live';
    var need=enrolLocal?[]:['fr-fqdn','fr-email','fr-org','fr-orgunit','fr-country','fr-state','fr-city'];
    for(var i=0;i<need.length;i++){var el=$(need[i]);
      if(!el.value.trim()){el.focus();say('err','Fill in the highlighted field.');return;}}
    if(!enrolLocal&&!pkg){say('err','Upload or pick a TAK Server release first.');return;}
    go.disabled=true; if(dry)dry.disabled=true;
    say('','Preparing this box…');
    var host=(location.hostname||'this-box');
    var ename=host.toLowerCase().replace(/[^a-z0-9-]/g,'-').slice(0,24)||'first-server';
    var name=ename.slice(0,20)+'-boot';
    J('/api/setup/self-target',{name:name}).then(function(x){
      if(x.code!==200){go.disabled=false;say('err',x.j.error||'could not prepare this box');return;}
      var fq=($('fr-fqdn')?$('fr-fqdn').value.trim():'');
      var body={name:name,dest:'root@127.0.0.1',estate_name:ename,
        label:x.j.host||host,profile:'cloud',confirm:true,creds:[]};
      var fc=collectCreds();
      if(fc.length){body.creds=fc;body.passphrase=($('fr-pass')?$('fr-pass').value:'');}
      if(enrolLocal){body.enrol_only='1';body.provision={fqdn:fq};}
      else{
        body.deb_file=pkg.file;body.deb_sha256=pkg.sha;
        var cap=$('fr-capass')?$('fr-capass').value:'';
        if(cap&&!/^[A-Za-z0-9._!-]{8,64}$/.test(cap)){go.disabled=false;if(dry)dry.disabled=false;
          say('err','Certificate password: 8-64 chars from letters, digits and . _ ! -');
          $('fr-capass').focus();return;}
        body.provision={fqdn:fq,ca_pass:cap,le_email:$('fr-email').value.trim(),
          org:$('fr-org').value.trim(),org_unit:$('fr-orgunit').value.trim(),
          country:$('fr-country').value.trim().toUpperCase(),
          state:$('fr-state').value.trim(),city:$('fr-city').value.trim(),
          deb:'/root/'+pkg.file,components:'',dry_run:(asDry?'1':'0')};
      }
      J('/api/setup/run',body).then(function(y){
        if(y.code!==200){go.disabled=false;if(dry)dry.disabled=false;
          say('err',y.j.error||'did not start');return;}
        watch(y.j.job);
      });
    });
  }
  go.addEventListener('click',function(){launch(false);});
  if(dry)dry.addEventListener('click',function(){launch(true);});

  // first devices: optional rows; the operator password appears when any are named
  var credBox=$('fr-creds'), addBtn=$('fr-addcred');
  function credRow(){
    var row=document.createElement('div');row.className='wz-credrow';
    row.innerHTML='<input class=wzc-user placeholder="device, e.g. alpha-1" maxlength=40>'
      +'<input class=wzc-group placeholder="group, e.g. blue-team" maxlength=40>'
      +'<input class=wzc-pass type=password placeholder="password (blank = auto)" maxlength=64 autocomplete=off>'
      +'<button type=button class=wzc-del>\u00d7</button>';
    row.querySelector('.wzc-del').onclick=function(){row.remove();passVis();};
    row.querySelector('.wzc-user').addEventListener('input',passVis);
    credBox.appendChild(row);
  }
  function collectCreds(){
    if(!credBox) return [];
    return [].map.call(credBox.querySelectorAll('.wz-credrow'),function(row){
      var pw=row.querySelector('.wzc-pass');
      return {user:row.querySelector('.wzc-user').value.trim(),
              group:row.querySelector('.wzc-group').value.trim(),
              password_b64:(pw&&pw.value)?btoa(pw.value):''};
    }).filter(function(c){return c.user&&c.group;});
  }
  function passVis(){ if($('fr-passwrap'))$('fr-passwrap').hidden=!collectCreds().length; }
  if(addBtn){addBtn.onclick=credRow;}
  function showFirstCreds(j){
    if(!j.creds||!j.creds.length) return false;
    var out='<div class=fr-credout><b>Your first devices are ready.</b> Scan each QR '
      +'with ATAK; the passwords are also re-downloadable from the server\u2019s page.';
    j.creds.forEach(function(c){
      out+='<div class=cred-enrol><div class="a-res ok">'+c.user+' ('+c.group+')</div>';
      if(c.png)out+='<img class=cred-qr alt="QR for '+c.user+'" src="data:image/png;base64,'+c.png+'">';
      if(c.password)out+='<div>Password: <code>'+c.password+'</code></div>';
      if(c.itak)out+='<div class=meta>iTAK: <code>'+c.itak+'</code></div>';
      out+='</div>';});
    out+='<div class=fr-acts style="margin-top:10px"><a class="a-go confirm" href="/">'
      +'Go to the estate</a></div></div>';
    log.insertAdjacentHTML('beforebegin',out);
    return true;
  }
})();
"""


UPGRADE_PICK_JS = r"""
(function(){
  document.querySelectorAll('form.action[data-id=upgrade-server]').forEach(function(f){
    var pick=f.querySelector('.up-pick'), file=f.querySelector('.up-file'),
        st=f.querySelector('.up-upstat'), hf=f.querySelector('.up-deb_file'),
        hs=f.querySelector('.up-deb_sha256');
    if(!pick||!hf) return;
    function sync(){var o=pick.options[pick.selectedIndex];
      hf.value=o?o.value:''; hs.value=o?(o.getAttribute('data-sha')||''):'';}
    pick.addEventListener('change',sync); sync();
    if(file)file.addEventListener('change',function(){
      var fl=file.files[0]; if(!fl)return;
      st.textContent='uploading 0%';
      var x=new XMLHttpRequest(); x.open('POST','/api/library/upload');
      x.setRequestHeader('X-Filename',fl.name);
      x.upload.onprogress=function(ev){if(ev.lengthComputable)
        st.textContent='uploading '+Math.round(100*ev.loaded/ev.total)+'%';};
      x.onload=function(){
        try{var j=JSON.parse(x.responseText);}catch(e){st.textContent='upload failed';return;}
        if(x.status!==200){st.textContent=j.error||'upload failed';return;}
        var o=document.createElement('option');
        o.value=j.file;o.setAttribute('data-sha',j.sha256);o.textContent=j.file;
        pick.appendChild(o);pick.value=j.file;sync();
        st.textContent='uploaded and selected: '+j.file;
      };
      x.onerror=function(){st.textContent='upload failed - console unreachable?';};
      x.send(fl);
    });
  });
})();
"""


SECURITY_JS = r"""
(function(){
  var grid=document.querySelector('.secgrid'); if(!grid) return;
  var T=grid.getAttribute('data-box');
  function badge(box,cls,txt){var b=box.querySelector('.sec-badge');
    b.className='sec-badge '+cls; b.textContent=txt;}
  function run(aid,cb){
    fetch('/api/action/'+aid,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({target:T,inputs:{},confirm:true})})
      .then(function(r){return r.json();}).then(cb).catch(function(){});
  }
  var h=document.getElementById('sec-harden');
  if(h) run('harden-plan',function(j){
    var m=(j.message||''), hard=/POSTURE hardened/.test(m);
    badge(h, hard?'ok':'warn', hard?'Hardened':'Standard');
    h.querySelector('.sec-acts').innerHTML = hard
      ? '<a href="#act-harden-revert">Revert to Standard</a>'
      : '<a href="#act-harden-apply">Harden this box</a>';
  });
  var f=document.getElementById('sec-fw');
  if(f) run('firewall-plan',function(j){
    var m=(j.message||''), clean=/firewall matches policy/.test(m);
    badge(f, clean?'ok':'warn', clean?'In policy':'Drift');
    f.querySelector('.sec-acts').innerHTML = clean
      ? '' : '<a href="#act-firewall-apply">Reconcile firewall</a>';
  });
})();
"""


AGENT_HUB_JS = r"""
(function(){
  var main=document.getElementById('main'); if(!main) return;
  function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
  // activity feed
  var feed=document.getElementById('actfeed');
  function fmtRow(e){
    var when=(e.ts||'').slice(11,19), act=e.action||'', d=document.createElement('div');
    d.className='act-row';
    var who=e.name||e.source||e.client||'', tool=e.tool?(' '+e.tool):'';
    var label=act==='mcp-tool'?('tool call'+tool):act==='agent-connect'?'connected':
      act==='agent-revoke'?'revoked':act==='agent-autonomy'?('autonomy \u2192 '+(e.autonomy||'')):
      act==='agent-key'?'API key set':act;
    var extra=e.target?(' on '+e.target):'';
    var res=e.result==='PROPOSED'?'proposed':e.result==='OK'?'ok':e.result||'';
    d.innerHTML='<span class=act-t>'+when+'</span><span class=act-l>'+esc(label)+esc(extra)
      +'</span><span class=act-w>'+esc(who)+'</span><span class=act-r>'+esc(res)+'</span>';
    return d;
  }
  function loadFeed(){ if(!feed)return;
    fetch('/api/agent/activity').then(function(r){return r.json();}).then(function(j){
      var rows=j.rows||[];
      if(!rows.length){feed.innerHTML='<div class=meta>No agent activity yet. Connect an '
        +'agent or use the chat, and its reads and actions appear here.</div>';return;}
      feed.innerHTML='';rows.forEach(function(e){feed.appendChild(fmtRow(e));});
    }).catch(function(){});
  }
  if(feed){loadFeed();setInterval(loadFeed,5000);}
  // route tabs
  main.querySelectorAll('.route-tab').forEach(function(t){t.onclick=function(){
    main.querySelectorAll('.route-tab').forEach(function(x){x.classList.toggle('on',x===t);});
    main.querySelectorAll('.route-panel').forEach(function(pn){
      pn.hidden = pn.getAttribute('data-r')!==t.getAttribute('data-r');});
  };});
  // create an MCP connection
  var f=main.querySelector('.addform[data-route=mcp]');
  if(f)f.addEventListener('submit',function(ev){ev.preventDefault();
    var name=f.querySelector('.cn-name').value.trim(),
        au=f.querySelector('.cn-autonomy').value,
        out=main.querySelector('.cn-result');
    if(!name){out.innerHTML='<div class="a-res error">Give the connection a name.</div>';return;}
    out.innerHTML='<div class=a-res>Creating…</div>';
    fetch('/api/agent/connect',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name,route:'mcp',autonomy:au})})
    .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
    .then(function(x){
      if(x.code!==200){out.innerHTML='<div class="a-res error">'+esc(x.j.error||'failed')+'</div>';return;}
      var url=location.origin+'/mcp', tok=x.j.token;
      var cfg=JSON.stringify({mcpServers:{vantage:{type:'http',url:url,
        headers:{Authorization:'Bearer '+tok}}}},null,2);
      out.innerHTML=
        '<div class=conn-new><b>Connection created — the token is shown once.</b>'
        +'<div class=cn-field><span>Endpoint</span><code>'+esc(url)+'</code></div>'
        +'<div class=cn-field><span>Token</span><code class=cn-tok>'+esc(tok)+'</code></div>'
        +'<div class=cn-field><span>Autonomy</span><code>'+esc(x.j.autonomy)+'</code></div>'
        +'<p class=meta>For a claude.ai or Cowork <b>custom connector</b>: add a connector with '
        +'the endpoint above and this token as a Bearer header. For Claude Desktop or Code, paste '
        +'this into the MCP config:</p>'
        +'<pre class=cn-cfg>'+esc(cfg)+'</pre>'
        +'<div class=fedpop-act><button type=button class=cn-test>Test from this browser</button>'
        +'<button type=button class=cn-done>Done - I saved the token</button>'
        +'<span class=cn-testres></span></div>'
        +'</div>';
      out.querySelector('.cn-done').onclick=function(){location.reload();};
      out.querySelector('.cn-test').onclick=function(){
        var tr=out.querySelector('.cn-testres');tr.textContent='Testing…';
        fetch('/mcp',{method:'POST',headers:{'Content-Type':'application/json',
          'Authorization':'Bearer '+tok},
          body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/list'})})
        .then(function(r){return r.json();}).then(function(j){
          var n=(j.result&&j.result.tools||[]).length;
          tr.textContent=n?('\u2713 answers from here - '+n+' tools at this autonomy. A cloud connector additionally needs this address reachable from the internet.'):'no tools returned';
          tr.style.color=n?'var(--ok)':'var(--fail)';
        }).catch(function(){tr.textContent='could not reach the endpoint';tr.style.color='var(--fail)';});
      };
      f.reset();
    }).catch(function(){out.innerHTML='<div class="a-res error">could not reach the console</div>';});
  });
  // API-key route: save/clear + a working chat
  var kf=document.getElementById('keyform');
  if(kf)kf.addEventListener('submit',function(ev){ev.preventDefault();
    var res=document.getElementById('ak-res');res.textContent='Saving\u2026';
    fetch('/api/agent/key',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:document.getElementById('ak-key').value,
        model:document.getElementById('ak-model').value.trim(),
        autonomy:document.getElementById('ak-autonomy').value})})
    .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
    .then(function(x){if(x.code!==200){res.textContent=x.j.error||'failed';return;}
      res.textContent='Saved. Reloading\u2026';setTimeout(function(){location.reload();},700);})
    .catch(function(){res.textContent='could not reach the console';});
  });
  var kc=document.getElementById('ak-clear');
  if(kc)kc.onclick=function(){if(!confirm('Remove the stored API key?'))return;
    fetch('/api/agent/key/clear',{method:'POST'}).then(function(){location.reload();});};
  var cform=document.getElementById('ak-chatform');
  if(cform){
    var log=document.getElementById('ak-log');
    fetch('/api/agent/chat/history').then(function(r){return r.json();}).then(function(j){
      (j.messages||[]).forEach(function(m){
        bubble(m.role==='user'?'u':'a',m.content);
        if(m.used&&m.used.length){var u=document.createElement('div');
          u.className='chat-used';u.textContent='used: '+m.used.join(', ');log.appendChild(u);}
      });
      log.scrollTop=log.scrollHeight;
    }).catch(function(){});
    var nb=document.getElementById('ak-new');
    if(nb)nb.onclick=function(){
      if(!confirm('Start a new conversation? The current one is cleared.'))return;
      fetch('/api/agent/chat/clear',{method:'POST'}).then(function(){location.reload();});};
    function bubble(role,txt){var d=document.createElement('div');
      d.className='chat-'+role;d.textContent=txt;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
    cform.addEventListener('submit',function(ev){ev.preventDefault();
      var inp=document.getElementById('ak-msg'), m=inp.value.trim(); if(!m)return;
      inp.value='';bubble('u',m);
      var w=bubble('a','\u2026');
      fetch('/api/agent/chat',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({message:m})})
      .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
      .then(function(x){
        if(x.code!==200){w.className='chat-err';w.textContent=x.j.error||'failed';return;}
        w.textContent=x.j.reply;
        if(x.j.used&&x.j.used.length){var u=document.createElement('div');
          u.className='chat-used';u.textContent='used: '+x.j.used.join(', ');log.appendChild(u);}
        log.scrollTop=log.scrollHeight;
      }).catch(function(){w.className='chat-err';w.textContent='could not reach the console';});
    });
  }
  // https switch: the console restarts wearing the box's certificate
  var hb=document.getElementById('httpson');
  if(hb)hb.onclick=function(){
    var fq=document.getElementById('httpsfqdn').value;
    if(!confirm('Restart this console on https://'+fq+':'+location.port+' ? You will sign in again.'))return;
    hb.disabled=true;hb.textContent='Switching\u2026';
    fetch('/api/agent/https-enable',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({fqdn:fq})})
    .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
    .then(function(x){
      if(x.code!==200){hb.disabled=false;hb.textContent='Serve HTTPS with this box\u2019s certificate';
        alert(x.j.error||'failed');return;}
      hb.textContent='Restarting on https\u2026';
      setTimeout(function(){location.href='https://'+fq+':'+location.port+'/agent';},4000);
    }).catch(function(){});
  };
  // AI configuration save
  var acf=document.getElementById('aicfg');
  if(acf)acf.addEventListener('submit',function(ev){ev.preventDefault();
    var res=document.getElementById('ac-res');res.textContent='Saving\u2026';
    var body={};['identity','orders','estate'].forEach(function(k){
      var el=document.getElementById('ac-'+k); if(el)body[k]=el.value;});
    fetch('/api/agent/context',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)})
    .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
    .then(function(x){res.textContent=x.code===200?'Saved.':(x.j.error||'failed');})
    .catch(function(){res.textContent='could not reach the console';});
  });
  // per-connection autonomy + revoke
  main.querySelectorAll('.conn').forEach(function(row){
    var id=row.getAttribute('data-id');
    var sel=row.querySelector('.cn-au');
    if(sel)sel.onchange=function(){
      fetch('/api/agent/autonomy',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:id,autonomy:sel.value})}).then(function(){});};
    var rv=row.querySelector('.cn-revoke');
    if(rv)rv.onclick=function(){ if(!confirm('Revoke this connection? Its token stops working.'))return;
      fetch('/api/agent/revoke',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:id})}).then(function(){location.reload();});};
  });
})();
"""


ACTION_JS = """
/* 1.24.0: bindings live in bindPage(root) so the Overview soft refresh can re-bind
   swapped-in content. Everything else about them is unchanged. */
function revealAction(id){
  var f=document.getElementById(id); if(!f)return null;
  var d=f.closest('details');
  while(d){d.open=true;d=d.parentElement&&d.parentElement.closest('details');}
  f.scrollIntoView({block:'center'});
  f.classList.add('flash');setTimeout(function(){f.classList.remove('flash');},1400);
  var g=f.querySelector('.a-go');if(g)g.focus();
  return f;
}
function bindPage(root){
root.querySelectorAll('a[href^="#act-"]').forEach(function(a){
  if(a.dataset.vrev)return;a.dataset.vrev=1;
  a.addEventListener('click',function(ev){ev.preventDefault();
    revealAction(a.getAttribute('href').slice(1));});
});
root.querySelectorAll('form.action').forEach(function(f){
  if(f.dataset.vbound)return; f.dataset.vbound=1;
  var res=f.querySelector('.a-res'),act=f.querySelector('.a-act'),go=f.querySelector('.a-go'),armed=false,cancel=null;
  function disarm(){armed=false;go.textContent='Run';go.classList.remove('confirm');
    var c=f.querySelector('.a-confirm');if(c)c.remove();if(cancel){cancel.remove();cancel=null;}}
  f.addEventListener('submit',function(ev){
    ev.preventDefault();
    if(!f.checkValidity()){f.reportValidity();return;}
    var id=f.dataset.id,tgt=f.querySelector('[name=target]').value,inputs={};
    f.querySelectorAll('input[name], select[name], textarea[name]').forEach(function(i){if(i.name!=='passphrase')inputs[i.name]=(i.dataset.encode==='b64'&&i.value)?btoa(unescape(encodeURIComponent(i.value))):i.value;});
    if(!armed && f.dataset.read!=='1'){
      var msg=f.dataset.confirm.replace(/\\{(\\w+)\\}/g,function(_,k){return k==='target'?tgt:(inputs[k]||'');});
      var d=document.createElement('div');d.className='a-confirm';d.setAttribute('role','alert');d.textContent='Confirm: '+msg;
      f.insertBefore(d,act);go.textContent='Confirm';go.classList.add('confirm');armed=true;
      cancel=document.createElement('button');cancel.type='button';cancel.className='a-cancel';cancel.textContent='Cancel';
      cancel.addEventListener('click',disarm);act.appendChild(cancel);
      return;
    }
    disarm();go.disabled=true;go.textContent='Running…';res.className='a-res';res.textContent='Running…';
    var body={target:tgt,inputs:inputs,confirm:true};
    var pass=f.querySelector('input[name=passphrase]');if(pass)body.passphrase=pass.value;
    fetch('/api/action/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(r){return r.json().then(function(j){return {code:r.status,j:j};});})
      .then(function(x){
        go.disabled=false;go.textContent='Run';var j=x.j;
        if(x.code===200&&j.job){
          res.className='a-res';res.textContent='Running as job '+j.job+'\\u2026';
          var pre=f.querySelector('pre.deplog');
          if(!pre){pre=document.createElement('pre');pre.className='deplog';f.appendChild(pre);}
          var jp=setInterval(function(){
            fetch('/api/job/'+j.job).then(function(rr){return rr.json();}).then(function(d){
              pre.textContent=d.log||'';pre.scrollTop=pre.scrollHeight;
              if(d.status==='running'){res.textContent='Running\\u2026';return;}
              clearInterval(jp);
              res.className='a-res '+(d.status==='done'?'ok':'error');
              res.textContent=d.status==='done'?'Done - the log above has the detail.'
                :'Failed (rc '+d.rc+') - the log says where.';
            }).catch(function(){});
          },2500);
          return;
        }
        if(x.code===200){
          res.className='a-res ok';var t=j.message||'Done.';if(j.fingerprint)t+='\\nFingerprint: '+j.fingerprint;
          res.textContent=t;
          if(j.p12&&j.name){var a=document.createElement('a');a.href='data:application/x-pkcs12;base64,'+j.p12;
            a.download=j.name+'.p12';a.textContent='Download '+j.name+'.p12';a.className='dl';
            res.appendChild(document.createElement('br'));res.appendChild(a);}
          if(j.png){var im=document.createElement('img');im.src='data:image/png;base64,'+j.png;
            im.alt='Enrolment QR for '+(j.name||'device');im.className='qr';res.appendChild(im);
            var cred=document.createElement('div');cred.textContent='Password: '+(j.password||'?');res.appendChild(cred);
            if(j.itak){var it=document.createElement('div');it.textContent='iTAK quick connect: '+j.itak;res.appendChild(it);}
            var sv=document.createElement('button');sv.type='button';sv.className='cred-refresh';
            sv.textContent='Save QR';sv.onclick=function(){
              var a=document.createElement('a');a.href='data:image/png;base64,'+j.png;
              a.download=(j.name||'device')+'-enrol-qr.png';document.body.appendChild(a);a.click();a.remove();};
            res.appendChild(sv);
            if(j.itak){var si=document.createElement('button');si.type='button';si.className='cred-refresh';
              si.style.marginLeft='8px';si.textContent='Save iTAK line';si.onclick=function(){
                var a=document.createElement('a');a.href='data:text/plain;base64,'+btoa(j.itak);
                a.download=(j.name||'device')+'-itak.txt';document.body.appendChild(a);a.click();a.remove();};
              res.appendChild(si);}}
          if(j.credentials){var ul=document.createElement('div');ul.className='credrows';
            j.credentials.forEach(function(c){var d=document.createElement('div');
              d.textContent=(c.ctype==='cert'?'certificate  ':'enrolment    ')+c.name
                +(c.expires?'  expires '+c.expires:'')+(c.created?'  created '+c.created:'')
                +(c.group?'  ('+c.group+')':'');
              ul.appendChild(d);});
            if(!j.credentials.length){ul.textContent='No credentials issued on this box yet.';}
            res.appendChild(ul);}
        }else{res.className='a-res error';res.textContent=(j.error||j.message||'Action failed. No detail returned.');}
        if(pass)pass.value='';
      })
      .catch(function(e){go.disabled=false;go.textContent='Run';res.className='a-res error';
        res.textContent='Could not reach the server. '+e;if(pass)pass.value='';});
  });
});
/* 1.2.0: drift Update links are plain anchors now - on a server page the forms are
   already bound to the box, so there is nothing to preselect. */
/* 1.4.0 supervised recovery: a "Propose fix" link jumps to the healing action and
   pre-fills its inputs from data-fix-* attributes, then focuses Run. The operator still
   reads the confirm and clicks; nothing runs on its own. Plain anchor without JS. */
root.querySelectorAll('a.fixlink').forEach(function(l){
  l.addEventListener('click',function(){
    var f=document.getElementById('act-'+l.dataset.fixAction);if(!f)return;
    Object.keys(l.dataset).forEach(function(k){
      if(k.indexOf('fix')===0&&k!=='fixAction'){
        var field=k.slice(3).toLowerCase();
        var inp=f.querySelector('[name='+field+']');if(inp)inp.value=l.dataset[k];
      }
    });
    setTimeout(function(){var g=f.querySelector('.a-go');if(g)g.focus();},80);
  });
});
/* 1.7.0 agent: dismiss a proposal from the queue. No-op on pages without one. */
root.querySelectorAll('.p-dismiss').forEach(function(b){
  b.addEventListener('click',function(){
    b.disabled=true;
    fetch('/api/propose/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:b.dataset.id})})
      .then(function(){location.reload();}).catch(function(){b.disabled=false;});
  });
});
}
bindPage(document);
if(location.hash.indexOf('#act-')===0)setTimeout(function(){revealAction(location.hash.slice(1));},150);
/* 1.24.0: no page reloads itself, ever - a timed reload was wiping form state on
   Deploy and every other interactive page. A page that marks <main data-live=1>
   (Overview) refreshes its CONTENT in place every 60s: fetch the page, swap main's
   children, carry over the header verdict and the checked chip, keep open sections
   open, re-bind. Anything the operator is touching or has typed defers the swap. */
(function(){
  var main=document.getElementById('main');
  if(!main||main.dataset.live!=='1')return;
  function busy(){
    if(main.querySelector('#firstrun,#wizard,#storepage,#vaultpage,#keyform,.addform'))return true;
    if(document.querySelector('form.action .a-confirm'))return true;
    var a=document.activeElement;
    if(a&&a!==document.body&&a.closest&&a.closest('#main'))return true;
    var used=false;
    main.querySelectorAll('input,textarea').forEach(function(i){
      if(i.type==='checkbox'||i.type==='radio'){if(i.checked!==i.defaultChecked)used=true;return;}
      if(i.value!==i.defaultValue)used=true;
    });
    return used;
  }
  setInterval(function(){
    if(busy())return;
    fetch(location.pathname+location.search,{cache:'no-store'}).then(function(r){
      return r.ok?r.text():null;
    }).then(function(html){
      if(!html)return;
      var doc=new DOMParser().parseFromString(html,'text/html');
      var fresh=doc.getElementById('main');
      if(!fresh)return;
      var open=[];main.querySelectorAll('details').forEach(function(d,i){if(d.open)open.push(i);});
      main.innerHTML=fresh.innerHTML;
      main.querySelectorAll('details').forEach(function(d,i){if(open.indexOf(i)>=0)d.open=true;});
      ['header .checked','header .verdict','.vband'].forEach(function(sel){
        var cur=document.querySelector(sel),nf=doc.querySelector(sel);
        if(cur&&nf)cur.replaceWith(document.importNode(nf,true));
      });
      bindPage(main);
    }).catch(function(){});
  },60000);
})();
"""

# 2.10.0 the federation map. Three fixes over 1.12.0, all of them things an operator hit:
#   - ONE line per federated PAIR. Two boxes that had each dialled the other drew two lines on
#     identical geometry with two labels on the same midpoint, which rendered as mush. A pair is
#     one federation; it gets one line, one label, and an arrowhead per direction actually made.
#   - The server's NAME lives under its circle, not inside it. A name longer than about eight
#     characters overflowed the circle and got truncated to nonsense ("Cloud - Playgr...").
#   - Every link says what CROSSES it. A live link with no shared group carries nothing, and a
#     map that draws the line but hides the group lets a half-done job look finished.
# The two drags are still separate gestures: drag a NODE to arrange the map (the position is
# POSTed and persists for every operator); drag the small CONNECT HANDLE beside a managed node to
# federate it. A connect drag never executes: it opens a confirm that POSTs the same gated
# federation-connect (or federation-enable) as the form.
FEDERATION_JS = """
(function(){
  var el=document.getElementById('fedmap'); if(!el) return;
  var data={}; try{data=JSON.parse(document.getElementById('feddata').textContent);}catch(e){return;}
  var nodes=data.nodes||[], pairs=data.pairs||[], canConnect=data.canConnect,
      canEnable=data.canEnable, canGroups=data.canGroups;
  var NS='http://www.w3.org/2000/svg', W=900, H=460, HR=11, linkLayer, nodeLayer,
      tempLine=null, dragFrom=null, moved=false, moveNode=null, moveOff={x:0,y:0};
  var byName={}; nodes.forEach(function(n){byName[n.name]=n;});
  // your servers are big, other people's are small: the hierarchy is readable before any label is
  var RM=34, RX=24;
  function nr(nd){return nd.managed?RM:RX;}
  function sv(n,a){var x=document.createElementNS(NS,n);for(var k in a)x.setAttribute(k,a[k]);return x;}
  function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
  function clamp(v,lo,hi){return v<lo?lo:(v>hi?hi:v);}
  function cut(s,n){s=s==null?'':String(s);return s.length>n?s.slice(0,n-1)+'\\u2026':s;}
  // Three or more servers went on ONE row in 1.12.0, which put every link along the same
  // horizontal band: the lines overlapped and so did their labels. Three or more now sit on a
  // RING, so each link gets its own angle and its own clear air. Two still sit side by side.
  // Each node carries three stacked lines under it, hence the generous vertical padding.
  var TOP=104, LBL=64;
  function layout(){
    // a topology needs a minimum canvas: below this the ring collapses and labels land on each
    // other, so the map keeps its width and the wrapper scrolls rather than degrading
    W=Math.max(el.clientWidth||900,760);
    var man=nodes.filter(function(n){return n.managed;}), ext=nodes.filter(function(n){return !n.managed;});
    function row(arr,y){var n=arr.length||1;arr.forEach(function(nd,i){nd.x=(i+1)*(W/(n+1));nd.y=y;});}
    var below;
    nodes.forEach(function(nd){nd.lblUp=false;});
    if(man.length<=2){
      row(man,TOP+RM); below=TOP+RM+RM+LBL;
    } else {
      var rr=clamp(W*0.30,150,240), cx=W/2, cy=TOP+RM+rr;
      man.forEach(function(nd,i){
        var a=-Math.PI/2+i*2*Math.PI/man.length;
        nd.x=cx+rr*Math.cos(a); nd.y=cy+rr*Math.sin(a);
        // labels go on the OUTSIDE of the ring. Every link leaves a node towards the middle,
        // so a label on the inside is the one thing guaranteed to be crossed by a line.
        nd.lblUp = nd.y < cy-4;
      });
      below=cy+rr+RM+LBL;
    }
    row(ext,below+RX+10);
    H = (ext.length ? below+RX*2+LBL+20 : below+20);
    // saved positions win over the computed layout; the canvas grows to keep them on it
    nodes.forEach(function(nd){
      if(typeof nd.px==='number'&&typeof nd.py==='number'){
        nd.x=clamp(nd.px,nr(nd)+6,Math.max(W-nr(nd)-6,nr(nd)+6));nd.y=Math.max(nd.py,nr(nd)+6);}
    });
    var maxY=0; nodes.forEach(function(nd){if(nd.y>maxY)maxY=nd.y;});
    H=Math.max(H,maxY+RM+LBL);
  }
  function nodeAt(x,y){for(var i=0;i<nodes.length;i++){var nd=nodes[i],dx=x-nd.x,dy=y-nd.y,
    r=nr(nd)+12;if(dx*dx+dy*dy<=r*r)return nd;}return null;}
  function handleXY(nd){return {x:nd.x+nr(nd)+HR+4, y:nd.y-nr(nd)+4};}
  function handleAt(x,y){
    if(!canConnect) return null;
    for(var i=0;i<nodes.length;i++){var nd=nodes[i];if(!nd.managed)continue;
      var h=handleXY(nd),dx=x-h.x,dy=y-h.y;if(dx*dx+dy*dy<=(HR+7)*(HR+7))return nd;}
    return null;
  }
  function pt(ev){var r=el.getBoundingClientRect();return {x:ev.clientX-r.left,y:ev.clientY-r.top};}
  // one word for the group state, the same word the table and the popup use
  function groupChip(p){
    if(p.group_state==='shared') return {t:p.group,c:'ok'};
    if(p.group_state==='our-side') return {t:p.group+' (one side)',c:'warn'};
    if(p.group_state==='one-side') return {t:p.group+' (one side)',c:'bad'};
    if(p.group_state==='mismatch') return {t:'groups differ',c:'bad'};
    return {t:'no group',c:'bad'};
  }
  function drawEdges(){
    var labels=[];
    pairs.forEach(function(p){
      var a=byName[p.a], b=byName[p.b]; if(!a||!b) return;
      var dx=b.x-a.x, dy=b.y-a.y, d=Math.sqrt(dx*dx+dy*dy)||1, ux=dx/d, uy=dy/d;
      var head=8, sa=nr(a), sb=nr(b);
      var startArrow=(p.dir==='b'||p.dir==='both'), endArrow=(p.dir==='a'||p.dir==='both');
      var x1=a.x+ux*(sa+(startArrow?head:2)), y1=a.y+uy*(sa+(startArrow?head:2)),
          x2=b.x-ux*(sb+(endArrow?head:2)), y2=b.y-uy*(sb+(endArrow?head:2));
      var attrs={x1:x1,y1:y1,x2:x2,y2:y2,'class':'fedge'+(p.live?' live':'')+(p.dir==='none'?' wire':'')};
      if(startArrow) attrs['marker-start']='url(#fedarrow)';
      if(endArrow) attrs['marker-end']='url(#fedarrow)';
      var ln=sv('line',attrs); linkLayer.appendChild(ln);
      var ttl=sv('title',{}); ttl.textContent=p.a_short+' \\u2194 '+p.b_short;
      ln.appendChild(ttl);
      if(d<120) return;                    // too short to label without sitting on a circle
      // Only the GROUP goes on the line. Live-versus-configured is already in the line style,
      // and two facts per label is what turns a busy map back into mush.
      var nx=-uy, ny=ux, mx=(x1+x2)/2+nx*11, my=(y1+y2)/2+ny*11;
      labels.push({x:mx,y:my,chip:groupChip(p)});
    });
    // nudge labels that would land on each other; two overlapping labels is what made the old
    // map unreadable, and a pair-per-line does not fix it on its own when nodes sit close
    labels.forEach(function(L,i){
      for(var j=0;j<i;j++){var M=labels[j];
        if(Math.abs(L.x-M.x)<95&&Math.abs(L.y-M.y)<15){L.y=M.y+16;}}
    });
    labels.forEach(function(L){
      var t=sv('text',{x:L.x,y:L.y,'class':'fedge-t fedge-g '+L.chip.c,'text-anchor':'middle'});
      t.textContent=L.chip.t; linkLayer.appendChild(t);
    });
  }
  function drawNodes(){
    nodes.forEach(function(nd){
      var r=nr(nd);
      var g=sv('g',{'class':'fnode'+(nd.managed?' managed':' unmanaged')+(nd.reachable?'':' off')+(nd.unrecorded?' unrec':''),tabindex:0,role:'button','aria-label':nd.label});
      g.appendChild(sv('circle',{cx:nd.x,cy:nd.y,r:r,'class':'fnode-c'+(nd.fed_on?' on':'')+(nd.managed?'':' ext')}));
      // inside the circle: the one number that matters, never the name - a name longer than
      // about eight characters does not fit in 68px and truncating it makes it a riddle
      if(nd.managed&&nd.fed_on){
        var c=sv('text',{x:nd.x,y:nd.y+8,'class':'fnode-n','text-anchor':'middle'});
        c.textContent=nd.federates; g.appendChild(c);
      } else {
        var w=sv('text',{x:nd.x,y:nd.y+4,'class':'fnode-w','text-anchor':'middle'});
        w.textContent=nd.managed?'off':'\\u2022'; g.appendChild(w);
      }
      // the name goes OUTSIDE the circle, where there is horizontal room for it, on whichever
      // side of the node has clear air; two lines, never three, so nodes stay compact
      var ny1=nd.lblUp?(nd.y-r-24):(nd.y+r+17), ny2=nd.lblUp?(nd.y-r-10):(nd.y+r+32);
      var nm=sv('text',{x:nd.x,y:ny1,'class':'fnode-t','text-anchor':'middle'});
      nm.textContent=cut(nd.short||nd.label,20); g.appendChild(nm);
      var full=sv('title',{}); full.textContent=nd.label+(nd.address?(' \\u00b7 '+nd.address):'');
      g.appendChild(full);
      var st=sv('text',{x:nd.x,y:ny2,'class':'fnode-s','text-anchor':'middle'});
      var s1=sv('tspan',{});
      s1.textContent=nd.managed?(nd.fed_on?'federation on':'standalone')
                               :(nd.unrecorded?'external federate':'partner');
      st.appendChild(s1);
      if(nd.managed&&nd.fed_on){
        var s2=sv('tspan',{'class':'fnode-g'+(nd.group?'':' none')});
        s2.textContent=nd.group?(' \\u00b7 shares '+cut(nd.group,16)):' \\u00b7 no group';
        st.appendChild(s2);
      }
      g.appendChild(st);
      if(nd.managed&&canConnect){
        var h=handleXY(nd);
        var hg=sv('g',{'class':'fedhandle','aria-label':'Drag to federate '+(nd.short||nd.name)});
        hg.appendChild(sv('circle',{cx:h.x,cy:h.y,r:HR,'class':'fedhandle-c'}));
        var ht=sv('text',{x:h.x,y:h.y+3.5,'class':'fedhandle-t','text-anchor':'middle'});ht.textContent='\\u26A1';hg.appendChild(ht);
        var title=sv('title',{});title.textContent='Drag this handle onto another server to federate';hg.appendChild(title);
        g.appendChild(hg);
      }
      nodeLayer.appendChild(g);
    });
  }
  function draw(){
    el.setAttribute('viewBox','0 0 '+W+' '+H);el.setAttribute('height',H);
    while(el.firstChild)el.removeChild(el.firstChild);
    var defs=sv('defs',{});
    defs.innerHTML='<marker id=fedarrow viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class=fa-h/></marker>';
    el.appendChild(defs);
    linkLayer=sv('g',{});nodeLayer=sv('g',{});el.appendChild(linkLayer);el.appendChild(nodeLayer);
    drawEdges(); drawNodes();
  }
  // Two drags, two meanings. The connect HANDLE starts a link drag; the node BODY moves the
  // node (and a click, no movement, opens its popup). 1.10's single gesture conflated them -
  // the map looked locked because dragging a node only ever drew a line.
  el.addEventListener('pointerdown',function(ev){
    var p=pt(ev),h=handleAt(p.x,p.y);
    if(h){ev.preventDefault();moved=false;dragFrom=h;
      var hp=handleXY(h);
      tempLine=sv('line',{x1:hp.x,y1:hp.y,x2:p.x,y2:p.y,'class':'fedge drag'});linkLayer.appendChild(tempLine);
      try{el.setPointerCapture(ev.pointerId);}catch(e){} return;}
    var nd=nodeAt(p.x,p.y);if(!nd)return;ev.preventDefault();
    moved=false;moveNode=nd;moveOff={x:p.x-nd.x,y:p.y-nd.y};
    try{el.setPointerCapture(ev.pointerId);}catch(e){}
  });
  el.addEventListener('pointermove',function(ev){
    var p=pt(ev);
    if(dragFrom){var d=Math.abs(p.x-dragFrom.x)+Math.abs(p.y-dragFrom.y);if(d>8)moved=true;
      if(tempLine){tempLine.setAttribute('x2',p.x);tempLine.setAttribute('y2',p.y);}return;}
    if(moveNode){var d2=Math.abs(p.x-moveOff.x-moveNode.x)+Math.abs(p.y-moveOff.y-moveNode.y);
      if(d2>4)moved=true;
      if(moved){var r=nr(moveNode);
        moveNode.x=clamp(p.x-moveOff.x,r+6,W-r-6);moveNode.y=clamp(p.y-moveOff.y,r+6,10000);
        if(moveNode.y>H-r-70){H=moveNode.y+r+70;el.setAttribute('viewBox','0 0 '+W+' '+H);el.setAttribute('height',H);}
        draw();}}
  });
  el.addEventListener('pointerup',function(ev){
    var p=pt(ev);
    if(dragFrom){
      var to=nodeAt(p.x,p.y),from=dragFrom;
      if(!to){ // forgive a near miss: snap to the closest node within reach
        var best=null,bd=70*70;
        nodes.forEach(function(n){if(n===from)return;
          var dx=n.x-p.x,dy=n.y-p.y,d=dx*dx+dy*dy;if(d<bd){bd=d;best=n;}});
        to=best;
      }
      if(tempLine){tempLine.remove();tempLine=null;}dragFrom=null;
      if(!moved||to===from){nodePop(from);return;}       // a click on the handle: show the node
      linkPop(from,(to&&to!==from)?to:null);              // dropped on a node, or empty space
      return;
    }
    if(moveNode){
      var nd=moveNode;moveNode=null;
      if(!moved){nodePop(nd);return;}                     // a click on the node body
      draw();
      fetch('/api/fedmap/pos',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:nd.name,x:Math.round(nd.x),y:Math.round(nd.y)})})
        .catch(function(){});                             // a lost save costs a layout, not a link
    }
  });
  // ---- popup ----
  var pop=document.getElementById('fedpop');
  function closePop(){pop.hidden=true;pop.innerHTML='';}
  function shell(title,body){
    pop.innerHTML='<div class=fedpop-back></div><div class=fedpop-card role=dialog aria-modal=true>'
      +'<div class=fedpop-h><h3>'+esc(title)+'</h3><button class=fedpop-x aria-label=Close>&times;</button></div>'
      +body+'<div class="a-res fedpop-res" role=status></div></div>';
    pop.hidden=false;
    pop.querySelector('.fedpop-x').onclick=closePop; pop.querySelector('.fedpop-back').onclick=closePop;
    return pop.querySelector('.fedpop-res');
  }
  function post(url,body,res,okmsg){
    res.className='a-res fedpop-res';res.textContent='Working\\u2026';
    fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
      .then(function(x){if(x.code===200){res.className='a-res ok fedpop-res';res.textContent=(okmsg||x.j.message||'Done.')+' Reloading the map\\u2026';setTimeout(function(){location.reload();},1500);}
        else{res.className='a-res error fedpop-res';res.textContent='Did not work: '+(x.j.error||x.j.message||'unknown error');}})
      .catch(function(e){res.className='a-res error fedpop-res';res.textContent='Could not reach the console. '+e;});
  }
  function enableBtn(nd,res){return function(){post('/api/action/federation-enable',{target:nd.name,inputs:{},confirm:true},res,'Federation enabled on '+nd.short+'.');};}
  function pairsOf(name){return pairs.filter(function(p){return p.a===name||p.b===name;});}
  function other(p,name){return p.a===name?p.b_short:p.a_short;}
  function dirWord(p,name){
    if(p.dir==='none') return 'seen on the wire';
    if(p.dir==='both') return 'both dialled';
    var iDialled=(p.dir==='a'&&p.a===name)||(p.dir==='b'&&p.b===name);
    return iDialled?'this server dialled':'they dialled in';
  }
  function nodePop(nd){
    if(nd.managed && !nd.fed_on){
      if(!canEnable){shell(nd.short,'<div class=fedpop-b>Federation is off here, and the enable action is not available.</div>');return;}
      var res=shell('Enable federation \\u00b7 '+nd.short,
        '<div class=fedpop-b>Turn on the federation server on <b>'+esc(nd.short)+'</b>. It opens the federation port and restarts once. After that, drag a link from it to a partner to federate.</div>'
        +'<div class=fedpop-act><button class="a-go confirm" id=fpgo>Enable federation</button></div>');
      pop.querySelector('#fpgo').onclick=enableBtn(nd,res); return;
    }
    var mine=pairsOf(nd.name);
    var body='<div class=fedpop-b>';
    if(nd.managed){
      body+='Federation is <b>on</b>, with <b>'+nd.federates+'</b> federate(s) connected right now. '
           +(nd.group?('This server shares group <b>'+esc(nd.group)+'</b> with its federation partners.')
                     :('<b>No group is shared</b> from this server, so its links carry nothing until you set one.'))
           +' Drag the \\u26A1 handle onto a partner to add a link.';
    } else {
      body+='<b>'+esc(nd.short)+'</b> is a partner you do not manage'
           +(nd.address?(' at <span class=mono>'+esc(nd.address)+'</span>'):'')
           +'. The console can see the link to it, not inside it.';
    }
    body+='</div>';
    if(nd.managed&&canGroups){
      body+='<label class=fl>Share a group across federation<input id=fpgrp value="'+esc(nd.group||'MilUX')+'" pattern="[A-Za-z0-9_-]{1,40}" required>'
        +'<span class=hint>Written into every federate entry on this box. Run it on the OTHER side too with the same name, or nothing crosses.</span></label>'
        +'<div class=fedpop-act><button class="a-go confirm" id=fpgrpgo>Share this group</button></div>';
    }
    if(mine.length){
      body+='<div class=fedlinks-h>Federated with</div><div class=fedlinks>';
      mine.forEach(function(p){
        var chip=groupChip(p);
        body+='<div class=fedlink-row><span><b>'+esc(other(p,nd.name))+'</b> '
          +'<span class=fedtbl-ar>'+(p.dir==='both'?'&harr;':p.dir==='none'?'&mdash;':'&rarr;')+'</span> '
          +'<span class=mono>'+esc(dirWord(p,nd.name))+'</span> '
          +(p.live?'<span class=fl-live>live</span>':'<span class=fl-cfg>configured</span>')
          +' <span class="fl-chip '+chip.c+'">'+esc(chip.t)+'</span></span>';
        var ours=(p.links||[]).filter(function(l){return l.from===nd.name;});
        if(ours.length){body+='<button class=fedforget data-a="'+esc(ours[0].address)+'" data-p="'+esc(ours[0].port)+'">Forget</button>';}
        body+='</div>';});
      body+='</div><div class=fedpop-note>Forget removes a link from this map. It does not un-federate the servers.</div>';
    }
    var res=shell(nd.short, body);
    if(nd.managed&&canGroups){
      pop.querySelector('#fpgrpgo').onclick=function(){
        var gv=pop.querySelector('#fpgrp').value.trim();
        if(!/^[A-Za-z0-9_-]{1,40}$/.test(gv)){res.className='a-res error fedpop-res';res.textContent='A group name is letters, numbers, dash and underscore, with no spaces.';return;}
        post('/api/action/federation-groups',{target:nd.name,inputs:{group:gv},confirm:true},res,'Group '+gv+' shared from '+nd.short+'.');
      };
    }
    pop.querySelectorAll('.fedforget').forEach(function(b){b.onclick=function(){post('/api/fedlink/forget',{source:nd.name,address:b.dataset.a,port:b.dataset.p},res,'Removed from the map.');};});
  }
  function validName(s){var v=(s||'partner').replace(/[^A-Za-z0-9._-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,48);return v||'partner';}
  function linkPop(from,to){
    if(!from.fed_on){
      var res0=shell('Turn on '+from.short+' first',
        '<div class=fedpop-b><b>'+esc(from.short)+'</b> has federation switched off, so it cannot hold a link yet. Turn it on (one restart), then drag again'+(to?(' onto <b>'+esc(to.short)+'</b>'):'')+' to federate.</div>'
        +(canEnable?'<div class=fedpop-act><button class="a-go confirm" id=fpgo>Turn on federation on '+esc(from.short)+'</button></div>':'<div class=fedpop-note>The enable action is not available here.</div>'));
      if(canEnable) pop.querySelector('#fpgo').onclick=enableBtn(from,res0);
      return;
    }
    // already federated? say so rather than quietly offering a second, redundant link
    if(to){
      var ex=pairs.filter(function(p){return (p.a===from.name&&p.b===to.name)||(p.b===from.name&&p.a===to.name);})[0];
      if(ex&&((ex.dir==='a'&&ex.a===from.name)||(ex.dir==='b'&&ex.b===from.name)||ex.dir==='both')){
        shell(from.short+' \\u2192 '+to.short+' is already linked',
          '<div class=fedpop-b><b>'+esc(from.short)+'</b> already has a link to <b>'+esc(to.short)+'</b>, so there is nothing to add. '
          +'One link is enough: federation is not one-way, and events cross both ways once both sides share a group.</div>'
          +'<div class=fedpop-note>'+esc(groupLong(ex))+'</div>');
        return;
      }
    }
    var estate=!!(to&&to.managed), nm=validName(to?(to.short||to.name):''), addr=to?(to.address||''):'';
    var intro, known='';
    if(estate){
      intro='Federate <b>'+esc(from.short)+'</b> with <b>'+esc(to.short)+'</b>. Both are servers you manage, so the console already knows where '+esc(to.short)+' is - there is nothing for you to look up.';
      known='<div class=fedpop-known>The console filled these in from your estate. You can just confirm.</div>';
    } else {
      intro='Federate <b>'+esc(from.short)+'</b> with a partner you do not manage. Enter where their TAK server is - you get this from whoever runs it.';
    }
    var res=shell('Federate '+from.short+' \\u2192 '+(to?esc(to.short):'a new partner'),
      '<div class=fedpop-b>'+intro+'</div>'+known
      +'<label class=fl>Name for this link<input id=fpname value="'+esc(nm)+'" pattern="[A-Za-z0-9._-]{1,48}" required>'
        +'<span class=hint>A label for the link. Letters, numbers, dot, dash, underscore - no spaces.</span></label>'
      +'<label class=fl>Partner address<input id=fpaddr value="'+esc(addr)+'" pattern="[a-z0-9.-]{1,253}" placeholder="e.g. tak.partner.example" required>'
        +'<span class=hint>'+(estate?('Already set from '+esc(to.short)+'; leave it as it is.'):'The other server&#39;s hostname or IP.')+'</span></label>'
      +'<label class=fl>Port<input id=fpport value="9001" pattern="[0-9]{2,5}" required>'
        +'<span class=hint>The federation port. Almost always 9001.</span></label>'
      +'<div class=fedpop-note>This adds the link on '+esc(from.short)+' and restarts it once. Only this side needs it - one arrow is a complete link. For traffic to flow, the partner also needs federation on, both sides must trust each other&#39;s certificate, and <b>both must share the same group</b>.</div>'
      +'<div class=fedpop-act><button class="a-go confirm" id=fpgo>Confirm and federate</button></div>');
    pop.querySelector('#fpgo').onclick=function(){
      var name=pop.querySelector('#fpname').value.trim(),a=pop.querySelector('#fpaddr').value.trim(),port=pop.querySelector('#fpport').value.trim();
      if(!/^[A-Za-z0-9._-]{1,48}$/.test(name)){res.className='a-res error fedpop-res';res.textContent='The link name can use letters, numbers, dot, dash and underscore, with no spaces.';return;}
      if(!/^[a-z0-9.-]{1,253}$/.test(a)){res.className='a-res error fedpop-res';res.textContent='The partner address should be a hostname or IP, e.g. tak.partner.example.';return;}
      if(!/^[0-9]{2,5}$/.test(port)){res.className='a-res error fedpop-res';res.textContent='The port should be digits, almost always 9001.';return;}
      post('/api/action/federation-connect',{target:from.name,inputs:{displayName:name,address:a,port:port},confirm:true},res,'Link added: '+from.short+' \\u2192 '+name+'.');
    };
  }
  function groupLong(p){
    if(p.group_state==='shared') return 'Both servers share group '+p.group+', so events in it cross this link.';
    if(p.group_state==='our-side') return 'This console set group '+p.group+' on '+p.group_side+'. The partner has to share the same group at their end.';
    if(p.group_state==='one-side') return 'Group '+p.group+' is set on '+p.group_side+' only, so nothing crosses yet. Run Share federation group on the other side too.';
    if(p.group_state==='mismatch') return 'The two servers share different groups, so nothing crosses. Both need the same group name.';
    return 'No group is shared, so this link is connected and carries nothing. Run Share federation group on both sides.';
  }
  document.addEventListener('keydown',function(e){if(e.key==='Escape')closePop();});
  layout();draw();
  window.addEventListener('resize',function(){layout();draw();});
})();
"""


# The favicon: a bold M drawn FOR 16px, Olive Gold on the Deep Green that is the
# console's own header surface. Not the wordmark - its M/X ligature is cover art
# and turns to mush at tab size (tried, rendered, rejected at 32px). Served from
# memory as SVG; no file on disk, no external fetch.
# The Vantage mark: an observer's station (the gold dot), a field of view opening
# upward (the V), a centre sightline, and the overlooked horizon (the arc). MilUX
# palette only. The same geometry serves as favicon (on the deep-green tile) and as
# the header mark (transparent).
VANTAGE_GEOMETRY = (
    # the viewpoint: high ground, with the OS-map sight-rays from the summit.
    # Solid family geometry - deep-green/light body, one ochre gesture (like Tally's
    # strike and Foothold's ledge).
    '<polygon points="8,88 38,40 54,62 68,44 92,88" fill="#F7F6EB"/>'
    '<g fill="#A35C17"><polygon points="38,40 34,12 42,13"/>'
    '<polygon points="38,40 16,20 22,15"/>'
    '<polygon points="38,40 58,14 52,11"/></g>')

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<rect width="100" height="100" rx="18" fill="#113308"/>'
    + VANTAGE_GEOMETRY + '</svg>')


def favicon_svg(inst=None):
    """The tab icon in the instance palette, on the instance's band colour."""
    inst = inst or load_instance()
    e = html.escape
    warm = inst.get("accent_warm", "#A35C17")
    geo = (f'<polygon points="10,86 38,42 54,62 68,46 90,86" fill="{e(inst["ink"])}"/>'
           f'<g fill="{e(warm)}"><polygon points="38,42 33,12 43,13"/>'
           '<polygon points="38,42 14,20 21,14"/>'
           '<polygon points="38,42 60,13 53,10"/></g>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            f'<rect width="100" height="100" rx="18" fill="{e(inst["band"])}"/>{geo}</svg>')


VANTAGE_MARK = ('<svg class=mark viewBox="0 0 100 100" role=img aria-label="Vantage">'
                + VANTAGE_GEOMETRY + '</svg>')

WORDMARK = '<svg class=mark viewBox="25 25 250 150" role=img aria-label="MilUX"><polygon points="114.98 92.32 100.24 92.37 90.53 85.05 80.58 92.38 66.02 92.32 83.05 79.5 74.01 72.72 40.56 47.69 35.92 44.32 35.92 92.36 27.04 92.35 27.04 26.58 33.03 30.98 68.13 57.26 90.45 73.98 126.44 47.06 153.76 26.6 153.76 92.35 144.88 92.36 144.88 44.31 118.91 63.74 97.82 79.52 114.98 92.32" fill="#B5B171"/><polygon points="144.88 110.87 153.76 110.83 153.75 172.75 27.05 172.75 27.04 110.84 35.68 110.84 35.69 163.88 144.87 163.87 144.88 110.87" fill="#F7F6EB"/><polygon points="273.52 172.63 258.88 172.77 224.29 147.1 189.83 172.74 175.12 172.64 216.86 141.61 175.05 110.52 189.68 110.37 224.32 136.1 259.04 110.32 273.57 110.52 231.76 141.62 273.52 172.63" fill="#F7F6EB"/><polygon points="273.24 83.72 273.28 92.36 205.61 92.35 205.6 26.36 214.24 26.36 214.25 83.71 273.24 83.72" fill="#B5B171"/><rect x="174.88" y="26.36" width="8.88" height="66" fill="#B5B171"/></svg>'


def estate_summary(state, ev, stale, age):
    tgts = state.get("targets", [])
    n = len(tgts)
    if stale:
        return f"Last checked {human_age(age)}. Data may be wrong."
    bad = [t for t in tgts if t.get("result") != "OK"]
    if not bad:
        return f"All {n} servers healthy." if n else "No servers configured."
    names = ", ".join(t.get("label", t.get("name", "?")) for t in bad)
    return f"{len(bad)} of {n} need attention: {names}."


# ---------- pages (1.2.0: the portal) ----------------------------------------------------------
# Three page types instead of one screen. "/" is the estate: fleet verdict, tiles, drift,
# baseline. "/server/<name>" is one box: its health, its inventory, and its actions
# pre-bound to it (no picker) and filtered to what the inventory says the box runs.
# "/operations" is the full catalogue plus the audit log, surfaced at last. Still
# server-rendered, still stdlib, still one file; the kit serves this offline unchanged.

def load_audit(limit=60):
    """Newest-first slice of the audit log. This process writes it; absent is empty."""
    try:
        with open(AUDIT) as fh:
            lines = fh.read().splitlines()
    except Exception:
        return []
    out = []
    for raw in reversed(lines[-limit * 3:]):
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def action_applies(aid, t):
    """Inventory-driven placement: an action appears on a server page only when the box
    runs the component the action manages. A box with no inventory (old checker) shows
    everything, because hiding tools on missing data would strand the operator."""
    need = ACTIONS[aid].get("needs")
    if not need:
        return True
    sw = t.get("software") or []
    if not sw:
        return True
    return any(s.get("name") == need for s in sw)


# The theme control: light palette is defined on bare :root, dark under prefers-color-scheme
# AND under [data-theme=dark], light forced by [data-theme=light]. So three states - system,
# light, dark - all the CSS already understands. This script applies the saved choice before
# first paint (no flash) and cycles it; the button lives in the header on every page.
THEME_HEAD = (
    "<script>(function(){try{var t=localStorage.getItem('vtheme');"
    "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);}catch(e){}"
    "function glyph(t){return t==='light'?'\\u2600':t==='dark'?'\\u263e':'\\u25d0';}"
    "window.cycleTheme=function(){var r=document.documentElement,"
    "cur=r.getAttribute('data-theme')||'system',"
    "next=cur==='system'?'light':cur==='light'?'dark':'system';"
    "if(next==='system'){r.removeAttribute('data-theme');try{localStorage.removeItem('vtheme');}catch(e){}}"
    "else{r.setAttribute('data-theme',next);try{localStorage.setItem('vtheme',next);}catch(e){}}"
    "var b=document.getElementById('themebtn');if(b){b.textContent=glyph(next);"
    "b.title='Theme: '+next+' (click to change)';}};"
    "document.addEventListener('DOMContentLoaded',function(){var b=document.getElementById('themebtn');"
    "if(b){var t=r0();b.textContent=glyph(t);b.title='Theme: '+t+' (click to change)';}"
    "function r0(){try{return localStorage.getItem('vtheme')||'system';}catch(e){return 'system';}}});"
    "})();</script>")


def brand_mark(inst=None, on="dark"):
    """The product mark, surface-aware like the family's asset pairs: light body on the
    dark header band, deep-green body on paper (welcome, sign-in). The rays stay warm
    on both - one gesture, both grounds."""
    inst = inst or load_instance()
    e = html.escape
    warm = inst.get("accent_warm", "#A35C17")
    body = inst["ink"] if on == "dark" else inst["band"]
    geo = (f'<polygon points="8,88 38,40 54,62 68,44 92,88" fill="{e(body)}"/>'
           f'<g fill="{e(warm)}"><polygon points="38,40 34,12 42,13"/>'
           '<polygon points="38,40 16,20 22,15"/>'
           '<polygon points="38,40 58,14 52,11"/></g>')
    return (f'<svg class=mark viewBox="0 0 100 100" role=img '
            f'aria-label="{e(inst["product_name"])}">{geo}</svg>')


def page_head(title, inst=None):
    inst = inst or load_instance()
    e = html.escape
    accent = ""
    # only emit an override when the instance actually diverges from the built-in gold,
    # so a stock Vantage console ships the exact CSS it always did. The token is redefined
    # per theme, so an override must win in all three states.
    if inst["accent"].lower() != INSTANCE_DEFAULTS["accent"].lower() \
       or inst["accent_2"].lower() != INSTANCE_DEFAULTS["accent_2"].lower():
        a, a2 = e(inst["accent"]), e(inst["accent_2"])
        decl = f"--gold:{a};--gold-light:{a2};--acc:{a};--focus:{a}"
        accent = (f":root{{{decl}}}"
                  f"@media(prefers-color-scheme:dark){{:root:not([data-theme=\"light\"]){{{decl}}}}}"
                  f":root[data-theme=\"dark\"]{{{decl}}}")
    stacks = FONT_STACKS.get(inst.get("font", "standard"))
    if stacks:
        accent += f":root{{--font-display:{stacks[0]};--font-sans:{stacks[1]}}}"
    return ["<!doctype html><html lang=en><head><meta charset=utf-8>",
            "<meta name=viewport content='width=device-width,initial-scale=1'>",
            f"<title>{e(title)}</title>",
            "<link rel=icon type=image/svg+xml href=/favicon.svg?v=4>",
            THEME_HEAD,
            f"<style>{CSS}</style>" + (f"<style>{accent}</style>" if accent else ""),
            "</head><body>",
            "<a class=skip href='#main'>Skip to content</a>"]


EDITION = os.environ.get("VANTAGE_CONSOLE_EDITION", "estate")   # "estate" | "deployed"


def nav_html(state, active, inst=None):
    """Two fixed items, whatever the fleet size - a tab per server dies at server
    seven. The tile grid is the server index; server pages carry a jump select.
    The deployed edition (a single box's own console) carries only the surfaces that
    make sense for one box: Overview, Operations, Store, Vault. No fleet federation,
    no deploy-a-new-box, no estate agent."""
    e = html.escape
    inst = inst or load_instance()
    agent_name = inst["agent_name"]
    if EDITION == "deployed":
        items = [("/", "Overview", active == "estate", "Overview"),
                 ("/operations", "Operations", active == "operations", "Operations"),
                 ("/store", "File store", active == "store", "File store"),
                 ("/vault", "Knowledge Vault", active == "vault", "Knowledge Vault")]
        links = "".join(
            f"<a href='{e(h)}'{' class=on aria-current=page' if on else ''}>{lbl}</a>"
            for h, lbl, on, _p in items)
        return f"<nav class=topnav aria-label='Console sections'>{links}</nav>"
    pend = sum(1 for p in load_proposals() if p.get("status") == "pending")
    items = [("/", "Overview", active == "estate", "Overview"),
             ("/federation", "Federation", active == "federation", "Federation"),
             ("/networks", "Networks", active == "networks", "Networks"),
             ("/operations", "Operations", active == "operations", "Operations"),
             ("/store", "File store", active == "store", "File store"),
             ("/vault", "Knowledge Vault", active == "vault", "Knowledge Vault")]
    items.append(("/sync", "Sync", active == "sync", "Sync"))
    if inst.get("console_mode", "admin") != "client":
        items.append(("/deploy", "Deploy", active == "deploy", "Deploy"))
    if inst["agent_enabled"]:
        # one tab for the agent: chat is the front door, its pending proposals ride on
        # the same tab as a badge and are surfaced on the page itself (Agent + Sam merged)
        albl = ("AI Connections<span class=navbadge>" + str(pend) + "</span>"
                if pend else "AI Connections")
        items.append(("/agent", albl, active in ("chat", "agent"), "AI Connections"))
    # Customize is not a section - it is settings, reached from the gear, top right
    links = "".join(
        f"<a href='{e(h)}'{' class=on aria-current=page' if on else ''}>{lbl}</a>"
        for h, lbl, on, _plain in items)
    jump = ""
    if isinstance(active, str) and active.startswith("server:"):
        cur = active.split(":", 1)[1]
        opts = "".join(
            f"<option value='/server/{e(t.get('name', ''))}'"
            f"{' selected' if t.get('name') == cur else ''}>"
            f"{e(t.get('label', t.get('name', '?')))}</option>"
            for t in (state or {}).get("targets", []))
        jump = ("<label class=navjump><span class=sr-only>Switch server</span>"
                f"<select onchange='location.href=this.value'>{opts}</select></label>")
    return f"<nav class=topnav aria-label='Console sections'>{links}{jump}</nav>"


def header_html(state, ev, age, active, crumb="Overview"):
    e = html.escape
    inst = load_instance()
    return ("<header><div class=head>" + brand_mark(inst)
            + f"<span class=brand>{e(inst['product_name'])}</span>"
            + f"<h1 class=eyebrow-h>{crumb}</h1>"
            f"<span class='verdict v-{e(ev)}'><span class=dot></span>{e(ev)}</span>"
            "<div class=spacer></div>"
            f"<div class=checked><span title='{e(state.get('generated_at', ''))}'>"
            f"Checked <b>{e(human_age(age))}</b></span></div>"
            + ("<a id=gearbtn class=themebtn href='/customization' title='Customize this "
               "console' aria-label='Customize'"
               + (" aria-current=page" if active == "customization" else "")
               + ">⚙</a>" if EDITION != "deployed" else "")
            + ("<a id=outbtn class=themebtn href='/logout' title='Sign out' "
               "aria-label='Sign out'>"
               "<svg viewBox='0 0 24 24' width=15 height=15 fill=none stroke=currentColor "
               "stroke-width=2.2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
               "<path d='M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4'/>"
               "<path d='M16 17l5-5-5-5'/><path d='M21 12H9'/></svg>"
               "</a>" if auth_configured() else "")
            + "<button id=themebtn class=themebtn type=button onclick=cycleTheme() "
            "aria-label='Toggle light or dark theme'>◐</button>"
            "</div>" + nav_html(state, active, inst) + "</header>")


def footer_html(state, acts):
    e = html.escape
    inst = load_instance()
    maker = (f" &middot; a {e(inst['maker'])} product" if inst["maker"] else "")
    # the maker's mark, bottom-right of every screen; only when this is a MilUX console,
    # so a white-labelled install never carries someone else's logo
    logo = (f"<span class=foot-logo title='MilUX'>{WORDMARK}</span>"
            if inst["maker"] == "MilUX" else "")
    out = ["<footer><div class=foot-t>"]
    if state.get("blind_spot"):
        out.append(e(state["blind_spot"]) + "<br>")
    out.append("The board is read-only. " + ("Actions are gated and logged. " if acts else
               "No actions enabled (read-only). ")
               + f"Machine-readable at <code>/api/health.json</code>. &middot; "
                 f"{e(inst['product_name'])} {VERSION}{maker}")
    out.append(f"</div>{logo}</footer></main>")
    if acts:
        out.append(f"<script>{ACTION_JS}</script>")
        out.append(f"<script>{UPGRADE_PICK_JS}</script>")
    out.append("</body></html>")
    return "".join(out)


def stale_banner(age, stale):
    if not stale:
        return ""
    return ("<div class='banner stale'><b>Stale.</b><span>Last refreshed "
            f"{html.escape(human_age(age))}. Data may be wrong; the collector may have "
            "stopped.</span></div>")


def library_deb_for(version):
    """Newest shelf package whose filename carries the wanted version token, so the
    upgrade form arrives filled with the baseline package instead of asking for a
    filename and a 64-character hash by hand."""
    try:
        for pkg in sorted(library_list(), key=lambda x: x.get("uploaded", ""), reverse=True):
            if version and version in pkg.get("file", ""):
                return pkg
    except Exception:
        pass
    return None


def library_image_for(module, version=""):
    """Newest image tarball in the library for a module, matching the baseline version if
    one is given (else the newest for that module). Feeds the offline module deploy."""
    try:
        imgs = [e for e in library_list()
                if e.get("kind") == "image" and e.get("module") == module]
        imgs.sort(key=lambda x: x.get("uploaded", ""), reverse=True)
        if version:
            for e in imgs:
                if version in (e.get("version", "") or "") or version in e.get("file", ""):
                    return e
            return None
        return imgs[0] if imgs else None
    except Exception:
        return None


def target_options(cfg, state):
    """(name, label) for every enrolled target - label from the state record, sorted by label,
    so every box picker shows the operator's name (Office), not the raw id (nuc)."""
    labels = {t.get("name"): (t.get("label") or t.get("name"))
              for t in (state or {}).get("targets", [])}
    names = (cfg or {}).get("targets") or {}
    return sorted(((n, labels.get(n, n)) for n in names), key=lambda x: str(x[1]).lower())


def action_form_html(aid, targets=None, fixed=None, prefill=None):
    """One action form. With `fixed`, the target is pre-bound (a server page: hidden
    input, no picker). With `targets`, the operator chooses (the operations page). With
    `prefill`, the inputs arrive filled (an agent proposal) - the operator still reads the
    confirm and clicks; a filled field changes nothing on its own."""
    e = html.escape
    prefill = prefill or {}
    a = ACTIONS[aid]
    if aid == "upgrade-server" and not prefill:
        pkg = library_deb_for((load_desired() or {}).get("takserver", ""))
        if pkg:
            prefill = {"deb_file": pkg["file"], "deb_sha256": pkg["sha256"]}
    risk = a.get("risk", "write")
    out = [f"<form class=action id='act-{e(aid)}' data-id='{e(aid)}' "
           f"data-pass='{1 if a['needs_passphrase'] else 0}' "
           f"data-read='{1 if a.get('read') else 0}' "
           f"data-confirm=\"{e(a['confirm'])}\" data-result='{e(a['result'])}'>"
           f"<div class=a-h><h3 class=a-t>{e(a['label'])}</h3><span class=a-tags>"
           f"<span class='a-tag {e(risk)}'>{e(a.get('tag', risk))}</span>"
           + ("<span class='a-tag pass'>Passphrase</span>" if a['needs_passphrase'] else "")
           + f"</span></div><div class=a-d>{e(a['desc'])}</div>"
           # trusted registry HTML (never user input): the longer how-to under the desc
           + (f"<div class='a-d a-more'>{a['more']}</div>" if a.get("more") else "")]
    if fixed is not None:
        out.append(f"<input type=hidden name=target value='{e(fixed)}'>")
    else:
        out.append("<label class=fl>Box<select name=target required>"
                   "<option value='' selected disabled>Choose a box</option>"
                   + "".join(
                       f"<option value='{e(x[0])}'>{e(x[1])}</option>"
                       if isinstance(x, (tuple, list))
                       else f"<option>{e(x)}</option>"
                       for x in (targets or []))
                   + "</select></label>")
    for f in a["inputs"]:
        pv = prefill.get(f["name"])
        if aid == "upgrade-server" and f["name"] in ("deb_file", "deb_sha256"):
            # the Package picker below drives these; the operator never hand-types a sha256
            out.append(f"<input type=hidden name='{e(f['name'])}' class='up-{e(f['name'])}' "
                       f"value='{e(str(pv or ''))}'>")
            continue
        if f.get("hidden"):
            out.append(f"<input type=hidden name='{e(f['name'])}' "
                       f"value='{e(str(pv or ''))}'>")
            continue
        hid = f"h-{e(aid)}-{e(f['name'])}"
        val = f" value='{e(str(pv))}'" if pv not in (None, "") else ""
        itype = f" type={e(f['input_type'])}" if f.get("input_type") else ""
        ienc = f" data-encode={e(f['encode'])}" if f.get("encode") else ""
        ireq = "" if f.get("optional") else " required"
        ipat = "" if f.get("encode") else f' pattern="{e(f["pattern"])}"'
        if f.get("choices"):
            def _copt(c):
                v, lbl = (c if isinstance(c, (tuple, list)) else (c, c))
                return (f"<option value='{e(v)}'" + (" selected" if pv == v else "")
                        + f">{e(lbl)}</option>")
            opts = "".join(_copt(c) for c in f["choices"])
            out.append(f"<label class=fl>{e(f['label'])}"
                       f"<select name='{e(f['name'])}'{ireq} aria-describedby='{hid}'>"
                       f"{opts}</select>"
                       f"<span class=hint id='{hid}'>{e(f.get('help', ''))}</span></label>")
        elif f.get("kind") == "textarea":
            out.append(f"<label class=fl>{e(f['label'])}"
                       f"<textarea name='{e(f['name'])}'{ienc}{ireq} rows=8 "
                       f"spellcheck=false aria-describedby='{hid}'>"
                       f"{e(str(pv)) if pv not in (None, '') else ''}</textarea>"
                       f"<span class=hint id='{hid}'>{e(f.get('help', ''))}</span></label>")
        else:
            out.append(f"<label class=fl>{e(f['label'])}<input name='{e(f['name'])}'{itype}{ienc}"
                       f"{ipat}{val}{ireq} aria-describedby='{hid}' autocomplete=off>"
                       f"<span class=hint id='{hid}'>{e(f.get('help', ''))}</span></label>")
    if aid == "upgrade-server":
        opts = "".join(
            f"<option value='{e(en['file'])}' data-sha='{e(en.get('sha256', ''))}'>"
            f"{e(en['file'])}</option>" for en in library_manifest() if en.get("file"))
        if not opts:
            opts = "<option value=''>nothing on the shelf yet</option>"
        out.append("<label class=fl>Package"
                   f"<select class=up-pick>{opts}</select>"
                   "<span class=hint>choose from the shelf, or upload below - the "
                   "name and checksum fill themselves</span></label>"
                   "<label class=fl>Upload a release to the shelf"
                   "<input type=file class=up-file accept=.deb>"
                   "<span class='hint up-upstat'>uploads into Store &gt; tak-server, "
                   "then selects itself</span></label>")
    if a["needs_passphrase"]:
        out.append("<label class=fl>Operator password"
                   "<input name=passphrase type=password required></label>")
    out.append("<div class=a-act><button class=a-go type=submit>Run</button></div>"
               "<div class='a-res' role=status aria-live=polite aria-atomic=true></div></form>")
    return "".join(out)


# The VPN layer's providers, declared: what each needs from the operator, the
# interface it creates, and what it honestly can and cannot do. Vantage does not
# choose your VPN - it carries whichever you choose; adding a provider is a new
# entry here plus its case in tak-vpn-priv on the boxes.
VPN_PROVIDERS = {
    "wireguard": {
        "label": "WireGuard", "iface": "wg0",
        "conf_label": "wg-quick config",
        "conf_help": "the full [Interface]/[Peer] file, exactly as it would live at "
                     "/etc/wireguard/wg0.conf - keys included, which is why this is "
                     "treated as a secret",
        "notes": "no vendor, no account; the TAK community's default"},
    "tailscale": {
        "label": "Tailscale", "iface": "tailscale0",
        "conf_label": "auth key",
        "conf_help": "a tailnet auth key (tskey-auth-...). Mint it one-use and "
                     "pre-authorised in your Tailscale admin console",
        "notes": "the only provider able to publish a single public path (Funnel)"},
    "openvpn": {
        "label": "OpenVPN", "iface": "tun0",
        "conf_label": "client .ovpn",
        "conf_help": "the full client profile with inline <ca>/<cert>/<key> blocks, "
                     "as your OpenVPN server exported it",
        "notes": "the long-standing standard many TAK guides assume"},
}

ACTION_GROUPS = [("tak", "TAK server"), ("federation", "Federation"),
                 ("network", "Network"),
                 ("software", "Software & updates"), ("box", "This box")]


def actions_grouped_html(aids, targets=None, fixed=None, collapsible=False):
    """The action grid, grouped by what each action manages. Ten flat cards blur;
    labelled groups also give the next actions (OS patching, reboot) a home before
    they exist. Registry order (safe -> dangerous) holds within a group. When
    collapsible, each group is a <details> that remembers open/closed per operator, so
    a long Operations page collapses down to the groups you actually use."""
    e = html.escape
    out = []
    for gid, glabel in ACTION_GROUPS:
        here = [a for a in aids if ACTIONS[a].get("group", "box") == gid]
        if not here:
            continue
        n = len(here)
        if collapsible:
            out.append(f"<details class=ag-group id='ag-{e(gid)}' data-g='{e(gid)}'>"
                       "<summary class=ag-sum><span class=ag-chev>▸</span>"
                       f"<span class=ag-t>{e(glabel)}</span>"
                       f"<span class=ag-count>{n}</span>"
                       "<span class=ag-hint>expand</span></summary><div class=agrid>")
        else:
            out.append(f"<h3 class=ag-t>{e(glabel)}</h3><div class=agrid>")
        for aid in here:
            out.append(action_form_html(aid, targets=targets, fixed=fixed))
        out.append("</details>" if collapsible else "</div>")
    if collapsible:
        out.append(f"<script>{ACTGROUP_JS}</script>")
    return "".join(out)


ACTGROUP_JS = r"""
(function(){
  var groups=document.querySelectorAll('details.ag-group'); if(!groups.length) return;
  function key(g){return 'agopen:'+g.getAttribute('data-g');}
  groups.forEach(function(g){
    // collapsed by default; a group the operator opened last time reopens
    try{if(localStorage.getItem(key(g))==='1')g.open=true;}catch(e){}
    g.addEventListener('toggle',function(){try{localStorage.setItem(key(g),g.open?'1':'0');}catch(e){}});
  });
  // a deep link (/operations#ag-network) opens its group and scrolls to it
  if(location.hash&&location.hash.indexOf('#ag-')===0){
    var tgt=document.getElementById(location.hash.slice(1));
    if(tgt){tgt.open=true;setTimeout(function(){tgt.scrollIntoView({block:'start'});},50);}
  }
})();
"""




EUD_FOLDERS = ["software", "mission-packs", "map-packs"]


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return (f"{n:.0f} {unit}" if unit in ("B", "KB")
                    else f"{n:.1f} {unit}".replace(".0 ", " "))
        n /= 1024


def render_eud():
    """The EUD downloads page: a read-only, phone-first shelf for the software, mission
    packs and map packs a device pulls at the edge. Deliberately NOT the console: no nav,
    no actions, nothing to press but downloads. Served on the same tailnet bind - a device
    that can reach it is already inside the estate's network boundary."""
    e = html.escape
    inst = load_instance()
    ac, ac2, ink, band = inst["accent"], inst["accent_2"], inst["ink"], inst["band"]
    doc = [f"<title>{e(inst['product_name'])} downloads</title>",
           "<meta name=viewport content='width=device-width,initial-scale=1'>",
           "<style>",
           f"body{{margin:0;background:#0d2205;color:{e(ink)};font:400 16px/1.5 system-ui,sans-serif}}",
           f".hd{{display:flex;gap:12px;align-items:center;padding:18px 16px;background:{e(band)}}}",
           ".hd svg{height:34px;width:34px}",
           ".hd b{font-size:19px;letter-spacing:.01em}",
           f".hd small{{display:block;color:{e(ac)};font-size:12px}}",
           f"h2{{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:{e(ac)};",
           " margin:22px 16px 8px}",
           ".f{display:flex;justify-content:space-between;align-items:center;gap:12px;",
           f" margin:0 10px;padding:13px 10px;border-bottom:1px solid #2c4a1e;text-decoration:none;color:{e(ink)}}}",
           ".f small{color:#9fb2ba;display:block;font-size:12px;margin-top:2px}",
           f".f .dl{{flex:0 0 auto;background:{e(ac)};color:{e(band)};font-weight:700;font-size:13px;",
           " padding:8px 14px;border-radius:6px}",
           ".empty{color:#9fb2ba;margin:4px 16px;font-size:14px}",
           f".cur{{display:inline-block;margin-left:8px;background:{e(ac)};color:{e(band)};",
           " font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;",
           " padding:2px 7px;border-radius:5px;vertical-align:middle}",
           ".ft{color:#9fb2ba;font-size:12px;padding:26px 16px}",
           "</style>",
           f"<div class=hd>{brand_mark(inst)}<div><b>{e(inst['product_name'])} downloads</b>",
           "<small>software and packs for this estate's devices</small></div></div>"]
    cur = software_current_set()
    for folder in EUD_FOLDERS:
        root = store_resolve(folder, "store")
        doc.append(f"<h2>{e(folder.replace('-', ' '))}</h2>")
        rows = []
        if root and os.path.isdir(root):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for fn in sorted(filenames):
                    if fn.startswith(".") or fn in ("library.json", "library.json.tmp"):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, store_resolve("", "store"))
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    sub = os.path.dirname(os.path.relpath(full, root))
                    from urllib.parse import quote as _q
                    href = "/store/file/" + "/".join(_q(part) for part in rel.split(os.sep))
                    is_cur = folder == "software" and fn in cur
                    badge = "<span class=cur>current</span>" if is_cur else ""
                    rows.append((is_cur,
                        f"<a class=f href='{href}'><span>{e(fn)}{badge}"
                        f"<small>{e(sub) + ' · ' if sub else ''}{_human_bytes(size)}</small></span>"
                        f"<span class=dl>Download</span></a>"))
        rows.sort(key=lambda r: not r[0])          # current build first
        doc.append("".join(h for _, h in rows) if rows else
                   "<div class=empty>Nothing on this shelf yet.</div>")
    doc.append("<div class=ft>Read-only. Files are managed in the console's Store. "
               f"{e(inst['product_name'])} {VERSION}.</div>")
    return "".join(doc)


def render_customization(state):
    """White-label the console: product name, maker, agent name, palette. A customer
    stands up their own branded console from here; the values live in one instance file
    the product reads, never in code. Writes go through the root helper, like every
    other config change."""
    e = html.escape
    inst = load_instance()
    age = age_seconds(state.get("generated_at", ""))
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    doc = page_head("Customization — " + inst["product_name"], inst)
    doc.append(header_html(state, ev, age, "customization", crumb="Customization"))
    doc.append("<main id=main class=wrap>")
    doc.append("<section aria-label='Customization'><div class=ah><h2 class=title>"
               "Make this console your own</h2><span class=meta>Colours, typeface, and "
               "the defaults a new server build starts from - change them and the whole "
               "console, its downloads page and its favicon follow. AI assistant set-up "
               "lives on <a href='/agent'>AI Connections</a>.</span></div>")
    def fld(k, label, hint, maxlen=40, typ="text", ph=""):
        pha = f" placeholder='{e(ph)}'" if ph else ""
        return (f"<label class=fl>{e(label)}<input id=cz-{k} type={typ} "
                f"maxlength={maxlen} value='{e(str(inst[k]))}'{pha}>"
                f"<span class=hint>{e(hint)}</span></label>")
    def col(k, label):
        return (f"<label class=fl>{e(label)}<span class=cz-colwrap>"
                f"<input id=cz-{k} type=color value='{e(inst[k])}'>"
                f"<code>{e(inst[k])}</code></span></label>")
    doc.append("<form id=czform class=depcard>")
    doc.append("<div class=fl style='grid-column:1/-1'><span class=wz-comp-t>Design</span>"
               "<span class=hint>how the console looks, everywhere it renders</span></div>")
    _fsel = "".join(
        f"<option value={v}{' selected' if inst.get('font', 'standard') == v else ''}>{lbl}</option>"
        for v, lbl in (("standard", "Standard - the Vantage pairing"),
                       ("system", "System - this device's own typeface"),
                       ("serif", "Serif - editorial")))
    doc.append("<label class=fl>Typeface"
               f"<select id=cz-font>{_fsel}</select>"
               "<span class=hint>headings and body together; code stays monospaced</span></label>")
    doc.append(col("accent", "Accent"))
    doc.append(col("accent_2", "Accent (light)"))
    doc.append(col("ink", "Mark ink"))
    doc.append(col("band", "Band / favicon ground"))
    doc.append(col("accent_warm", "Mark rays"))
    doc.append("<div class=fl style='grid-column:1/-1'><span class=wz-comp-t>Console mode</span>"
               "<span class=hint>what this console is allowed to do to the estate</span></div>")
    _mode_now = inst.get("console_mode", "admin")
    doc.append("<label class=fl>Mode"
               f"<select id=cz-console_mode data-orig='{e(_mode_now)}'>"
               f"<option value=admin{' selected' if _mode_now == 'admin' else ''}>"
               "admin - full estate control</option>"
               f"<option value=client{' selected' if _mode_now == 'client' else ''}>"
               "client - watch and operate, never reshape</option></select>"
               "<span class=hint>client mode hides Deploy and refuses estate-shaping "
               "actions on this console; devices, files and the vault stay full. Turning "
               "the last admin into a client would leave the estate with no controller, so "
               "the change is password-protected</span></label>")
    # the mode change is guarded: it only reveals when the mode actually differs, and the
    # server re-checks the operator password before applying it
    if auth_configured():
        doc.append("<label class=fl id=cz-modepw-row hidden style='grid-column:1/-1'>"
                   "Operator password"
                   "<input type=password id=cz-modepw autocomplete=off>"
                   "<span class=hint>changing console mode needs your operator "
                   "password</span></label>")
    doc.append("<div class=fl style='grid-column:1/-1'><span class=wz-comp-t>Maps</span>"
               "<span class=hint>the basemap the Networks map draws on</span></div>")
    doc.append("<label class=fl style='grid-column:1/-1'>Google Maps key"
               f"<input id=cz-maps_key type=text maxlength=64 value='{e(inst['maps_key'])}' "
               "placeholder='AIza…' autocomplete=off>"
               "<span class=hint>optional. With a key the Networks map draws mesh nodes on "
               "Google Maps; without one it draws its own plan view, which is what a console "
               "on a closed network gets either way, because the basemap loads in YOUR "
               "browser and not on the box. A Maps browser key is public by design - it ships "
               "in the page - so restrict it to this console's address in the Google Cloud "
               "console when you mint it</span></label>")
    doc.append("<div class=fl style='grid-column:1/-1'><span class=wz-comp-t>Build defaults</span>"
               "<span class=hint>the PKI identity a new server's certificate authority "
               "carries, prefilled in the Deploy wizard. Enter these once: a console "
               "installed from this one (Operations &rsaquo; Install a console) starts "
               "with the same design and defaults</span></div>")
    doc.append(fld("org", "Organisation", "PKI organisation", ph="e.g. Acme Defence"))
    doc.append(fld("org_unit", "Org unit", "PKI organisational unit", ph="e.g. Operations"))
    doc.append(fld("country", "Country", "2-letter code", maxlen=2, ph="e.g. GB"))
    doc.append(fld("state", "State / county", "PKI state", ph="e.g. Hampshire"))
    doc.append(fld("city", "City", "PKI locality", ph="e.g. Andover"))
    if "provision-server" in acts or True:
        doc.append("<div class=fedpop-act style='grid-column:1/-1'>"
                   "<button type=submit class='a-go confirm' id=czgo>Save and apply</button>"
                   "<span id=cz-res class=lib-status role=status style='margin-left:12px'></span>"
                   "</div>")
    doc.append("</form>")
    # operator sign-in password: the product's front door. No password = the console runs
    # open (today's tailnet-trusted posture) and Overview carries a banner until one is set.
    doc.append("<section aria-label='Sign-in'><div class=ah><h2 class=title>Sign-in</h2>"
               "<span class=meta>One operator password guards this console. "
               + ("A password is currently set; sessions last 12 hours."
                  if auth_configured() else
                  "No password is set: the console is open to anyone who can reach it.")
               + "</span></div><form id=pwform class=depcard>")
    if auth_configured():
        doc.append("<label class=fl>Current password<input type=password id=pw-cur "
                   "autocomplete=current-password></label>")
    doc.append("<label class=fl>New password<input type=password id=pw-new minlength=12 "
               "autocomplete=new-password required>"
               "<span class=hint>at least 12 characters. Length beats complexity: a "
               "phrase of three or four unrelated words is strong and typeable.</span></label>"
               "<label class=fl>Confirm new password<input type=password id=pw-new2 "
               "minlength=12 autocomplete=new-password required>"
               "<span class=hint>typed again, to catch a slip</span></label>"
               "<div class=fedpop-act><button type=submit class='a-go confirm'>"
               + ("Change password" if auth_configured() else "Set password")
               + "</button><span id=pw-res class=lib-status role=status "
               "style='margin-left:12px'></span></div></form></section>")
    doc.append("""<script>(function(){
var pf=document.getElementById('pwform');
if(pf)pf.addEventListener('submit',function(ev){ev.preventDefault();
  var n1=document.getElementById('pw-new').value,
      n2=document.getElementById('pw-new2').value;
  if(n1!==n2){document.getElementById('pw-res').textContent='The two passwords do not match.';return;}
  var r=document.getElementById('pw-res');r.textContent='Saving\\u2026';
  var cur=document.getElementById('pw-cur');
  fetch('/api/setup/password',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:'operator',current:cur?cur.value:'',
      new:document.getElementById('pw-new').value})})
  .then(function(x){return x.json().then(function(j){return{code:x.status,j:j};});})
  .then(function(x){if(x.code===200){r.textContent='Password set. It applies from the next sign-in.';}
    else{r.textContent=x.j.error||'failed';}})
  .catch(function(){r.textContent='could not reach the console';});
});})();</script>""")
    doc.append("""<script>(function(){
var f=document.getElementById('czform'); if(!f)return;
f.querySelectorAll("input[type=color]").forEach(function(i){
  var code=i.parentNode.querySelector('code');
  i.addEventListener('input',function(){if(code)code.textContent=i.value;});
});
// reveal the operator-password field only when the console mode is actually changing
(function(){var m=document.getElementById('cz-console_mode'),row=document.getElementById('cz-modepw-row');
  if(m&&row)m.addEventListener('change',function(){
    var chg=m.value!==m.getAttribute('data-orig');
    row.hidden=!chg; if(!chg){var p=document.getElementById('cz-modepw');if(p)p.value='';}});})();
f.addEventListener('submit',function(ev){ev.preventDefault();
  var r=document.getElementById('cz-res');r.textContent='Saving\\u2026';
  // send ONLY what this form carries; the API merges, so identity fields
  // (product name, maker, tagline, assistant) keep their stored values
  var body={};
  ['font','console_mode','accent','accent_2','ink','band','accent_warm',
   'maps_key','org','org_unit','country','state','city'].forEach(function(k){
    var el=document.getElementById('cz-'+k); if(el) body[k]=el.value;
  });
  // the password only travels when the mode is genuinely changing
  var m=document.getElementById('cz-console_mode'),pw=document.getElementById('cz-modepw');
  if(m&&pw&&m.value!==m.getAttribute('data-orig')){
    if(!pw.value){r.textContent='enter your operator password to change console mode';return;}
    body.mode_passphrase=pw.value;}
  fetch('/api/setup/instance',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}).then(function(x){return x.json().then(function(j){return{code:x.status,j:j};});})
  .then(function(x){if(x.code===200){r.textContent='Saved. Reloading\\u2026';setTimeout(function(){location.reload();},900);}
    else{r.textContent=x.j.error||'failed';}}).catch(function(){r.textContent='could not reach the console';});
});})();</script>""")
    doc.append(footer_html(state, acts))
    return "".join(doc)


def render_error(err, active="estate"):
    e = html.escape
    inst = load_instance()
    doc = page_head(inst["product_name"], inst)
    doc.append("<header><div class=head>" + brand_mark(inst)
               + f"<span class=brand>{e(inst['product_name'])}</span>"
               + "<h1 class=eyebrow-h>Overview</h1></div>"
               + nav_html({}, active, inst) + "</header>"
               f"<main id=main class=wrap><div class='banner stale'>"
               f"<b>Board unavailable.</b> {e(err)}</div></main></body></html>")
    return "".join(doc)


def render_estate(state):
    """The fleet view and the pitch screen: verdict, tiles, drift, baseline."""
    e = html.escape
    age = age_seconds(state.get("generated_at", ""))
    stale = age is None or age > STALE_AFTER
    ev = state.get("estate_result", "UNKNOWN")
    history = load_history()
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    act_targets = set((cfg or {}).get("targets", {}).keys())
    desired = load_desired()
    drift_sw = software_drift(state, desired) if desired else []
    targets = state.get("targets", [])

    # 1.26.0: an enrolled box the wizard has not yet built - reachable, failing, and
    # with no takserver in its software inventory - is AWAITING BUILD, not FAIL. The
    # checker is rightly honest about an empty box; the estate page should not scream
    # about a server that does not exist yet, nor let it drag the estate verdict red.
    def _awaiting(t):
        if t.get("result") != "FAIL" or not t.get("reachable"):
            return False
        rows = software_rows(t)
        return bool(rows) and not any(s.get("name") == "takserver" for s in rows)

    awaiting = {t.get("name") for t in targets if _awaiting(t)}
    band_note = ""
    if awaiting:
        rest = [t.get("result", "UNKNOWN") for t in targets if t.get("name") not in awaiting]
        if any(r in ("FAIL", "UNREACHABLE") for r in rest):
            ev = "FAIL"
        elif "WARN" in rest:
            ev = "WARN"
        elif rest:
            ev = "OK"
        n = len(awaiting)
        band_note = f" · {n} box{'es' if n != 1 else ''} awaiting first build"

    doc = page_head(load_instance()["product_name"])
    doc.append(header_html(state, ev, age, "estate"))
    doc.append(f"<div class='vband {e(ev)}'><span class=vdot></span>"
               f"{e(estate_summary(state, ev, stale, age) + band_note)}</div>")
    # the empty estate renders the first-run BUILD FORM; live-refresh swapping innerHTML
    # would silently strip its handlers (inserted <script> does not execute). A form is
    # not a monitor: only a populated estate refreshes itself.
    doc.append("<main id=main class=wrap" + (" data-live=1" if targets else "") + ">")
    if AUTH_OPEN_MODE and not auth_configured() and EDITION != "deployed":
        doc.append("<div class='banner drift'><b>Running open by choice.</b><span> "
                   "VANTAGE_CONSOLE_AUTH=open is set and no password exists; anyone who "
                   "can reach this console can use it. Set a password under Customize "
                   "to close it.</span></div>")
    # a refresh mid-build must never lose the operator: if a setup job is running,
    # say so and point back at the page that reattaches to its stream
    lj = setup_last_job()
    if lj and lj.get("status") == "running":
        doc.append("<div class='banner drift'><b>A server build is running"
                   + (f" on {e(lj.get('target', ''))}" if lj.get("target") else "")
                   + ".</b><span> It carries on whatever this page does - a full build "
                   "takes 20 to 30 minutes. <a href='/deploy'>Watch its progress</a>."
                   "</span></div>")
    # An empty estate's Overview IS the first-run screen: the whole first build happens
    # right here - upload, the few details that matter, dry run, live run - and the
    # section is gone the moment anything is enrolled.
    if not targets and EDITION != "deployed":
        inst_fr = load_instance()
        # a box that already runs TAK Server gets ENROLLED, not built over - the
        # provisioner would rightly refuse, so the screen offers the right verb up front
        local_tak = os.path.isdir("/opt/tak")
        lib = [en for en in library_manifest() if en.get("file")]
        libopts = "".join(
            f"<option value='{e(en['file'])}' data-sha='{e(en.get('sha256', ''))}'>"
            f"{e(en['file'])}</option>" for en in lib)
        if local_tak:
            doc.append(
                "<section class=firstrun aria-label='First server' id=firstrun "
                "data-mode=enrol-local>"
                "<h2 class=title>TAK Server is already running on this box</h2>"
                "<p class=doct>Enrol it, and this console starts monitoring and managing "
                "it - health, credentials, federation, upgrades. Nothing is installed or "
                "changed by enrolment.</p>"
                "<label class=fl style='max-width:380px'>Public name (FQDN)"
                f"<input id=fr-fqdn placeholder='tak.example.org'>"
                "<span class=hint>the name devices connect to; leave blank if unsure</span>"
                "</label>"
                "<div class=fr-acts>"
                "<button type=button class='a-go confirm' id=fr-go>Enrol this box</button>"
                "<span id=fr-res class=lib-status role=status></span>"
                "<a class=fr-alt href='/deploy'>or the full wizard: another box, or a "
                "fresh build</a></div>"
                "<pre id=fr-log class=deplog hidden></pre>"
                "</section>"
                f"<script>{FIRSTRUN_JS}</script>")
        else:
            doc.append(
            "<section class=firstrun aria-label='First server' id=firstrun>"
            "<h2 class=title>No servers yet</h2>"
            "<p class=doct>Your first TAK server, right here on this box. TAK software is "
            "licensed, so the release always enters as your deliberate upload; the console "
            "never fetches or bundles it.</p>"
            "<p class=doct>When you press Install, the console works through nine stages: "
            "it secures the operating system, installs the foundations (Java, a database, "
            "a web server), installs TAK Server, builds its certificate authority, gets a "
            "trusted TLS certificate for your public name, and starts it up. Each stage "
            "narrates itself below as it runs; the whole build usually takes 20 to 30 "
            "minutes, most of it in the first and last stages.</p>"
            "<div class=fr-grid>"
            "<div class=fr-col><span class=wz-comp-t>1 · The TAK Server release</span>"
            + (f"<label class=fl>Already uploaded<select id=fr-lib>{libopts}</select>"
               "<span class=hint>or upload a different one below</span></label>" if lib else "")
            + "<label class=fl>Upload the .deb from tak.gov"
            "<input type=file id=fr-file accept=.deb>"
            "<span class=hint id=fr-upstat>nothing uploaded yet</span></label></div>"
            "<div class=fr-col><span class=wz-comp-t>2 · This server's identity</span>"
            f"<label class=fl>Public name (FQDN)<input id=fr-fqdn placeholder='tak.example.org' "
            "required><span class=hint>devices connect to this name; it becomes the "
            "certificate. It must already point at this box.</span></label>"
            f"<label class=fl>Certificate email<input id=fr-email type=email "
            "placeholder='ops@example.org' required>"
            "<span class=hint>Let's Encrypt expiry notices</span></label>"
            f"<label class=fl>Organisation<input id=fr-org value='{e(inst_fr['org'])}' "
            "placeholder='e.g. Acme Defence' required></label>"
            "<div class=fr-pair>"
            f"<label class=fl>Org unit<input id=fr-orgunit value='{e(inst_fr['org_unit'])}' "
            "placeholder='e.g. Operations' required></label>"
            f"<label class=fl>Country<input id=fr-country value='{e(inst_fr['country'])}' "
            "maxlength=2 placeholder='GB' required></label></div>"
            "<div class=fr-pair>"
            f"<label class=fl>State / county<input id=fr-state value='{e(inst_fr['state'])}' "
            "placeholder='e.g. Hampshire' required></label>"
            f"<label class=fl>City<input id=fr-city value='{e(inst_fr['city'])}' "
            "placeholder='e.g. Andover' required></label>"
            "<label class=fl>Certificate password (optional)"
            "<input id=fr-capass type=password maxlength=64 autocomplete=off "
            "placeholder='blank = strong generated one'>"
            "<span class=hint>typed into devices when importing certificates. A simple "
            "one is fine for an experimental build; leave blank for anything real.</span>"
            "</label></div></div>"
            "<div class=fr-devices><span class=wz-comp-t>3 · First devices (optional)</span>"
            "<span class=hint>Named here, their enrolment QR codes are minted the moment "
            "the build finishes - scan and you are on the map. You can always add more "
            "later from the server's page.</span>"
            "<div id=fr-creds></div>"
            "<button type=button class=cred-refresh id=fr-addcred>+ Add a device</button>"
            + ("" if auth_configured() else
               "<label class=fl id=fr-passwrap hidden>Operator password"
               "<input type=password id=fr-pass autocomplete=off>"
               "<span class=hint>this console runs open, so minting credentials asks "
               "for the action passphrase</span>"
               "</label>") + "</div>"
            "<div class=fr-acts>"
            "<button type=button class='a-go confirm' id=fr-go disabled>"
            "Install TAK Server</button>"
            "<button type=button class=fr-dry id=fr-dry disabled>preview first "
            "(dry run)</button>"
            "<span id=fr-res class=lib-status role=status></span>"
            "<a class=fr-alt href='/deploy'>or the full wizard: another box, or enrol a "
            "server you already run</a></div>"
            "<pre id=fr-log class=deplog hidden></pre>"
            "</section>"
            f"<script>{FIRSTRUN_JS}</script>")
    doc.append(stale_banner(age, stale))

    if drift_sw:
        items = []
        for name, lbl, comp, rep, want in drift_sw:
            aid = COMPONENT_ACTION.get(comp)
            link = (f" <a class=driftlink href='/server/{e(name)}#act-{e(aid)}'>update</a>"
                    if aid in acts and name in act_targets else "")
            items.append(f"{e(lbl)}: {e(comp)} {e(rep)} → {e(want)}{link}")
        doc.append("<div class='banner drift'><b>Software drift.</b><span>"
                   f"{len(drift_sw)} component{'s' if len(drift_sw) != 1 else ''} differ from "
                   "the baseline. " + "; ".join(items) + ". Set the baseline below to what you "
                   "run.</span></div>")
    elif not desired:
        drift = state.get("checker_drift") or []
        if drift:
            doc.append("<div class='banner drift'><b>Checker drift.</b><span>Boxes are on "
                       f"different tak-health versions ({e(', '.join(drift))}). Push the "
                       "latest to all boxes.</span></div>")

    # tile decorations: which box runs THIS console (admin), and what services each box
    # carries - tiny icons from the health inventory, each with a hover tooltip. Only what
    # the inventory can prove; a box running a deployed-edition console is not detectable
    # from here, so no false "client" badges.
    _SVC_ICONS = {
        "takserver": ("TAK Server",
                      "<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2>"
                      "<rect x=3 y=4 width=18 height=7 rx=1.5 />"
                      "<rect x=3 y=13 width=18 height=7 rx=1.5 />"
                      "<path d='M7 7.5h.01M7 16.5h.01' stroke-linecap=round /></svg>"),
        "cloudtak": ("CloudTAK web map",
                     "<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 "
                     "stroke-linejoin=round><path d='M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2z'/>"
                     "<path d='M9 4v14M15 6v14'/></svg>"),
        "mediamtx": ("MediaMTX video",
                     "<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 "
                     "stroke-linejoin=round><rect x=3 y=6 width=13 height=12 rx=2 />"
                     "<path d='M16 10l5-3v10l-5-3'/></svg>"),
    }
    _chost = str(state.get("console_host") or "")

    def _is_admin_box(t):
        if not _chost:
            return False
        fq = str(t.get("fqdn") or "")
        return (t.get("name") == _chost or fq.split(".")[0] == _chost
                or (_chost and _chost in fq))

    def _svc_row(t):
        spans = []
        for s in software_rows(t):
            ic = _SVC_ICONS.get(s.get("name"))
            if not ic:
                continue
            tip = f"{ic[0]} {s.get('version', '')} · {s.get('state', '')}".strip(" ·")
            spans.append(f"<span class=svc title='{e(tip)}'>{ic[1]}</span>")
        return f"<div class=svcrow>{''.join(spans)}</div>" if spans else ""

    def _tile_issues(t):
        # the FAIL/WARN checks, on the tile face, so a problem shows without expanding
        bad = [ck for ck in (t.get("checks") or [])
               if ck.get("status") in ("FAIL", "WARN")]
        if not bad:
            return ""
        bad.sort(key=lambda ck: 0 if ck.get("status") == "FAIL" else 1)
        items = []
        for ck in bad[:3]:
            st = ck.get("status", "")
            nm = f"{ck.get('category', '')} › {ck.get('name', '')}".strip(" ›")
            dt = ck.get("detail", "")
            items.append(f"<li class='iss {e(st)}'><span class=iss-st>{e(st)}</span>"
                         f"<span class=iss-nm>{e(nm)}</span>"
                         + (f"<span class=iss-dt>{e(dt)}</span>" if dt else "") + "</li>")
        if len(bad) > 3:
            items.append(f"<li class=iss-more>+{len(bad) - 3} more</li>")
        return f"<ul class=tissues>{''.join(items)}</ul>"

    def _tile_inventory(t):
        # what is on THIS box, each component against the baseline - the detail that used
        # to be a per-server column in the baseline table, now folded into the tile
        base = desired or {}
        rows = []
        for comp in ("tak-health", "takserver", "cloudtak", "mediamtx"):
            row = next((s for s in software_rows(t) if s.get("name") == comp), None)
            want = base.get(comp, "")
            if row is None and not want:
                continue
            ver = (row or {}).get("version") or ""
            if row is None:
                cell, cls = "not installed", "b-abs"
            elif not ver:
                cell, cls = "present", "b-abs"
            elif want and version_current(ver, want):
                cell, cls = ver, "b-ok"
            elif want:
                cell, cls = ver, "b-drift"
            else:
                cell, cls = ver, ""
            rows.append(f"<tr><th scope=row>{e(comp)}</th>"
                        f"<td class='{cls}'>{e(cell)}</td>"
                        f"<td class=b-abs>{e(want or '—')}</td></tr>")
        if not rows:
            return "<div class=meta style='margin-top:12px'>No software inventory reported yet.</div>"
        return ("<table class=tinv><tr><th>Component</th><th>On this box</th>"
                "<th>Baseline</th></tr>" + "".join(rows) + "</table>")

    def _mesh_row(t):
        # Spec 001's AC6 contract: output, not process. ok only when a packet has actually
        # been forwarded; an active gateway that has never forwarded shows quiet, and so
        # does a missing radio. The data-mesh marker is the tested contract - it stays out
        # of the CSS and the JS so the page carries exactly one per gateway box.
        m = t.get("mesh") or {}
        if not m:
            return ""
        ok = bool(m.get("last_forwarded"))
        n = m.get("nodes_seen", 0) or 0
        if ok:
            lbl = f"mesh · {n} node{'s' if n != 1 else ''}"
        elif not m.get("radio_present"):
            lbl = "mesh · radio missing"
        else:
            lbl = "mesh · quiet, nothing forwarded"
        return (f"<div class='meshline {'m-ok' if ok else 'm-quiet'}' "
                f"data-mesh={'ok' if ok else 'quiet'}>{e(lbl)}</div>")

    doc.append("<h2 class=sec-eye>Servers</h2><div class=board>")
    for t in targets:
        res, name = t.get("result", "UNKNOWN"), t.get("name", "?")
        c = t.get("counts") or {}
        if name in awaiting:
            res_cls, chip = "BUILD", "AWAITING BUILD"
            sub = "enrolled - TAK Server not yet installed"
        else:
            res_cls, chip = res, res
            sub = str(t.get("fqdn") or t.get("profile") or "")
        admin = ("<span class=chip-admin title='this box runs the console you are "
                 "looking at'>admin</span>" if _is_admin_box(t) else "")
        doc.append(f"<details class='tile {e(res_cls)}' data-name='{e(name)}'><summary>"
                   f"<div class=t1><div><div class=nm>{e(t.get('label', name))}{admin}</div>"
                   f"<div class=sub>{e(sub)}</div>{_svc_row(t)}{_mesh_row(t)}</div>"
                   f"<span class='chip c-{e(res_cls)}'>{e(chip)}</span></div>")
        if c:
            doc.append("<div class=counts>"
                       f"<span class=k-ok><b>{c.get('ok', 0)}</b> ok</span>"
                       f"<span class=k-warn><b>{c.get('warn', 0)}</b> warn</span>"
                       f"<span class=k-fail><b>{c.get('fail', 0)}</b> fail</span>"
                       f"<span><b>{c.get('skip', 0)}</b> skip</span></div>")
        doc.append(_tile_issues(t))
        doc.append(uptime_strip(history, name, 48, mini=True))
        doc.append("<div class=tile-toggle><span class=chev></span>"
                   "What's installed &amp; baseline</div></summary>")
        doc.append("<div class=tile-body>" + _tile_inventory(t)
                   + f"<a class=tile-open href='/server/{e(name)}'>"
                     "Open server page &rsaquo;</a></div></details>")
    doc.append("</div>")

    # the key: name every marker the tiles above actually carry, because a hover tooltip
    # is easy to miss. Only the services present somewhere in the estate, plus the admin
    # marker when a box here runs this console.
    present = []
    for nm in _SVC_ICONS:
        if any(s.get("name") == nm for t in targets for s in software_rows(t)):
            present.append(nm)
    key_bits = []
    if any(_is_admin_box(t) for t in targets):
        key_bits.append("<span class=ki><span class=chip-admin>admin</span>"
                        "runs this console</span>")
    for nm in present:
        lbl, svg = _SVC_ICONS[nm]
        key_bits.append(f"<span class=ki><span class=svc>{svg}</span>{e(lbl)}</span>")
    if key_bits:
        doc.append("<div class=svc-key><span class=kk>Key</span>"
                   + "".join(key_bits) + "</div>")

    # servers seen through peers: the rest of the estate, read-only, honestly labelled
    _pcache = _load_json_list(PEERS_CACHE)
    if _pcache:
        doc.append("<h2 class=sec-eye>Via peer consoles</h2><div class=board>")
        for pc in _pcache:
            snap = pc.get("snapshot") or {}
            for t in snap.get("servers", []):
                res = t.get("result", "UNKNOWN")
                doc.append(
                    f"<div class='tile {e(res)} peer-tile'><div class=t1>"
                    f"<div><div class=nm>{e(t.get('label') or t.get('name', '?'))}</div>"
                    f"<div class=sub>{e(t.get('fqdn') or '')}</div></div>"
                    f"<span class='chip c-{e(res)}'>{e(res)}</span></div>"
                    f"<div class=counts><span>managed by <b>{e(pc.get('name', '?'))}</b>"
                    f" · seen {e(str(pc.get('pulled', ''))[11:16])}Z</span></div></div>")
        doc.append("</div>")

    # Software baseline (editable): the versions drift is judged against. Shown whenever
    # the estate has boxes, so one with no baseline can create one; "Take current
    # inventory" adopts what is installed now - the one-click fix when a new release makes
    # an old baseline read backwards (a box AHEAD of the baseline still shows as drift).
    if targets and EDITION != "deployed":
        KNOWN_COMPONENTS = ("tak-health", "takserver", "cloudtak", "mediamtx")
        base = desired or {}
        inv_now = {}
        for comp in KNOWN_COMPONENTS:
            seen = {}
            for t in targets:
                row = next((s for s in software_rows(t) if s.get("name") == comp), None)
                v = ((row or {}).get("version") or "").strip()
                if v:
                    seen[v] = seen.get(v, 0) + 1
            if seen:
                inv_now[comp] = max(seen, key=seen.get)
        doc.append("<h2 class=sec-eye>Software baseline</h2>")
        doc.append("<form class=baseline-form data-inv='" + e(json.dumps(inv_now)) + "'>")
        # just the target baseline now: each box is judged against it inside its own tile
        # (expand a tile above to see that box's versions against these targets)
        doc.append("<div class=tablewrap><table class='dtable baseline-only'><tr>"
                   "<th scope=col>Component</th><th scope=col>Baseline (target)</th></tr>")
        for comp in KNOWN_COMPONENTS:
            want = base.get(comp, "")
            doc.append(f"<tr><th scope=row>{e(comp)}</th>"
                       f"<td><input class=bl-inp name='{e(comp)}' value=\"{e(want)}\" "
                       "placeholder='unset' spellcheck=false autocomplete=off></td></tr>")
        doc.append("</table></div>")
        doc.append("<div class=baseline-actions>"
                   "<button type=button class='cred-refresh bl-adopt'>Take current inventory as baseline</button>"
                   "<button type=submit class='a-go bl-save'>Save baseline</button>"
                   "<span class=bl-msg role=status></span></div>")
        doc.append("<div class=meta style='margin-top:8px'>The baseline is "
                   "<code>/etc/vantage-console/desired.json</code>, root-owned. Each box is judged "
                   "against these targets inside its own tile above (expand a tile): on-baseline "
                   "reads green, a drift reads amber. Leave a field blank to stop judging that "
                   "component. When a new release lands, <b>Take current inventory</b> then "
                   "<b>Save</b> adopts what is installed now.</div>")
        doc.append(f"<script>{BASELINE_JS}</script>")

    doc.append(footer_html(state, acts))
    return "".join(doc)


# ---------- bulk enrolment: QR sheet (PDF) + cert bundle (ZIP), pure stdlib ------------
# Devices enrol from a QR the box renders with qrencode (a 1-bit palette PNG). For a whole
# team we loop that and lay the QRs out as a printable PDF, plus a ZIP of per-device files.
# No third-party libraries (the console is stdlib-only): a minimal PNG decoder feeds a
# minimal PDF writer that embeds each QR as a crisp 1-bit image.
def _png_to_matrix(data):
    """Decode a PNG (grayscale/palette 1-8 bit, or 8-bit RGB/RGBA) to a 2D bytearray grid
    of 0/1 where 1 = light. Covers qrencode's 1-bit palette output."""
    import struct as _st
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, width, height, bit, ctype, plte, idat = 8, 0, 0, 8, 0, b"", bytearray()
    while pos + 8 <= len(data):
        ln = _st.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            width, height, bit, ctype = _st.unpack(">IIBB", body[:10])
        elif typ == b"PLTE":
            plte = body
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    raw = zlib.decompress(bytes(idat))
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    stride = (width * ch * bit + 7) // 8
    bpp = max(1, (ch * bit + 7) // 8)
    lines, prev, p = [], bytearray(stride), 0
    for _y in range(height):
        ft = raw[p]; p += 1
        cur = bytearray(raw[p:p + stride]); p += stride
        if ft == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (cur[i] + pr) & 255
        lines.append(cur); prev = cur

    def _plum(idx):
        o = idx * 3
        return (plte[o] * 299 + plte[o + 1] * 587 + plte[o + 2] * 114) // 1000
    maxv = (1 << bit) - 1
    matrix = []
    for line in lines:
        row = bytearray(width)
        for x in range(width):
            if bit < 8:
                bp = x * bit
                val = (line[bp >> 3] >> (8 - bit - (bp & 7))) & maxv
                lum = _plum(val) if ctype == 3 else val * 255 // maxv
            else:
                base = x * ch
                if ctype == 3:
                    lum = _plum(line[base])
                elif ctype in (0, 4):
                    lum = line[base]
                else:
                    lum = (line[base] * 299 + line[base + 1] * 587 + line[base + 2] * 114) // 1000
            row[x] = 1 if lum >= 128 else 0
        matrix.append(row)
    return matrix


def _pdf_image_obj(matrix):
    """1-bit DeviceGray image XObject (dict, FlateDecode bytes) from a 0/1 matrix."""
    h, w = len(matrix), len(matrix[0])
    rowbytes = (w + 7) // 8
    buf = bytearray(rowbytes * h)
    for y in range(h):
        base, rr = y * rowbytes, matrix[y]
        for x in range(w):
            if rr[x]:
                buf[base + (x >> 3)] |= (0x80 >> (x & 7))
    comp = zlib.compress(bytes(buf), 9)
    d = (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray "
         f"/BitsPerComponent 1 /Filter /FlateDecode /Length {len(comp)} >>")
    return d, comp


def build_qr_pdf(title, items, cols=3, per_page=9):
    """items: [{label, sub, matrix}]. A4 portrait grid of QR images with labels."""
    PW, PH, M, gut = 595.0, 842.0, 40.0, 16.0
    rows = (per_page + cols - 1) // cols
    cellw = (PW - 2 * M - (cols - 1) * gut) / cols
    cellh = (PH - 2 * M - 34 - (rows - 1) * gut) / rows
    qsize = min(cellw, cellh - 30)

    def esc(s):
        s = str(s).encode("latin-1", "replace").decode("latin-1")
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    objs, next_id, page_ids = [], 3, []
    pages = [items[i:i + per_page] for i in range(0, len(items), per_page)] or [[]]
    for pg in pages:
        content = [f"BT /F1 15 Tf {M} {PH - M - 4} Td ({esc(title)}) Tj ET"]
        refs = []
        for idx, it in enumerate(pg):
            r, c = idx // cols, idx % cols
            cx = M + c * (cellw + gut)
            cy = PH - M - 34 - r * (cellh + gut) - cellh
            qx = cx + (cellw - qsize) / 2
            qy = cy + cellh - qsize
            dstr, comp = _pdf_image_obj(it["matrix"])
            iid = next_id; next_id += 1
            objs.append((iid, dstr, comp))
            refs.append((iid, idx))
            content.append(f"q {qsize:.2f} 0 0 {qsize:.2f} {qx:.2f} {qy:.2f} cm /Im{idx} Do Q")
            content.append(f"BT /F1 10 Tf {cx:.2f} {qy - 13:.2f} Td ({esc(it.get('label',''))}) Tj ET")
            if it.get("sub"):
                content.append(f"BT /F1 7.5 Tf {cx:.2f} {qy - 24:.2f} Td ({esc(it['sub'])}) Tj ET")
        cs = zlib.compress("\n".join(content).encode(), 9)
        cid = next_id; next_id += 1
        objs.append((cid, f"<< /Length {len(cs)} /Filter /FlateDecode >>", cs))
        pid = next_id; next_id += 1
        xobj = " ".join(f"/Im{idx} {iid} 0 R" for iid, idx in refs)
        objs.append((pid, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                          f"/Resources << /Font << /F1 FONT 0 R >> /XObject << {xobj} >> >> "
                          f"/Contents {cid} 0 R >>", None))
        page_ids.append(pid)
    fid = next_id; next_id += 1
    objs.append((fid, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", None))
    objs = [(o, (b.replace("FONT", str(fid)) if isinstance(b, str) else b), e) for (o, b, e) in objs]
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    allo = [(1, "<< /Type /Catalog /Pages 2 0 R >>", None),
            (2, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>", None)] + objs
    allo.sort(key=lambda o: o[0])
    out, off = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), {}
    for oid, body, extra in allo:
        off[oid] = len(out)
        out += f"{oid} 0 obj\n".encode()
        out += (body.encode() + b"\nstream\n" + extra + b"\nendstream") if extra is not None else body.encode()
        out += b"\nendobj\n"
    xp = len(out); n = max(off) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for i in range(1, n):
        out += f"{off.get(i, 0):010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xp}\n%%EOF\n".encode()
    return bytes(out)


def build_enrol_artifacts(server_label, devices):
    """From enrolled devices [{user, group, url, itak, password, png(b64)}], build the QR
    sheet PDF and a ZIP (per-device QR png + a credentials text). Returns (pdf, zipbytes)."""
    import io as _io, zipfile as _zf
    items = []
    for d in devices:
        png = d.get("png") or ""
        try:
            matrix = _png_to_matrix(base64.b64decode(png)) if png else None
        except Exception:
            matrix = None
        if matrix:
            items.append({"label": d.get("user", "?"),
                          "sub": f"group: {d.get('group', '')}", "matrix": matrix})
    title = f"Vantage enrolment QR sheet - {server_label}"
    pdf = build_qr_pdf(title, items) if items else build_qr_pdf(title, [])
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as z:
        for d in devices:
            u = re.sub(r"[^A-Za-z0-9._-]", "_", str(d.get("user", "device")))
            if d.get("png"):
                try:
                    z.writestr(f"{u}/{u}-enrol-qr.png", base64.b64decode(d["png"]))
                except Exception:
                    pass
            txt = (f"Vantage enrolment credential\nDevice:   {d.get('user', '')}\n"
                   f"Group:    {d.get('group', '')}\nServer:   {server_label}\n"
                   f"Password: {d.get('password', '')}\n\nATAK  (scan the QR, or use this URL):\n"
                   f"{d.get('url', '')}\n\niTAK  (host,user,pass):\n{d.get('itak', '')}\n")
            z.writestr(f"{u}/{u}-credentials.txt", txt)
        z.writestr("QR-SHEET.pdf", pdf)
    return pdf, buf.getvalue()


def enrol_batch_api(data, client):
    """Bulk-enrol N devices into one group, then build the QR sheet PDF + a cert ZIP.
    Names come as an explicit list or a prefix+count. Session-gated; on an open console the
    operator passphrase gates it, exactly as single enrol does. Synchronous, capped at 30."""
    target = str(data.get("target", ""))
    group = str(data.get("group", ""))
    cfg = load_actions_config()
    if target not in ((cfg or {}).get("targets") or {}):
        return 400, {"error": "unknown target"}
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", group) or group.startswith("-"):
        return 400, {"error": "group must match [A-Za-z0-9_-], 1-40 chars"}
    raw_users = data.get("users")
    if isinstance(raw_users, list) and raw_users:
        users = [str(u).strip() for u in raw_users if str(u).strip()]
    else:
        prefix = str(data.get("prefix", "")).strip()
        try:
            count, start = int(data.get("count", 0)), int(data.get("start", 1))
        except (TypeError, ValueError):
            return 400, {"error": "count and start must be numbers"}
        if not prefix or count < 1:
            return 400, {"error": "give a name list, or a prefix and a count"}
        width = max(2, len(str(start + count - 1)))
        users = [f"{prefix}{str(start + i).zfill(width)}" for i in range(count)]
    seen, clean = set(), []
    for u in users:
        if u in seen:
            continue
        seen.add(u)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", u) or u.startswith("-"):
            return 400, {"error": f"bad device name: {u[:40]}"}
        clean.append(u)
    if not clean:
        return 400, {"error": "no device names"}
    if len(clean) > 30:
        return 400, {"error": "at most 30 devices at once"}
    authed = auth_configured()
    if not authed and not verify_passphrase(cfg, data.get("passphrase")):
        return 403, {"error": "operator passphrase required for enrolment"}
    devices, errors = [], []
    for u in clean:
        code, res = run_action("enrol-device", target, {"user": u, "group": group},
                               data.get("passphrase"), True, client, passphrase_ok=True)
        if code == 200:
            devices.append({"user": u, "group": group, "password": res.get("password", ""),
                            "url": res.get("url", ""), "itak": res.get("itak", ""),
                            "png": res.get("png", "")})
        else:
            errors.append({"user": u, "error": (res.get("error") or res.get("message") or "failed")[:120]})
    label = next((t.get("label", t.get("name")) for t in load_state().get("targets", [])
                  if t.get("name") == target), target)
    pdf, zbytes = build_enrol_artifacts(label, devices)
    audit({"action": "enrol-batch", "target": target,
           "detail": f"{len(devices)}/{len(clean)} into {group}",
           "result": "OK" if devices else "ERROR", "client": client})
    return 200, {"count": len(devices), "attempted": len(clean), "errors": errors,
                 "devices": [{k: d[k] for k in ("user", "group", "url", "itak", "png")} for d in devices],
                 "pdf_b64": base64.b64encode(pdf).decode(),
                 "zip_b64": base64.b64encode(zbytes).decode()}


def usb_copy_api(data, client):
    """Copy the enrolment bundle (QR sheet PDF + ZIP) onto a mounted USB stick on the
    console's own box - the kiosk path, where the operator has no shell. Session-gated.
    The console stages the two files in its own agent dir; the root helper finds the USB
    and copies them, then the staging dir is always cleaned up."""
    try:
        pdf = base64.b64decode(data.get("pdf_b64", "") or "")
        zbytes = base64.b64decode(data.get("zip_b64", "") or "")
    except Exception:
        return 400, {"error": "bad bundle"}
    if not pdf or not zbytes:
        return 400, {"error": "nothing to copy"}
    stage = os.path.join("/var/lib/vantage-console/agent/usb-staging", os.urandom(6).hex())
    try:
        os.makedirs(stage, exist_ok=True)
        with open(os.path.join(stage, "QR-SHEET.pdf"), "wb") as fh:
            fh.write(pdf)
        with open(os.path.join(stage, "enrolment.zip"), "wb") as fh:
            fh.write(zbytes)
        ok, out = setup_helper("usb-copy", "usbcopy", stage, timeout=90)
    except Exception as ex:
        return 500, {"error": f"staging failed: {ex}"[:200]}
    finally:
        try:
            for f in os.listdir(stage):
                os.remove(os.path.join(stage, f))
            os.rmdir(stage)
        except Exception:
            pass
    if not ok:
        return 400, {"error": (out or "no USB stick found").strip()[:200]}
    dest = (out.replace("OK", "", 1).strip().splitlines() or ["USB"])[-1].strip()
    audit({"action": "usb-copy", "result": "OK", "detail": dest[:120], "client": client})
    return 200, {"dest": os.path.basename(dest) or "USB"}


def load_offline_api(data, client):
    """Deploy a module (cloudtak/mediamtx) OFFLINE: stream the library's saved image tarball
    to the target over its load-images key, where it is docker-loaded and the pinned compose
    applied - no internet. A background job with a live log, because the tarball is large."""
    module = str(data.get("module", ""))
    target = str(data.get("target", ""))
    if module not in ("cloudtak", "mediamtx"):
        return 400, {"error": "module must be cloudtak or mediamtx"}
    cfg = load_actions_config()
    if target not in ((cfg or {}).get("targets") or {}):
        return 400, {"error": "unknown target"}
    if "load-images" not in enabled_actions(cfg):
        return 400, {"error": "the load-images action is not enrolled on this estate"}
    want = (load_desired() or {}).get(module, "")
    entry = library_image_for(module, want)
    if not entry:
        return 404, {"error": f"no {module} image tarball in the library"
                     + (f" for baseline {want}" if want else "") + " - upload one first"}
    tarball = os.path.join(LIBRARY_DIR, entry["file"])
    if not os.path.isfile(tarball):
        return 404, {"error": "the image file is missing from the library"}
    key = os.path.join(ACTION_KEYS, ACTIONS["load-images"]["key"])
    dest = cfg["targets"][target]
    digest = str(entry.get("sha256", "") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        h = hashlib.sha256()
        with open(tarball, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
    job_id = "j" + os.urandom(6).hex()
    os.makedirs(JOBS_DIR, exist_ok=True)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _job_write(job_id, {"job": job_id, "action": "load-images", "target": target,
                        "module": module, "status": "running", "started": started})
    audit({"action": "load-images", "target": target, "detail": f"{module} {entry['file']}",
           "result": "STARTED", "job": job_id, "client": client})
    size = os.path.getsize(tarball)

    def run():
        log_path, _ = _job_paths(job_id)
        rc = 1
        try:
            with open(log_path, "w") as log:
                def say(m):
                    log.write(m.rstrip() + "\n"); log.flush()
                say(f"== OFFLINE DEPLOY {module} -> {target} ==")
                say(f"tarball {entry['file']} ({size // (1 << 20)} MB), sha256 {digest[:12]}…")
                say("-- streaming to the box, then docker load + compose (no internet) --")
                cmd = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                       "-o", "StrictHostKeyChecking=accept-new", dest,
                       f"load-images {digest} {module}"]
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, bufsize=0)
                sent, mark = 0, 0
                with open(tarball, "rb") as fh:
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        try:
                            p.stdin.write(chunk)
                        except (BrokenPipeError, OSError):
                            break
                        sent += len(chunk)
                        if sent - mark >= (64 << 20):        # a note every 64 MB
                            mark = sent
                            say(f"   sent {sent // (1 << 20)} / {size // (1 << 20)} MB")
                try:
                    p.stdin.close()
                except Exception:
                    pass
                for line in p.stdout:
                    say(line.decode("utf-8", "replace").rstrip())
                prc = p.wait(timeout=3600)
                say(f"-- sent {sent // (1 << 20)} MB; box exit {prc} --")
                if prc == 0:
                    say("OK offline deploy complete"); rc = 0
        except Exception as ex:
            try:
                with open(log_path, "a") as log:
                    log.write(f"ERR {ex}\n")
            except Exception:
                pass
        fin = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _job_write(job_id, {"job": job_id, "action": "load-images", "target": target,
                            "module": module, "status": "done" if rc == 0 else "failed",
                            "started": started, "finished": fin})

    threading.Thread(target=run, daemon=True).start()
    return 200, {"job": job_id}


def usb_import_list(client):
    """List importable files on a mounted USB stick (disconnected ingest)."""
    ok, out = setup_helper("usb-import", "list", timeout=30)
    if not ok:
        return 400, {"error": (out or "no USB stick found").strip()[:200]}
    files = []
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and re.fullmatch(r"[A-Za-z0-9._-]{1,120}", parts[0]):
            try:
                files.append({"name": parts[0], "bytes": int(parts[1])})
            except ValueError:
                pass
    return 200, {"files": files}


def usb_import_file(data, client):
    """Import one file from the USB into the right place: .deb -> library, a cloudtak/mediamtx
    image tarball -> library (kind image), .apk/.zip -> the Store software shelf. The helper
    stages it into the console's agent dir; the console validates and files it, recording the
    manifest where one is kept."""
    fname = str(data.get("file", ""))
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", fname):
        return 400, {"error": "bad filename"}
    ok, out = setup_helper("usb-import", "stage", fname, timeout=600)
    if not ok:
        return 400, {"error": (out or "import failed").strip()[:200]}
    staged = (out.replace("OK", "", 1).strip().splitlines() or [""])[-1].strip()
    if not staged.startswith("/var/lib/vantage-console/agent/usb-import/") or not os.path.isfile(staged):
        return 400, {"error": "the file did not stage"}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    low = fname.lower()
    try:
        n = os.path.getsize(staged)
        sha, head = hashlib.sha256(), b""
        with open(staged, "rb") as fh:
            head = fh.read(512)
            sha.update(head)
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha.update(chunk)
        if low.endswith(".deb"):
            if not head.startswith(b"!<arch>"):
                os.remove(staged); return 400, {"error": "not a Debian package (.deb)"}
            os.makedirs(LIBRARY_DIR, exist_ok=True)
            os.replace(staged, os.path.join(LIBRARY_DIR, fname))
            library_record({"file": fname, "sha256": sha.hexdigest(), "bytes": n, "arch": "amd64",
                            "uploaded": now})
            where = "library (TAK Server package)"
        elif low.endswith(".tar") or low.endswith(".tar.gz"):
            is_gzip = head[:2] == b"\x1f\x8b"
            is_tar = len(head) >= 262 and head[257:262] == b"ustar"
            if not (is_gzip or is_tar):
                os.remove(staged); return 400, {"error": "not a tar/gzip archive"}
            module = ("cloudtak" if low.startswith("cloudtak")
                      else "mediamtx" if low.startswith("mediamtx") else "")
            if not module:
                os.remove(staged)
                return 400, {"error": "name an image tarball cloudtak-… or mediamtx-… so its module is known"}
            m = re.search(r"(v?\d[\w.+-]*)", fname)
            version = m.group(1) if m else ""
            os.makedirs(LIBRARY_DIR, exist_ok=True)
            os.replace(staged, os.path.join(LIBRARY_DIR, fname))
            library_record({"file": fname, "sha256": sha.hexdigest(), "bytes": n, "kind": "image",
                            "module": module, "version": version, "uploaded": now})
            where = f"library ({module} image{', ' + version if version else ''})"
        else:  # .apk / .zip -> the device software shelf
            shelf = os.path.join(STORE_ROOT, "software")
            os.makedirs(shelf, exist_ok=True)
            os.replace(staged, os.path.join(shelf, fname))
            where = "File store › software (device shelf)"
        audit({"action": "usb-import", "detail": f"{fname} -> {where}", "result": "OK", "client": client})
        return 200, {"imported": fname, "where": where}
    except Exception as ex:
        try:
            os.remove(staged)
        except Exception:
            pass
        return 500, {"error": f"import failed: {ex}"[:200]}


def _software_shelf():
    return os.path.join(STORE_ROOT, "software")


def software_current_set():
    """Filenames on the software shelf the operator has marked the current version - what a
    device should install. Held in .current.json on the shelf (the console owns the shelf)."""
    try:
        with open(os.path.join(_software_shelf(), ".current.json")) as fh:
            d = json.load(fh)
        return set(d) if isinstance(d, list) else set()
    except Exception:
        return set()


def software_apps():
    """Files on the software shelf, newest first, each flagged current or not."""
    shelf = _software_shelf()
    cur = software_current_set()
    out = []
    if os.path.isdir(shelf):
        for fn in os.listdir(shelf):
            if fn.startswith("."):
                continue
            p = os.path.join(shelf, fn)
            if os.path.isfile(p):
                out.append({"name": fn, "bytes": os.path.getsize(p),
                            "mtime": os.path.getmtime(p), "current": fn in cur})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def software_current_api(data, client):
    """Mark (or unmark) a software-shelf file as the current version. /eud surfaces the
    current build as the recommended download; devices pull it over the closed network."""
    fname = str(data.get("file", ""))
    on = bool(data.get("current", True))
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", fname):
        return 400, {"error": "bad filename"}
    shelf = _software_shelf()
    if not os.path.isfile(os.path.join(shelf, fname)):
        return 404, {"error": "no such file on the software shelf"}
    cur = software_current_set()
    cur.add(fname) if on else cur.discard(fname)
    os.makedirs(shelf, exist_ok=True)
    tmp = os.path.join(shelf, ".current.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(sorted(cur), fh)
    os.replace(tmp, os.path.join(shelf, ".current.json"))
    audit({"action": "software-current", "detail": f"{fname}={'on' if on else 'off'}",
           "result": "OK", "client": client})
    return 200, {"file": fname, "current": on}


def destroy_api(data, client):
    """ERASE the estate footprint from a box. Gated hard: a typed DESTROY and the operator
    passphrase. Runs tak-destroy on the box as a job that streams the wipe back (the operator's
    confirmation). If the box is unreachable the job fails cleanly - Remove from monitoring is
    the console-side fallback."""
    target = str(data.get("target", ""))
    cfg = load_actions_config()
    if target not in ((cfg or {}).get("targets") or {}):
        return 400, {"error": "unknown target"}
    if str(data.get("confirm", "")) != "DESTROY":
        return 400, {"error": "type DESTROY (capitals) to confirm"}
    if "destroy-server" not in enabled_actions(cfg):
        return 400, {"error": "the destroy action is not enrolled on this box"}
    if not verify_passphrase(cfg, data.get("passphrase")):
        return 403, {"error": "operator password required to destroy a server"}
    key = os.path.join(ACTION_KEYS, ACTIONS["destroy-server"]["key"])
    dest = cfg["targets"][target]
    job_id = "j" + os.urandom(6).hex()
    os.makedirs(JOBS_DIR, exist_ok=True)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _job_write(job_id, {"job": job_id, "action": "destroy", "target": target,
                        "status": "running", "started": started})
    audit({"action": "destroy", "target": target, "result": "STARTED", "job": job_id, "client": client})

    def run():
        log_path, _ = _job_paths(job_id)
        rc = 1
        try:
            with open(log_path, "w") as log:
                def say(m):
                    log.write(m.rstrip() + "\n"); log.flush()
                say(f"== DESTROY {target} - erasing the box ==")
                cmd = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                       "-o", "StrictHostKeyChecking=accept-new", dest, "destroy"]
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
                for line in p.stdout:
                    say(line.rstrip())
                prc = p.wait(timeout=600)
                if prc == 0:
                    say("OK the box has been erased. Now Remove it from monitoring below.")
                    rc = 0
                else:
                    say(f"-- box exit {prc}. If the box is unreachable, it cannot be wiped "
                        "remotely - use Remove from monitoring to drop it console-side. --")
        except Exception as ex:
            try:
                with open(log_path, "a") as log:
                    log.write(f"ERR {ex}\n")
            except Exception:
                pass
        fin = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _job_write(job_id, {"job": job_id, "action": "destroy", "target": target,
                            "status": "done" if rc == 0 else "failed",
                            "started": started, "finished": fin})

    threading.Thread(target=run, daemon=True).start()
    return 200, {"job": job_id}


def set_console_mode_api(data, client):
    """Promote or demote the Vantage console running ON a target box between admin and client,
    from an admin console. Gated with the operator password - the same re-entry the local
    Customize gate uses (the operator's choice); an open console has no password to ask. Runs
    the console-mode forced-command action on the box, which edits only that console's
    console_mode and restarts it. Short and synchronous; audited."""
    target = str(data.get("target", ""))
    mode = str(data.get("mode", ""))
    cfg = load_actions_config()
    if target not in ((cfg or {}).get("targets") or {}):
        return 400, {"error": "unknown target"}
    if mode not in ("admin", "client"):
        return 400, {"error": "role must be admin or client"}
    if "console-mode" not in enabled_actions(cfg):
        return 400, {"error": "this box does not accept remote role changes yet - it needs "
                     "its own Vantage console, enrolled for the console-mode action"}
    # operator-password re-entry, exactly like the local mode gate; skipped only when this
    # console runs open (there is no password to ask for)
    if auth_configured() and not verify_operator_password(data.get("passphrase", "")):
        return 403, {"error": "your operator password is required to change a box's console role"}
    key = os.path.join(ACTION_KEYS, ACTIONS["console-mode"]["key"])
    dest = cfg["targets"][target]
    cmd = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
           "-o", "StrictHostKeyChecking=accept-new", dest, f"console-mode {mode}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as ex:
        audit({"action": "console-mode", "target": target, "detail": mode,
               "result": "ERROR", "reason": str(ex)[:200], "client": client})
        return 500, {"error": f"could not reach {target}: {ex}"[:200]}
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0 or not out.startswith("OK"):
        reason = ((err or out or "the box refused the change").splitlines() or [""])[-1][:200]
        audit({"action": "console-mode", "target": target, "detail": mode,
               "result": "FAIL", "reason": reason, "client": client})
        return 502, {"error": f"the box did not change role: {reason}"}
    audit({"action": "console-mode", "target": target, "detail": mode,
           "result": "OK", "client": client})
    return 200, {"role": mode}


def set_kiosk_api(data, client):
    """Manage the boot-to-console kiosk on a target box from an admin console: install it, turn
    it on, turn it off, or read its state. The three state-changing ops are gated with the
    operator password (the same re-entry the console-role change uses); a status read is not,
    and an open console has no password to ask. Runs the kiosk forced-command action on the box,
    which installs or toggles that box's own vantage-kiosk service. Short and synchronous; the
    install can take a while on a fresh box (it fetches cage and a browser), so it gets a longer
    timeout. Audited."""
    target = str(data.get("target", ""))
    op = str(data.get("op", ""))
    cfg = load_actions_config()
    if target not in ((cfg or {}).get("targets") or {}):
        return 400, {"error": "unknown target"}
    if op not in ("install", "on", "off", "status"):
        return 400, {"error": "op must be install, on, off or status"}
    if "kiosk" not in enabled_actions(cfg):
        return 400, {"error": "this box does not accept remote kiosk control yet - re-enrol it "
                     "for the kiosk action"}
    if op != "status" and auth_configured() and not verify_operator_password(
            data.get("passphrase", "")):
        return 403, {"error": "your operator password is required to change a box's kiosk"}
    key = os.path.join(ACTION_KEYS, ACTIONS["kiosk"]["key"])
    dest = cfg["targets"][target]
    cmd = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
           "-o", "StrictHostKeyChecking=accept-new", dest, f"kiosk {op}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=600 if op == "install" else 60)
    except Exception as ex:
        audit({"action": "kiosk", "target": target, "detail": op,
               "result": "ERROR", "reason": str(ex)[:200], "client": client})
        return 500, {"error": f"could not reach {target}: {ex}"[:200]}
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if op == "status":
        if p.returncode != 0:
            reason = ((err or out or "the box did not answer").splitlines() or [""])[-1][:200]
            audit({"action": "kiosk", "target": target, "detail": "status",
                   "result": "FAIL", "reason": reason, "client": client})
            return 502, {"error": f"could not read the kiosk state: {reason}"}
        st = {}
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) == 2 and parts[0] in ("INSTALLED", "ON"):
                st[parts[0].lower()] = (parts[1].lower() == "yes")
        audit({"action": "kiosk", "target": target, "detail": "status",
               "result": "OK", "client": client})
        return 200, {"installed": st.get("installed", False), "on": st.get("on", False)}
    if p.returncode != 0 or not out.startswith("OK"):
        reason = ((err or out or "the box refused the change").splitlines() or [""])[-1][:200]
        audit({"action": "kiosk", "target": target, "detail": op,
               "result": "FAIL", "reason": reason, "client": client})
        return 502, {"error": f"the box did not change the kiosk: {reason}"}
    audit({"action": "kiosk", "target": target, "detail": op, "result": "OK", "client": client})
    return 200, {"op": op, "message": (out.splitlines() or [""])[0][:200]}


# The estate software baseline editor (Operations page). "Take current inventory" fills
# each target version from what the estate reports now (data-inv), so a new release is
# adopted in two clicks; Save writes desired.json through the setup helper.
BASELINE_JS = """
(function(){
  if(window.__blBound) return; window.__blBound=true;   // bind ONCE on document:
  // the estate page live-refreshes #main (innerHTML swap), which strips handlers bound
  // to the form itself. Delegated listeners on document survive that and re-find the
  // current form at event time.
  document.addEventListener('click',function(ev){
    var b=ev.target.closest('.bl-adopt'); if(!b) return;
    var f=document.querySelector('form.baseline-form'); if(!f) return;
    var msg=f.querySelector('.bl-msg'), inv={};
    try{inv=JSON.parse(f.dataset.inv||'{}');}catch(e){}
    var n=0;
    f.querySelectorAll('input.bl-inp').forEach(function(i){ if(inv[i.name]){ i.value=inv[i.name]; n++; } });
    if(msg){ msg.textContent = n ? ('Filled '+n+' from the versions installed now - review, then Save.')
                                  : 'No installed versions reported yet.'; msg.className='bl-msg'; }
  });
  document.addEventListener('submit',function(ev){
    var f=ev.target.closest('form.baseline-form'); if(!f) return;
    ev.preventDefault();
    var msg=f.querySelector('.bl-msg'), btn=f.querySelector('.bl-save'), comps={};
    f.querySelectorAll('input.bl-inp').forEach(function(i){ var v=i.value.trim(); if(v) comps[i.name]=v; });
    if(btn) btn.disabled=true; if(msg){ msg.textContent='Saving...'; msg.className='bl-msg'; }
    fetch('/api/setup/baseline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({components:comps})})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(res){
        if(btn) btn.disabled=false;
        if(res.ok){ if(msg){ msg.textContent='Baseline saved. Refreshing...'; msg.className='bl-msg ok'; }
          setTimeout(function(){location.reload();},700); }
        else if(msg){ msg.textContent=(res.j&&res.j.error)||'Save failed'; msg.className='bl-msg err'; }
      })
      .catch(function(){ if(btn) btn.disabled=false; if(msg){ msg.textContent='Network error'; msg.className='bl-msg err'; } });
  });
})();
"""


# 1.15.0 the Credentials panel. List is a read; each download returns a secret and, when the
# box requires it, is gated by the operator passphrase. Downloads use a data: URL on a real
# browser (the console is browsed directly, not an Artifact), so no external anything.
USB_IMPORT_JS = """
(function(){
  if(window.__uiBound) return; window.__uiBound=true;
  function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
  function fmt(n){ if(n>1073741824) return (n/1073741824).toFixed(1)+' GB'; if(n>1048576) return Math.round(n/1048576)+' MB'; return Math.max(1,Math.round(n/1024))+' KB'; }
  document.addEventListener('click',function(ev){
    var scan=ev.target.closest('.ui-scan');
    if(scan){
      var det=scan.closest('.usb-import'), msg=det.querySelector('.ui-msg'), list=det.querySelector('.ui-list');
      scan.disabled=true; msg.textContent='Scanning...'; msg.className='ui-msg'; list.innerHTML='';
      fetch('/api/usb-list',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
        .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
        .then(function(res){ scan.disabled=false;
          if(!res.ok){ msg.textContent=(res.j&&res.j.error)||'no USB found'; msg.className='ui-msg err'; return; }
          var files=res.j.files||[];
          if(!files.length){ msg.textContent='No importable files on the stick.'; msg.className='ui-msg'; return; }
          msg.textContent=files.length+' file'+(files.length>1?'s':'')+' found.'; msg.className='ui-msg ok';
          list.innerHTML=files.map(function(f){
            return '<div class=ui-row><span class=ui-name>'+esc(f.name)+'</span><span class=ui-sz>'+fmt(f.bytes)+'</span>'
              +'<button type=button class=ui-imp data-file="'+esc(f.name)+'">Import</button></div>';
          }).join('');
        }).catch(function(){ scan.disabled=false; msg.textContent='could not reach the console'; msg.className='ui-msg err'; });
      return;
    }
    var imp=ev.target.closest('.ui-imp');
    if(imp){
      var row=imp.closest('.ui-row'), det=imp.closest('.usb-import'), msg=det.querySelector('.ui-msg');
      imp.disabled=true; imp.textContent='Importing...';
      fetch('/api/usb-import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:imp.dataset.file})})
        .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
        .then(function(res){
          if(res.ok){ imp.textContent='Imported'; row.classList.add('done'); msg.textContent=esc(res.j.imported)+' -> '+esc(res.j.where); msg.className='ui-msg ok'; }
          else { imp.disabled=false; imp.textContent='Import'; msg.textContent=(res.j&&res.j.error)||'import failed'; msg.className='ui-msg err'; }
        }).catch(function(){ imp.disabled=false; imp.textContent='Import'; msg.textContent='could not reach the console'; msg.className='ui-msg err'; });
      return;
    }
    var tog=ev.target.closest('.ca-toggle');
    if(tog){
      var want=tog.dataset.on!=='1';
      tog.disabled=true;
      fetch('/api/software/current',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({file:tog.dataset.file, current:want})})
        .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
        .then(function(res){ tog.disabled=false;
          if(res.ok){ tog.dataset.on=want?'1':'0'; tog.textContent=want?'Current \\u2713':'Make current'; tog.classList.toggle('on',want); }
          else { alert((res.j&&res.j.error)||'failed'); }
        }).catch(function(){ tog.disabled=false; alert('could not reach the console'); });
    }
  });
})();
"""


MODULE_OFFLINE_JS = """
(function(){
  if(window.__moBound) return; window.__moBound=true;
  document.addEventListener('change',function(ev){
    var inp=ev.target.closest('.mo-file'); if(!inp||!inp.files.length) return;
    var det=inp.closest('.mod-offline'), body=det.querySelector('.mo-body');
    var prog=body.querySelector('.mo-prog'), verEl=body.querySelector('.mo-verinp');
    var f=inp.files[0], module=det.dataset.module, version=(verEl?verEl.value.trim():'');
    prog.textContent='Uploading '+f.name+' 0%'; prog.className='mo-prog';
    var x=new XMLHttpRequest(); x.open('POST','/api/library/upload-image');
    x.setRequestHeader('X-Filename', f.name);
    x.setRequestHeader('X-Module', module);
    x.setRequestHeader('X-Version', version);
    x.upload.onprogress=function(e){ if(e.lengthComputable) prog.textContent='Uploading '+f.name+' '+Math.round(e.loaded/e.total*100)+'%'; };
    x.onload=function(){
      if(x.status===200){ prog.textContent='Uploaded. Refreshing...'; prog.className='mo-prog ok'; setTimeout(function(){location.reload();},900); }
      else { var m='upload failed'; try{m=JSON.parse(x.responseText).error||m;}catch(e){} prog.textContent=m; prog.className='mo-prog err'; }
    };
    x.onerror=function(){ prog.textContent='network error'; prog.className='mo-prog err'; };
    x.send(f); inp.value='';
  });
  document.addEventListener('click',function(ev){
    var b=ev.target.closest('.mo-deploy'); if(!b) return;
    var det=b.closest('.mod-offline'), body=det.querySelector('.mo-body');
    var prog=body.querySelector('.mo-prog'), logEl=body.querySelector('.mo-log');
    b.disabled=true; prog.textContent='Starting offline deploy...'; prog.className='mo-prog';
    fetch('/api/module/load-offline',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({target:b.dataset.target, module:b.dataset.module})})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(res){
        if(!res.ok){ b.disabled=false; prog.textContent=(res.j&&res.j.error)||'failed'; prog.className='mo-prog err'; return; }
        var job=res.j.job; prog.textContent='Deploying (streaming the tarball, then docker load)...';
        var poll=setInterval(function(){
          fetch('/api/job/'+job).then(function(r){return r.json();}).then(function(j){
            if(!j) return;
            logEl.textContent=j.log||''; logEl.scrollTop=logEl.scrollHeight;
            if(j.status!=='running'){ clearInterval(poll); b.disabled=false;
              if(j.status==='done'){ prog.textContent='Deployed offline'; prog.className='mo-prog ok'; }
              else { prog.textContent='Deploy failed - see the log below'; prog.className='mo-prog err'; }
            }
          }).catch(function(){});
        },2000);
      })
      .catch(function(){ b.disabled=false; prog.textContent='could not reach the console'; prog.className='mo-prog err'; });
  });
})();
"""


BULK_ENROL_JS = """
(function(){
  if(window.__bulkBound) return; window.__bulkBound=true;
  function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
  document.addEventListener('submit',function(ev){
    var f=ev.target.closest('form.bulk-form'); if(!f) return;
    ev.preventDefault();
    var box=f.dataset.box, msg=f.querySelector('.bulk-msg');
    var out=f.parentNode.querySelector('.bulk-out');
    var group=f.querySelector('.bulk-group').value.trim();
    var names=f.querySelector('.bulk-list').value.split('\\n').map(function(s){return s.trim();}).filter(Boolean);
    var passEl=f.querySelector('.bulk-pass');
    var body={target:box, group:group};
    if(passEl) body.passphrase=passEl.value;
    if(names.length){ body.users=names; }
    else { body.prefix=f.querySelector('.bulk-prefix').value.trim();
           body.count=parseInt(f.querySelector('.bulk-count').value,10);
           body.start=parseInt(f.querySelector('.bulk-start').value,10)||1; }
    if(!group){ msg.textContent='Give the devices a group.'; msg.className='bulk-msg err'; return; }
    var btn=f.querySelector('button[type=submit]'); btn.disabled=true;
    msg.textContent='Enrolling and building the sheet... (a few seconds per device)'; msg.className='bulk-msg';
    fetch('/api/enrol-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(res){
        btn.disabled=false;
        if(!res.ok){ msg.textContent=(res.j&&res.j.error)||'Enrolment failed'; msg.className='bulk-msg err'; return; }
        var j=res.j;
        msg.textContent=j.count+' of '+j.attempted+' enrolled.'; msg.className='bulk-msg ok';
        var h='<div class=bulk-dl>';
        h+='<a class=a-go download="'+esc(box)+'-qr-sheet.pdf" href="data:application/pdf;base64,'+j.pdf_b64+'">Download QR sheet (PDF)</a>';
        h+='<a class=cred-refresh download="'+esc(box)+'-enrolment.zip" href="data:application/zip;base64,'+j.zip_b64+'">Download all (ZIP)</a>';
        h+='<button type=button class="cred-refresh bulk-usb" data-box="'+esc(box)+'">Copy to USB stick</button></div>';
        if(j.errors && j.errors.length){
          h+='<div class=bulk-errs>Could not enrol: '+j.errors.map(function(x){return esc(x.user)+' ('+esc(x.error)+')';}).join(', ')+'</div>';
        }
        h+='<div class=bulk-grid>';
        (j.devices||[]).forEach(function(d){
          h+='<figure class=bulk-cell><img alt="QR for '+esc(d.user)+'" src="data:image/png;base64,'+d.png+'"><figcaption>'+esc(d.user)+'</figcaption></figure>';
        });
        out.innerHTML=h+'</div>'; out.__pdf=j.pdf_b64; out.__zip=j.zip_b64;
      })
      .catch(function(){ btn.disabled=false; msg.textContent='Could not reach the console'; msg.className='bulk-msg err'; });
  });
  document.addEventListener('click',function(ev){
    var b=ev.target.closest('.bulk-usb'); if(!b) return;
    var out=b.closest('.bulk-out'); if(!out||!out.__pdf) return;
    b.disabled=true; var old=b.textContent; b.textContent='Copying...';
    fetch('/api/usb-copy',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({box:b.dataset.box, pdf_b64:out.__pdf, zip_b64:out.__zip})})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(res){ b.disabled=false;
        if(res.ok){ b.textContent='Copied to '+(res.j.dest||'USB'); }
        else { b.textContent=old; alert((res.j&&res.j.error)||'No USB stick found. Insert one, wait a moment, and try again.'); }
      }).catch(function(){ b.disabled=false; b.textContent=old; alert('Could not reach the console'); });
  });
})();
"""


# The loadout editor: tick the modules a box carries. The boxes feed a hidden `components`
# field the generic action submit reads, so the existing set-loadout action does the work.
LOADOUT_EDITOR_JS = """
(function(){
  if(window.__loBound) return; window.__loBound=true;
  function sync(form){
    var hidden=form.querySelector('input[name=components]'); if(!hidden) return;
    var vals=[];
    form.querySelectorAll('.lo-box').forEach(function(x){ if(x.checked) vals.push(x.value); });
    if(vals.indexOf('takserver')<0) vals.unshift('takserver');   // always required
    hidden.value=vals.join(',');
  }
  document.addEventListener('change',function(ev){
    var b=ev.target; if(!(b.classList&&b.classList.contains('lo-box'))) return;
    var form=b.closest('form.loadout-form'); if(form) sync(form);
  });
})();
"""


CONSOLE_MODE_JS = """
(function(){
  if(window.__cmBound) return; window.__cmBound=true;   // bind ONCE on document (estate live-refreshes #main)
  document.addEventListener('submit',function(ev){
    var f=ev.target.closest('form.conroleform'); if(!f) return;
    ev.preventDefault();
    var res=f.querySelector('.cr-res');
    var mode=f.querySelector('.cr-mode').value;
    var passEl=f.querySelector('.cr-pass');
    var target=f.getAttribute('data-name');
    if(passEl && !passEl.value){ res.textContent='Enter your operator password.'; res.className='cr-res lib-status err'; return; }
    res.textContent='Setting\\u2026'; res.className='cr-res lib-status';
    var body={target:target, mode:mode};
    if(passEl) body.passphrase=passEl.value;
    fetch('/api/console/set-mode',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)}).then(function(x){return x.json().then(function(j){return{code:x.status,j:j};});})
    .then(function(x){
      if(x.code===200){ res.textContent='Set to '+x.j.role+'.'; res.className='cr-res lib-status ok'; if(passEl)passEl.value=''; }
      else{ res.textContent=x.j.error||'failed'; res.className='cr-res lib-status err'; }
    }).catch(function(){ res.textContent='could not reach the console'; res.className='cr-res lib-status err'; });
  });
})();
"""


KIOSK_JS = """
(function(){
  if(window.__kioskBound) return; window.__kioskBound=true;   // bind ONCE on document (estate live-refreshes #main)
  document.addEventListener('click',function(ev){
    var b=ev.target.closest('button.kioskbtn'); if(!b) return;
    var f=b.closest('form.kioskform'); if(!f) return;
    ev.preventDefault();
    var op=b.getAttribute('data-op');
    var res=f.querySelector('.kk-res');
    var passEl=f.querySelector('.kk-pass');
    var target=f.getAttribute('data-name');
    if(op!=='status' && passEl && !passEl.value){ res.textContent='Enter your operator password.'; res.className='kk-res lib-status err'; return; }
    res.textContent=(op==='status'?'Checking\\u2026':'Working\\u2026'); res.className='kk-res lib-status';
    var body={target:target, op:op};
    if(op!=='status' && passEl) body.passphrase=passEl.value;
    fetch('/api/console/kiosk',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)}).then(function(x){return x.json().then(function(j){return{code:x.status,j:j};});})
    .then(function(x){
      if(x.code!==200){ res.textContent=(x.j&&x.j.error)||'failed'; res.className='kk-res lib-status err'; return; }
      if(op==='status'){
        res.textContent='Kiosk '+(x.j.installed?'installed':'not installed')+', '+(x.j.on?'on now':'off')+'.';
      } else {
        res.textContent=x.j.message||'Done.'; if(passEl)passEl.value='';
      }
      res.className='kk-res lib-status ok';
    }).catch(function(){ res.textContent='could not reach the console'; res.className='kk-res lib-status err'; });
  });
})();
"""


DESTROY_SERVER_JS = """
(function(){
  if(window.__dsBound) return; window.__dsBound=true;
  document.addEventListener('submit',function(ev){
    var f=ev.target.closest('form.destroyform'); if(!f) return;
    ev.preventDefault();
    var res=f.querySelector('.destroy-res'), logEl=f.querySelector('.destroy-log');
    var word=f.querySelector('.destroy-word').value.trim();
    var pass=f.querySelector('.destroy-pass').value;
    if(word!=='DESTROY'){ res.textContent='Type DESTROY (capitals) to confirm.'; res.className='destroy-res lib-status err'; return; }
    if(!confirm('This ERASES the estate footprint from '+f.dataset.name+' - TAK Server, its database and certificates, CloudTAK, the vault. It cannot be undone. Continue?')) return;
    var btn=f.querySelector('button[type=submit]'); btn.disabled=true;
    res.textContent='Destroying...'; res.className='destroy-res lib-status';
    fetch('/api/destroy',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({target:f.dataset.name, confirm:word, passphrase:pass})})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(out){
        if(!out.ok){ btn.disabled=false; res.textContent=(out.j&&out.j.error)||'failed'; res.className='destroy-res lib-status err'; return; }
        var job=out.j.job;
        var poll=setInterval(function(){
          fetch('/api/job/'+job).then(function(r){return r.json();}).then(function(j){
            if(!j) return;
            logEl.textContent=j.log||''; logEl.scrollTop=logEl.scrollHeight;
            if(j.status!=='running'){ clearInterval(poll); btn.disabled=false;
              if(j.status==='done'){ res.textContent='Box erased. Remove it from monitoring below.'; res.className='destroy-res lib-status ok'; }
              else { res.textContent='Did not complete - see the log.'; res.className='destroy-res lib-status err'; }
            }
          }).catch(function(){});
        },2000);
      }).catch(function(){ btn.disabled=false; res.textContent='could not reach the console'; res.className='destroy-res lib-status err'; });
  });
})();
"""


CREDENTIALS_JS = """
(function(){
  var sec=document.querySelector('section.creds'); if(!sec) return;
  var box=sec.dataset.box, needpass=sec.dataset.needpass==='1';
  var listEl=sec.querySelector('.cred-list'), viewEl=sec.querySelector('.cred-view'),
      refresh=sec.querySelector('.cred-refresh'), passInp=sec.querySelector('.cred-passinp');
  function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
  function dl(name, mime, b64){
    var a=document.createElement('a');
    a.href='data:'+mime+';base64,'+b64; a.download=name;
    document.body.appendChild(a); a.click(); a.remove();
  }
  function post(aid, inputs){
    var body={target:box, inputs:inputs, confirm:true};
    if(needpass && passInp) body.passphrase=passInp.value;
    return fetch('/api/action/'+aid,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)}).then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});});
  }
    function show(h){viewEl.innerHTML=h;viewEl.scrollIntoView({block:'nearest',behavior:'smooth'});}
  function fetchCred(c){
    if(needpass && passInp && !passInp.value){
      show('<div class="a-res error">Enter the operator passphrase (the box next to '
        +'List credentials), then press Download again.</div>');
      passInp.focus();passInp.scrollIntoView({block:'center',behavior:'smooth'});return;}
    show('<div class="a-res">Fetching '+esc(c.name)+'\\u2026</div>');
    post('fetch-credential',{ctype:c.ctype,name:c.name}).then(function(x){
      if(x.code!==200){show('<div class="a-res error">'+esc(x.j.message||x.j.error||'failed')+'</div>');return;}
      var j=x.j;
      if(j.ctype==='cert'){
        dl(j.file||(c.name+'.p12'), 'application/x-pkcs12', j.p12);
        show('<div class="a-res ok">Downloaded '+esc(j.file||c.name)+'. Import it with its password (issued when the cert was made).</div>');
      } else {
        var html='<div class=cred-enrol><div class="a-res ok">Enrolment for '+esc(j.name)+'</div>';
        if(j.png) html+='<img alt="Enrolment QR for '+esc(j.name)+'" class=cred-qr src="data:image/png;base64,'+j.png+'">';
        html+='<div class=cred-lines><code>'+esc(j.itak||'')+'</code></div>'
            +'<div class=cred-btns><button type=button class=cred-savqr>Save QR</button>'
            +'<button type=button class=cred-savitak>Save iTAK line</button></div></div>';
        show(html);
        var q=viewEl.querySelector('.cred-savqr'); if(q&&j.png) q.onclick=function(){dl(j.name+'-enrol-qr.png','image/png',j.png);};
        var it=viewEl.querySelector('.cred-savitak'); if(it) it.onclick=function(){dl(j.name+'-itak.txt','text/plain',btoa(j.itak||''));};
      }
    }).catch(function(e){viewEl.innerHTML='<div class="a-res error">Could not reach the console. '+e+'</div>';});
  }
  function render(creds){
    if(!creds.length){listEl.innerHTML='<div class=cred-empty>No credentials issued on this box yet. Use <b>Add a device</b> above to create the first one.</div>';return;}
    var rows=creds.map(function(c){
      var meta=c.ctype==='cert'?('certificate \\u00b7 expires '+esc(c.expires||'?')):('enrolment \\u00b7 group '+esc(c.group||'?'));
      return '<div class=cred-row><span class=cred-name>'+esc(c.name)+'</span>'
        +'<span class=cred-meta>'+meta+'</span>'
        +'<button type=button class=cred-dl data-i="'+esc(JSON.stringify(c))+'">Download</button></div>';
    }).join('');
    listEl.innerHTML=rows;
    listEl.querySelectorAll('.cred-dl').forEach(function(b){
      b.onclick=function(){fetchCred(JSON.parse(b.dataset.i));};
    });
  }
  refresh.onclick=function(){
    listEl.innerHTML='<div class=cred-empty>Listing\\u2026</div>';
    post('list-credentials',{}).then(function(x){
      if(x.code!==200){listEl.innerHTML='<div class="a-res error">'+esc(x.j.message||x.j.error||'failed')+'</div>';return;}
      render(x.j.credentials||[]);
    }).catch(function(e){listEl.innerHTML='<div class="a-res error">Could not reach the console. '+e+'</div>';});
  };
  refresh.onclick();
})();
"""


def render_server(state, name):
    """One box, whole story: health, history, inventory vs baseline, and its actions
    pre-bound so nothing on this page asks which box you mean."""
    e = html.escape
    t = next((x for x in state.get("targets", []) if x.get("name") == name), None)
    if t is None:
        return None
    age = age_seconds(state.get("generated_at", ""))
    stale = age is None or age > STALE_AFTER
    history = load_history()
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    act_targets = set((cfg or {}).get("targets", {}).keys())
    desired = load_desired()
    res = t.get("result", "UNKNOWN")
    label = t.get("label", name)
    checks = t.get("checks") or []

    doc = page_head(f"{label} — " + load_instance()["product_name"])
    doc.append(header_html(state, res, age, f"server:{name}", crumb=e(label)))
    line = (f"{label}: {res}. Checker {t.get('checker_version') or '?'}. "
            f"Checked {human_age(age_seconds(t.get('checked_at', '')))}.")
    doc.append(f"<div class='vband {e(res)}'><span class=vdot></span>{e(line)}</div>")
    doc.append("<main id=main class=wrap>")
    doc.append(f"<div class=rename-row><form id=renameform data-name='{e(name)}'>"
               f"<label class=fl>Display name<input id=rename-label maxlength=40 "
               f"value='{e(label)}'></label><button class=cred-refresh type=submit>"
               "Rename</button><span id=rename-res class=lib-status role=status></span>"
               "</form></div>")
    doc.append("""<script>(function(){
var f=document.getElementById('renameform'); if(!f)return;
f.addEventListener('submit',function(ev){ev.preventDefault();
  var r=document.getElementById('rename-res');r.textContent='Saving\u2026';
  fetch('/api/setup/rename',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:f.dataset.name,label:document.getElementById('rename-label').value.trim()})})
  .then(function(x){return x.json().then(function(j){return {code:x.status,j:j};});})
  .then(function(x){
    if(x.code===200){r.textContent='Renamed. Updating\u2026';setTimeout(function(){location.reload();},2500);}
    else{r.textContent=x.j.error||'failed';}
  }).catch(function(){r.textContent='could not reach the console';});
});})();</script>""")
    # Console role: promote/demote the Vantage console running ON this box, from here. Only
    # when the box accepts it (its own console, enrolled for console-mode). A normal control,
    # not a danger zone - it is reversible - but gated with the operator password.
    if "console-mode" in acts and name in act_targets:
        pw = ("<label class=cred-pass><span>Operator password</span>"
              "<input type=password class=cr-pass autocomplete=off></label>"
              if auth_configured() else "")
        doc.append(
            "<h2 class=sec-eye>Console role</h2>"
            f"<div class=conrole><form class=conroleform data-name='{e(name)}'>"
            "<p class=meta>This box runs its own Vantage console. Set whether it acts as an "
            "<b>admin</b> console (full estate control) or a <b>client</b> (watch and operate, "
            "never reshape the estate). The change is applied on that box and restarts its "
            "console. Do not leave the estate with no admin.</p>"
            "<label class=fl>Role<select class=cr-mode>"
            "<option value=client>client - watch and operate</option>"
            "<option value=admin>admin - full estate control</option>"
            "</select></label>" + pw
            + f"<button type=submit class='a-go confirm'>Set role for {e(label)}</button>"
            "<span class='cr-res lib-status' role=status style='margin-left:10px'></span>"
            "</form></div>")
        doc.append(f"<script>{CONSOLE_MODE_JS}</script>")
    # Kiosk: the box's own screen showing its console. Added after an install, then turned on
    # or off from here. Only when the box accepts it (enrolled for the kiosk action). A normal
    # control - reversible - but the state-changing buttons are gated with the operator password.
    if "kiosk" in acts and name in act_targets:
        pw = ("<label class=cred-pass><span>Operator password</span>"
              "<input type=password class=kk-pass autocomplete=off></label>"
              if auth_configured() else "")
        doc.append(
            "<h2 class=sec-eye>Kiosk (screen)</h2>"
            f"<div class=conrole><form class=kioskform data-name='{e(name)}'>"
            "<p class=meta>For a box with a screen attached. <b>Install</b> lays the kiosk down "
            "(cage and a browser) and turns it on; the screen then shows this box's own console. "
            "<b>Turn on</b> and <b>Turn off</b> toggle it afterwards - off hands the screen back "
            "to a normal text login. <b>Check</b> reads the current state. A fresh install fetches "
            "packages and can take a minute.</p>" + pw
            + "<div class=kk-btns style='display:flex;gap:8px;flex-wrap:wrap;margin:8px 0'>"
            f"<button type=button class='a-go kioskbtn' data-op=status>Check {e(label)}</button>"
            "<button type=button class='a-go kioskbtn confirm' data-op=install>Install kiosk</button>"
            "<button type=button class='a-go kioskbtn confirm' data-op=on>Turn on</button>"
            "<button type=button class='a-go kioskbtn confirm' data-op=off>Turn off</button>"
            "</div>"
            "<span class='kk-res lib-status' role=status style='margin-left:10px'></span>"
            "</form></div>")
        doc.append(f"<script>{KIOSK_JS}</script>")
    # Destroy: erase the estate footprint from the box. The most destructive action - its own
    # danger zone, a typed DESTROY plus the operator password, and it needs the box reachable.
    # The wipe streams back as the confirmation. Unreachable? Use Remove from monitoring below.
    if "destroy-server" in acts and name in act_targets:
        doc.append(
            "<details class=dangerzone><summary>Destroy this server (wipe the box)</summary>"
            f"<form class=destroyform data-name='{e(name)}'>"
            "<p class=meta>Irreversibly <b>erase the estate footprint</b> from the box: TAK "
            "Server, its certificates and database, the CloudTAK/MediaMTX stack and volumes, and "
            "the vault. This runs on the box, so the box must be reachable; the wipe streams back "
            "here as confirmation. If the box is gone or unreachable, use <b>Remove this "
            "server</b> below instead (console-side only).</p>"
            "<label class=cred-pass><span>Type DESTROY to confirm</span>"
            "<input class=destroy-word autocomplete=off spellcheck=false placeholder='DESTROY'></label>"
            "<label class=cred-pass><span>Operator password</span>"
            "<input type=password class=destroy-pass autocomplete=off></label>"
            f"<button type=submit class='a-go confirm'>Destroy {e(label)}</button>"
            "<span class='destroy-res lib-status' role=status style='margin-left:10px'></span>"
            "<pre class='deplog destroy-log'></pre></form></details>")
        doc.append(f"<script>{DESTROY_SERVER_JS}</script>")
    # Remove a server that was deleted, destroyed or reset. Console-side only (works even
    # if the box is gone), passphrase-gated. In its own collapsed danger zone so it is
    # deliberate, never a mis-click.
    if acts:
        doc.append("<details class=dangerzone><summary>Remove this server</summary>"
                   f"<form id=unenrolform data-name='{e(name)}'>"
                   "<p class=meta>Stop monitoring this server and unbind its actions. Use "
                   "this when the box has been deleted, destroyed or reset. Nothing is done "
                   "to the box itself, so it works whether or not it still exists. To "
                   "re-add it later, enrol it again.</p>"
                   "<label class=cred-pass><span>Operator password</span>"
                   "<input type=password id=unenrol-pass autocomplete=off></label>"
                   f"<button type=submit class='a-go confirm'>Remove {e(label)} from "
                   "monitoring</button><span id=unenrol-res class=lib-status role=status "
                   "style='margin-left:10px'></span></form></details>")
        doc.append("""<script>(function(){
var f=document.getElementById('unenrolform'); if(!f)return;
f.addEventListener('submit',function(ev){ev.preventDefault();
  var r=document.getElementById('unenrol-res');
  if(!confirm('Remove this server from monitoring? Actions bound to it are unbound. '
    +'The box itself is not touched.'))return;
  r.textContent='Removing\\u2026';
  fetch('/api/setup/unenrol',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:f.dataset.name,passphrase:document.getElementById('unenrol-pass').value})})
  .then(function(x){return x.json().then(function(j){return {code:x.status,j:j};});})
  .then(function(x){
    if(x.code===200){r.textContent='Removed. Returning to Overview\\u2026';
      setTimeout(function(){location.href='/';},1500);}
    else{r.textContent=x.j.error||'failed';}
  }).catch(function(){r.textContent='could not reach the console';});
});})();</script>""")
    doc.append(stale_banner(age, stale))
    if t.get("unconfirmed"):
        doc.append("<div class='banner drift'><b>Confirming.</b><span>"
                   f"{e(t.get('note', 'a check failed once'))}. A single transient (a public "
                   "box's accept queue briefly overflowing under scanner load, say) does not "
                   "turn the estate red; a real fault fails the next poll too.</span></div>")
    if t.get("error"):
        doc.append(f"<div class='banner stale'><b>Error.</b><span>{e(t['error'])}</span></div>")
    if res in ("OFFLINE", "UNREACHABLE"):
        seen = last_seen(history, name)
        ago = human_age(age_seconds(seen)) if seen else "not in recent history"
        doc.append(f"<div class='banner drift'><b>Last seen.</b><span>{e(ago)}"
                   + (f" &middot; {e(t['note'])}" if t.get("note") else "") + "</span></div>")

    flip = changed_since_last(history, name)
    meta = (f"{t.get('fqdn') or t.get('profile') or ''} &middot; profile "
            f"{t.get('profile') or '?'} &middot; last poll {t.get('elapsed_s', '?')}s"
            + (f" &middot; {flip}" if flip else ""))
    doc.append(f"<div class=meta style='margin-top:14px'>{meta}</div>")

    strip = uptime_strip(history, name, 48)
    if strip:
        doc.append(f"<h2 class=sec-eye>Recent polls</h2>{strip}"
                   "<div class=striplab><span>older</span><span>now</span></div>")

    bad = [c for c in checks if c.get("status") in ("FAIL", "WARN")]
    if bad:
        bad.sort(key=lambda c: 0 if c.get("status") == "FAIL" else 1)
        doc.append("<h2 class=sec-eye>Needs attention</h2><div class=rows>")
        for c in bad:
            fix = ""
            rec = recovery_for(c)
            if rec and name in act_targets:
                aid, ins = rec
                if aid in acts and action_applies(aid, t):
                    attrs = " ".join(f"data-fix-{k}='{e(str(v))}'" for k, v in ins.items())
                    fix = (f"<a class=fixlink href='#act-{e(aid)}' data-fix-action='{e(aid)}' {attrs}>"
                           f"Propose fix<span class=sr-only>: {e(ACTIONS[aid]['label'])}</span></a>")
            doc.append(f"<div class='row {e(c['status'])}'><span class=st>{e(c['status'])}</span>"
                       f"<span class=nm>{e(c.get('category', ''))} &rsaquo; {e(c.get('name', ''))}</span>"
                       f"<span class=dt>{e(c.get('detail', ''))}{fix}</span></div>")
        doc.append("</div>")

    keys = sorted({f"{c.get('category', '')}/{c.get('name', '')}" for c in checks
                   if isinstance(c.get('value'), (int, float))})
    unit_of = {f"{c.get('category', '')}/{c.get('name', '')}": c.get("unit", "")
               for c in checks if isinstance(c.get('value'), (int, float))}
    mcards = []
    for k in keys:
        vals = series_for(history, name, k)
        if not vals:
            v = next((c["value"] for c in checks
                      if f"{c.get('category', '')}/{c.get('name', '')}" == k), None)
            vals = [v, v] if isinstance(v, (int, float)) else []
        if not vals:
            continue
        cur, prev = vals[-1], (vals[-2] if len(vals) > 1 else vals[-1])
        ar = "<span class='ar up'>↑</span>" if cur > prev else (
             "<span class='ar dn'>↓</span>" if cur < prev else "")
        mcards.append(f"<div class=metric><div class=ml>{e(k.split('/', 1)[-1])}</div>"
                      f"<div class=mv>{e(str(cur))}<u>{e(unit_of.get(k, ''))}</u>{ar}</div>"
                      f"{sparkline(vals)}</div>")
    if mcards:
        doc.append(f"<h2 class=sec-eye>Metrics</h2><div class=metrics>{''.join(mcards)}</div>")

    software = software_rows(t)
    if software:
        doc.append("<h2 class=sec-eye>Software</h2><div class=swlist>")
        for s in software:
            comp, ver = s.get("name", "?"), (s.get("version") or "")
            want = (desired or {}).get(comp)
            cell = ""
            if want and not version_current(ver, want):
                aid = COMPONENT_ACTION.get(comp)
                cell = f"<span class=sw-drift>→ {e(want)}</span>"
                if aid in acts and name in act_targets:
                    cell += (f"<a class=sw-up href='#act-{e(aid)}'>Update"
                             f"<span class=sr-only> {e(comp)} on {e(label)}</span></a>")
            elif want:
                cell = "<span class=sw-cur>current</span>"
            doc.append(f"<div class=swrow><span class=sw-n>{e(comp)}</span>"
                       f"<span class=sw-v>{e(ver) if ver else '–'}</span>"
                       f"<span class=sw-s>{e(s.get('state', ''))}</span>"
                       f"<span class=sw-a>{cell}</span></div>")
        doc.append("</div>")
    elif t.get("reachable"):
        doc.append("<h2 class=sec-eye>Software</h2><div class=meta>No inventory reported. "
                   "This box's checker predates 1.1.0; run Update health checker below.</div>")

    # 1.32.0 Modules: the per-box marketplace. Each installable component as a card:
    # its state from the health inventory, Update when behind the baseline, Install
    # (the provisioner's components stage, as a gated job) when absent.
    MODULES = [
        ("takserver", "TAK Server", "the server itself - installed by the Deploy wizard"),
        ("cloudtak", "CloudTAK", "web map and missions stack (Docker, pinned; needs internet or a staged image)"),
        ("mediamtx", "MediaMTX", "video relay for drone and camera feeds (pinned fetch; needs internet)"),
        ("maps", "Offline maps", "mbtileserver tile service (pinned fetch; needs internet). Tiles arrive from the Store shelf afterwards"),
        ("mosquitto", "Mosquitto MQTT", "loopback broker for box integrations (distribution package; needs internet or an apt mirror)"),
        ("nodered", "Node-RED", "flows as a pinned container on loopback (needs internet, or a staged image via Offline deploy)"),
        ("ollama", "Ollama", "local models on loopback (needs internet: the archive is ~1.4GB; models pulled deliberately later)"),
        ("takbot", "TAKBOT", "CoT chat bot; needs the MilUX-vendored artefact staged from the Store first"),
        ("lanntp", "LAN time (NTP)", "chrony serves time to the kit LAN so device clocks hold offline (no internet needed)"),
    ]
    # Inventory rows are named by SERVICE; three module tokens differ, and for the
    # shared-substrate ones presence is not fittedness (chrony exists on every
    # Ubuntu box without serving the LAN). The declared loadout, where one exists,
    # is the authority on what counts as fitted.
    SW_ALIAS = {"nodered": "node-red", "maps": "mbtileserver", "lanntp": "chrony"}
    lo = t.get("loadout") or {}
    declared_set = set(x for x in str(lo.get("declared") or "").split(",") if x)
    lo_declared = bool(declared_set) and str(lo.get("source") or "") == "loadout.conf"
    sw_by = {r.get("name"): r for r in software}
    box_fqdn = str(t.get("fqdn") or "")
    _pki = load_instance()
    can_install = ("provision-server" in acts and name in act_targets
                   and re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+",
                                    box_fqdn or "") is not None)
    cards = []
    for comp, clabel, cdesc in MODULES:
        row = sw_by.get(comp) or sw_by.get(SW_ALIAS.get(comp, ""))
        if lo_declared and comp != "takserver" and comp not in declared_set:
            # Declared not-fitted beats an inventory row: chrony existing is not
            # lan-ntp, and a module the operator declared away offers Install.
            row = None
        if row:
            ver = row.get("version") or "present"
            st = row.get("state") or ""
            want = (desired or {}).get(comp)
            badge = ("<span class='mod-b ok'>installed</span>"
                     if not want or version_current(row.get("version") or "", want)
                     else "<span class='mod-b drift'>update available</span>")
            aid = COMPONENT_ACTION.get(comp)
            act = (f"<a class=mod-act href='#act-{e(aid)}'>Update</a>"
                   if want and not version_current(row.get("version") or "", want)
                   and aid in acts and name in act_targets else "")
            offline = ""
            if comp in ("cloudtak", "mediamtx"):
                img = library_image_for(comp, want)
                can_off = "load-images" in acts and name in act_targets
                istat = (f"image in library: <code>{e(img['file'])}</code>"
                         + (f" ({e(img.get('version',''))})" if img.get("version") else "")
                         if img else "no image tarball held yet")
                dep = (f"<button type=button class=mo-deploy data-module='{e(comp)}' "
                       f"data-target='{e(name)}'>Deploy offline (docker load)</button>"
                       if img and can_off else "")
                offline = (
                    f"<details class=mod-offline data-module='{e(comp)}' data-target='{e(name)}'>"
                    "<summary>Offline deploy</summary><div class=mo-body>"
                    "<p class=meta>Deploy with no internet: hold this module's Docker images on "
                    "the admin box, then load them onto the box over the closed network. Capture "
                    "on a connected machine with <code>docker save -o " + e(comp) + "-images.tar &lt;images&gt;</code>.</p>"
                    f"<div class=mo-status>{istat}</div>"
                    f"<div class=mo-actions>{dep}"
                    "<label class=mo-upload>Upload image tarball"
                    "<input type=file class=mo-file accept='.tar,.gz'></label>"
                    f"<label class=mo-ver>version<input class=mo-verinp value=\"{e(want or '')}\" "
                    "placeholder='e.g. v13.70.0' spellcheck=false></label></div>"
                    "<div class=mo-prog role=status></div><div class=mo-log></div>"
                    "</div></details>")
            cards.append(f"<div class=mod><div class=mod-h><b>{e(clabel)}</b>{badge}</div>"
                         f"<div class=mod-d>{e(ver)}{(' · ' + e(st)) if st else ''}</div>{act}{offline}</div>")
        elif comp == "takserver":
            cards.append(f"<div class=mod><div class=mod-h><b>{e(clabel)}</b>"
                         "<span class='mod-b abs'>not installed</span></div>"
                         "<div class=mod-d>installed by the <a href='/deploy'>Deploy wizard</a>, "
                         "never from here</div></div>")
        else:
            inst = ""
            if can_install and sw_by.get("takserver"):
                inst = ("<form class=action data-id=provision-server data-pass=0 data-read=0 "
                        f"data-confirm=\"Install {e(clabel)} on {{target}}. Software is installed "
                        "and services start on that box.\" data-result=text>"
                        f"<input type=hidden name=target value='{e(name)}'>"
                        "<input type=hidden name=stage value=components>"
                        f"<input type=hidden name=components value='{e(comp)}'>"
                        f"<input type=hidden name=fqdn value='{e(box_fqdn)}'>"
                        f"<input type=hidden name=le_email value='ops@{e(box_fqdn)}'>"
                        f"<input type=hidden name=org value='{e(_pki.get('org') or 'Vantage')}'>"
                        f"<input type=hidden name=org_unit value='{e(_pki.get('org_unit') or 'TAK')}'>"
                        f"<input type=hidden name=country value='{e(_pki.get('country') or 'GB')}'>"
                        f"<input type=hidden name=state value='{e(_pki.get('state') or 'England')}'>"
                        f"<input type=hidden name=city value='{e(_pki.get('city') or 'Andover')}'>"
                        "<input type=hidden name=deb value='/root/unused.deb'>"
                        "<input type=hidden name=dry_run value='0'>"
                        "<div class=a-act><button class=a-go type=submit>Install</button></div>"
                        "<div class='a-res' role=status aria-live=polite></div></form>")
            if not inst and "provision-server" in acts and name in act_targets \
               and sw_by.get("takserver"):
                inst = ("<div class=meta>To install modules, give this box its public "
                        "DNS name first - the rename control at the top of this page.</div>")
            cards.append(f"<div class=mod><div class=mod-h><b>{e(clabel)}</b>"
                         "<span class='mod-b abs'>not installed</span></div>"
                         f"<div class=mod-d>{e(cdesc)}</div>{inst}</div>")
    doc.append("<h2 class=sec-eye>Modules</h2><div class=mods>" + "".join(cards) + "</div>")
    # Spec 002: the declared loadout, visible beside the modules it governs, and
    # the gated editor where the action is enabled for this box.
    if lo_declared:
        doc.append(f"<div class=meta>Declared loadout: <code>{e(str(lo.get('declared')))}</code>"
                   " - health checks judge exactly this set; everything else reads"
                   " “not fitted (declared)” on the checks above.</div>")
    elif lo:
        doc.append(f"<div class=meta>Loadout: {e(str(lo.get('source') or 'profile defaults'))}"
                   " - the profile's full expectations are in force.</div>")
    if "set-loadout" in acts and name in act_targets:
        # tick what the box carries. Start from the current declaration; if none has been
        # made, pre-tick what is actually fitted so "Save" adopts reality (and clears the
        # false-red on a box judged against the full profile).
        if lo.get("declared"):
            initial = set(x for x in str(lo["declared"]).split(",") if x)
        else:
            initial = {c for c, _, _ in MODULES
                       if sw_by.get(c) or sw_by.get(SW_ALIAS.get(c, ""))}
        initial.add("takserver")
        boxes = []
        for comp, clabel, _ in MODULES:
            req = comp == "takserver"
            ck = " checked" if (req or comp in initial) else ""
            dis = " disabled" if req else ""
            boxes.append(f"<label class=lo-item><input type=checkbox class=lo-box "
                         f"value='{e(comp)}'{ck}{dis}> {e(clabel)}"
                         + (" <span class=lo-req>required</span>" if req else "") + "</label>")
        doc.append("<h2 class=sec-eye>What this box is meant to run</h2>")
        doc.append(
            "<form class='action loadout-form' data-id=set-loadout data-pass=1 data-read=0 "
            f"data-confirm=\"Set what {e(label)} is meant to run. Health checks judge exactly "
            "the ticked set; anything unticked stops being watched.\" data-result=text>"
            f"<input type=hidden name=target value='{e(name)}'>"
            "<input type=hidden name=tilesets value='-'>"
            f"<input type=hidden name=components value='{e(','.join(sorted(initial)))}'>"
            "<p class=meta>Tick the modules this box carries. Anything unticked reads "
            "“not fitted” on its checks instead of failing. TAK Server is always required.</p>"
            f"<div class=lo-grid>{''.join(boxes)}</div>"
            # set-loadout is passphrase-gated (data-pass=1), so the form must carry the operator
            # password field the generic action submit reads - exactly as action_form_html does.
            # Without it the submit sent an empty passphrase and the box answered "incorrect".
            "<label class=fl>Operator password"
            "<input name=passphrase type=password required autocomplete=off></label>"
            "<div class=a-act><button class=a-go type=submit>Save loadout</button></div>"
            "<div class='a-res' role=status aria-live=polite></div></form>")
        doc.append(f"<script>{LOADOUT_EDITOR_JS}</script>")

    # 1.34.0 Security posture: read the box's hardening and firewall state, and act on it.
    # The plan reads are live (read-only, safe); apply/revert are gated action forms.
    if any(x in acts for x in ("harden-plan", "firewall-plan")) and name in act_targets:
        doc.append(f"<h2 class=sec-eye>Security</h2><div class=secgrid data-box='{e(name)}'>")
        if "harden-plan" in acts:
            doc.append("<div class=seccard id=sec-harden><div class=sec-h><b>Posture</b>"
                       "<span class=sec-badge>reading…</span></div>"
                       "<div class='sec-body meta'>SSH, kernel, auditd, fail2ban, core "
                       "dumps.</div><div class=sec-acts></div></div>")
        if "firewall-plan" in acts:
            doc.append("<div class=seccard id=sec-fw><div class=sec-h><b>Firewall</b>"
                       "<span class=sec-badge>reading…</span></div>"
                       "<div class='sec-body meta'>ufw against this box's declared policy."
                       "</div><div class=sec-acts></div></div>")
        doc.append("</div>")
        doc.append(f"<script>{SECURITY_JS}</script>")

    fed = next((c for c in checks if c.get("category") == "federation"), None)
    if fed:
        on = "not enabled" not in (fed.get("detail") or "")
        doc.append("<h2 class=sec-eye>Federation</h2>"
                   f"<div class=swrow style='border-top:0'><span class=sw-n>v2 server</span>"
                   f"<span class=sw-v>{'on · 9001' if on else 'standalone'}</span>"
                   f"<span class=sw-s>{e(fed.get('detail', ''))}</span><span class=sw-a></span></div>")

    links = service_links(t)
    if links:
        doc.append("<h2 class=sec-eye>Services</h2><div class=links>")
        for lbl, url, note in links:
            doc.append(f"<a class=svc href='{e(url)}' target=_blank rel='noopener noreferrer'>"
                       f"<span class=svc-l>{e(lbl)}</span>"
                       f"<span class=svc-u>{e(url)}</span>"
                       f"<span class=svc-n>{e(note)}</span></a>")
        doc.append("</div>")

    if checks:
        cats = {}
        for c in checks:
            cats.setdefault(c.get("category", "other"), []).append(c)
        doc.append(f"<details style='margin-top:22px'><summary>All {len(checks)} checks</summary>")
        for cat in sorted(cats):
            doc.append(f"<div class=cat>{e(cat)}</div>")
            for c in cats[cat]:
                val = (f"{c['value']} {e(c.get('unit', ''))}"
                       if isinstance(c.get('value'), (int, float)) else "")
                doc.append(f"<div class=checkrow><span class='d {e(c.get('status', ''))}'></span>"
                           f"<span class=cn><span class=sr-only>{e(c.get('status', ''))} </span>"
                           f"{e(c.get('name', ''))}</span>"
                           f"<span class=cd>{e(c.get('detail', '') or val)}</span></div>")
        doc.append("</details>")

    if acts and name in act_targets:
        # the *-plan reads are the Security cards' own machinery, not catalogue buttons
        here = [aid for aid in acts if action_applies(aid, t)
                and aid not in ("harden-plan", "firewall-plan")]
        # the page's whole point, promoted: putting a device on the map is not one of
        # twenty catalogue rows, it is THE job
        if "enrol-device" in here:
            doc.append("<section class=actions aria-label='Add a device'><div class=ah>"
                       "<h2 class=title>Add a device</h2><span class=meta>"
                       "Name it, give it a group, confirm - you get a QR code the device "
                       "scans to join this server. Groups matter: devices only see their "
                       "own group's traffic.</span></div><div class=agrid>"
                       + action_form_html("enrol-device", fixed=name) + "</div></section>")
            # Bulk enrolment: a whole team at once -> a printable QR sheet (PDF) + a ZIP of
            # every device's QR and credentials, downloaded here or copied to a USB stick.
            openpass = (not auth_configured()) and ACTIONS["enrol-device"].get("needs_passphrase")
            doc.append(
                "<section class=actions aria-label='Bulk enrolment'><div class=ah>"
                "<h2 class=title>Bulk enrolment</h2><span class=meta>"
                "Enrol a whole team at once. You get a printable <b>QR sheet (PDF)</b> and a "
                "<b>ZIP</b> of every device's QR and credentials - download them here, or, on "
                "the box itself, copy them straight to a USB stick.</span></div>"
                f"<form class=bulk-form data-box='{e(name)}'>"
                "<div class=bulk-fields>"
                "<label class=fl>Group<input class=bulk-group maxlength=40 placeholder='e.g. recon' spellcheck=false autocomplete=off></label>"
                "<label class=fl>Name prefix<input class=bulk-prefix maxlength=32 placeholder='e.g. alpha-' spellcheck=false autocomplete=off></label>"
                "<label class=fl>How many<input class=bulk-count type=number min=1 max=30 value=5></label>"
                "<label class=fl>Start at<input class=bulk-start type=number min=1 value=1></label>"
                "</div>"
                "<details class=bulk-names><summary>Or type device names, one per line</summary>"
                "<textarea class=bulk-list rows=4 spellcheck=false autocomplete=off "
                "placeholder='alpha-01&#10;alpha-02&#10;bravo-lead'></textarea></details>"
                + ("<label class=cred-pass><span>Operator password</span>"
                   "<input type=password class=bulk-pass autocomplete=off placeholder='to enrol'></label>"
                   if openpass else "")
                + "<div class=bulk-actions><button type=submit class=a-go>Generate QR sheet</button>"
                "<span class=bulk-msg role=status></span></div></form>"
                "<div class=bulk-out aria-live=polite></div></section>")
        cat = [a for a in here if a != "enrol-device" and ACTIONS.get(a, {}).get("catalogue", True)]
        if cat:
            doc.append(f"<section class=actions aria-label='Actions on {e(label)}'><div class=ah>"
                       f"<h2 class=title>Actions on this server</h2><span class=meta>"
                       "Pre-bound to this box; shown only where the inventory says they "
                       "apply. Reads run on one click, writes confirm, all are logged."
                       "</span></div>")
            doc.append(actions_grouped_html(cat, fixed=name, collapsible=True))
            doc.append("</section>")

        # Credentials panel: re-download the device credentials this box has issued. The list is
        # a read (no passphrase); each download returns a secret, so it asks for the passphrase.
        if "list-credentials" in here and "fetch-credential" in acts:
            need_pass = ACTIONS["fetch-credential"]["needs_passphrase"]
            doc.append(f"<section class=creds aria-label='Credentials on {e(label)}' "
                       f"data-box='{e(name)}' data-needpass='{1 if need_pass else 0}'>"
                       "<div class=ah><h2 class=title>Credentials</h2><span class=meta>"
                       "Device certificates and enrolment QR codes issued on this box, for "
                       "re-download. Enrolment tokens minted before the credential store existed "
                       "cannot be recovered - reissue those with Enrol device.</span></div>"
                       "<div class=cred-tools>"
                       "<button type=button class=cred-refresh>List credentials</button>"
                       + (f"<label class=cred-pass><span>Operator password</span>"
                          f"<input type=password class=cred-passinp autocomplete=off "
                          f"placeholder='to download'></label>" if need_pass else "")
                       + "</div><div class=cred-list aria-live=polite>"
                       "<div class=cred-empty>Listing credentials&hellip;</div></div>"
                       "<div class=cred-view></div></section>")

    doc.append(footer_html(state, acts))
    if "list-credentials" in enabled_actions(load_actions_config()):
        doc.append(f"<script>{CREDENTIALS_JS}</script>")
    if "enrol-device" in enabled_actions(load_actions_config()):
        doc.append(f"<script>{BULK_ENROL_JS}</script>")
    doc.append(f"<script>{MODULE_OFFLINE_JS}</script>")
    return "".join(doc)


def render_operations(state):
    """The whole catalogue with a box picker, and the audit log made visible."""
    e = html.escape
    age = age_seconds(state.get("generated_at", ""))
    stale = age is None or age > STALE_AFTER
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    targets = target_options(cfg, state)

    doc = page_head("Operations — " + load_instance()["product_name"])
    doc.append(header_html(state, ev, age, "operations", crumb="Operations"))
    doc.append(f"<div class='vband {e(ev)}'><span class=vdot></span>"
               f"{e(estate_summary(state, ev, stale, age))}</div>")
    doc.append("<main id=main class=wrap>")
    doc.append(stale_banner(age, stale))

    if acts:
        doc.append("<section class=actions aria-label='Estate actions'><div class=ah>"
                   "<h2 class=title>Estate actions</h2><span class=meta>The full catalogue, "
                   "any box. Each server's own page carries the same actions pre-bound. "
                   "Reads run on one click, writes confirm, all are logged."
                   "</span></div>")
        doc.append(actions_grouped_html(acts, targets=targets, collapsible=True))
        doc.append("</section>")
    else:
        doc.append("<div class=meta style='margin-top:18px'>No actions enabled. The console "
                   "is read-only until <code>/etc/vantage-console/actions.json</code> exists.</div>")

    entries = load_audit()
    doc.append("<h2 class=sec-eye>Audit log</h2>")
    if entries:
        doc.append("<div class=tablewrap><table class=dtable>"
                   "<tr><th scope=col>When</th><th scope=col>Action</th><th scope=col>Box</th>"
                   "<th scope=col>Result</th><th scope=col>Inputs</th><th scope=col>From</th></tr>")
        for a in entries:
            ins = json.dumps(a.get("inputs") or {}, separators=(",", ":"))
            ins = ins if len(ins) <= 60 else ins[:57] + "…"
            r = a.get("result", "?")
            doc.append(f"<tr><td>{e(str(a.get('ts', '?')))}</td>"
                       f"<td>{e(str(a.get('action', '?')))}</td>"
                       f"<td>{e(str(a.get('target', '?')))}</td>"
                       f"<td class='r-{e(r)}'>{e(r)}</td>"
                       f"<td>{e(ins)}</td><td>{e(str(a.get('client', '')))}</td></tr>")
        doc.append("</table></div><div class=meta style='margin-top:8px'>Last "
                   f"{len(entries)} attempts, newest first, from "
                   "<code>/var/lib/vantage-console/agent/actions.log</code>. Every attempt lands "
                   "here, denied and failed included. Secrets are never logged.</div>")
    else:
        doc.append("<div class=meta>No actions recorded yet.</div>")

    doc.append("<h2 class=sec-eye>Doctrine</h2>"
               "<p class=doct>This surface is a catalogue of known operations, never a "
               "remote shell. Every action is one reviewed, pinned script behind its own "
               "forced-command key; the console chooses when, never what. Drift is "
               "surfaced, convergence is clicked; nothing here acts on its own initiative. "
               "Adding a server to the estate is a deliberate act on the console box, "
               "documented in <code>console/actions/</code>.</p>")

    doc.append(footer_html(state, acts))
    return "".join(doc)


def render_agent(state):
    """The Agent hub: the operator decides how AI connects and how much it may do. Each
    connection is a route (an MCP socket, an API key, a resident OpenClaw) at an autonomy
    level the operator set (observe / propose / act). Pending proposals live here too - the
    default posture is still a human confirming - and the whole thing is one page a newcomer
    can set up from nothing."""
    e = html.escape
    age = age_seconds(state.get("generated_at", ""))
    stale = age is None or age > STALE_AFTER
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    targets = {t.get("name"): t for t in state.get("targets", [])}
    pending = [p for p in load_proposals() if p.get("status") == "pending"]
    conns = load_connections()
    inst = load_instance()
    host = state.get("console_host") or "this-console"

    doc = page_head("AI Connections — " + inst["product_name"])
    doc.append(header_html(state, ev, age, "agent", crumb="AI Connections"))
    doc.append("<main id=main class=wrap>")
    doc.append("<section aria-label='AI connections'><div class=ah>"
               "<h2 class=title>AI Connections</h2><span class=meta>"
               "Connect an AI assistant to this console, on your terms. You choose the route "
               "and how much it may do; the console gives it eyes on the estate and the "
               "knowledge vault, and does exactly as much as you allow. No route bills you "
               "for the AI itself - the model that thinks is always the one you bring.</span>"
               "</div>")
    # what can actually connect HERE, computed from this box's real state - the page
    # explains itself instead of offering routes that cannot work
    _fq = next((str(t.get("fqdn") or "") for t in state.get("targets", [])
                if t.get("fqdn")), "")
    _https = tls_active()
    _haskey = bool(load_agent_key().get("key"))
    doc.append("<h2 class=sec-eye>1 · How this works</h2>")
    doc.append("<p class=doct>Agents connect to <b>this console</b>, not to each TAK "
               "server - one connection sees every box this console manages. "
               "<b>Reachability grants nothing:</b> being on the same network as the "
               "console gives an AI no access at all. Access exists only when you mint "
               "a connection below, and it grants exactly this, no more: read the "
               "estate's health and servers, read credential <i>names</i> (never "
               "secrets), <b>read</b> the Knowledge Vault (a connection can never "
               "write it), and - at the level you choose - propose or run actions "
               "from the gated catalogue. A connection can never change software, "
               "never open a shell, never touch anything outside that list.</p>")
    # AUTONOMY explainer
    doc.append("<div class=agrid style='margin:6px 0 18px'>"
               "<div class=mod><div class=mod-h><b>Observe</b>"
               "<span class='mod-b abs'>read only</span></div>"
               "<div class=mod-d>sees estate health, servers and the vault; changes nothing."
               "</div></div>"
               "<div class=mod><div class=mod-h><b>Propose</b>"
               "<span class='mod-b drift'>human confirms</span></div>"
               "<div class=mod-d>suggests gated actions that land below for you to run - the "
               "default, and the safest place to start.</div></div>"
               "<div class=mod><div class=mod-h><b>Act</b>"
               "<span class='mod-b ok'>autonomous</span></div>"
               "<div class=mod-d>runs its allowed actions directly, every one audited under "
               "the connection's name. Your deliberate choice.</div></div></div>")

    # Draw the line clearly (Matt): what ships in Vantage vs what is an estate build MilUX
    # helps with. The connection HUB is the product; a resident agent or local model on your
    # own hardware is a service.
    doc.append("<h2 class=sec-eye>What is in Vantage, and what we can help you build</h2>"
               "<div class=agrid style='margin:6px 0 18px'>"
               "<div class=mod><div class=mod-h><b>In Vantage, today</b>"
               "<span class='mod-b ok'>self-serve</span></div>"
               "<div class=mod-d>Everything on this page. Connect an MCP-capable agent, an API "
               "key, or a resident agent; set its autonomy; it reads the estate and the "
               "Knowledge Vault and, at the level you choose, proposes or runs gated actions. No "
               "MilUX involvement, and no cost beyond the model you bring.</div></div>"
               "<div class=mod><div class=mod-h><b>On your estate, with our help</b>"
               "<span class='mod-b drift'>a conversation</span></div>"
               "<div class=mod-d>A resident agent running on your own hardware, a local model for "
               "a disconnected network, or an assistant wired into your own systems and data - "
               "these are estate-specific builds. Vantage gives the connection point; standing "
               "the agent up on your estate is where MilUX helps. Talk to us: "
               "<b>matt@milux.co.uk</b>.</div></div></div>")

    # CONNECTIONS
    _ctx = load_agent_context_doc()
    doc.append("<details class=ai-config><summary class=onto-sum>AI configuration "
               "<span class=meta>- the identity and standing orders the built-in chat "
               "and MCP-connected agents follow</span></summary>"
               "<p class=meta>This is the assistant's setup, not estate knowledge, so "
               "it lives here rather than in the Knowledge Vault. The built-in chat and "
               "MCP-connected agents read it before answering. A <b>resident agent</b> "
               "(ours is called " + e(inst["agent_name"]) + ") has its own identity on "
               "its own box and does not use this.</p>"
               "<form id=aicfg>")
    for key, label, hint in AGENT_CONTEXT_FIELDS:
        doc.append(f"<label class=fl>{e(label)}"
                   f"<textarea id='ac-{key}' rows=4 spellcheck=false>"
                   f"{e(_ctx.get(key, ''))}</textarea>"
                   f"<span class=hint>{e(hint)}</span></label>")
    doc.append("<div class=fedpop-act><button type=submit class='a-go confirm'>"
               "Save AI configuration</button>"
               "<span id=ac-res class=lib-status role=status "
               "style='margin-left:10px'></span></div></form></details>")
    doc.append("<h2 class=sec-eye>2 · Choose your route</h2>")
    doc.append("<p class=doct>The difference between the routes is <b>where the "
               "connection comes from</b>: your own computer, Anthropic's cloud, this "
               "console itself, or a box on this estate.</p>")
    rows = []
    rows.append(("Claude Desktop / Claude Code",
                 "YOUR OWN COMPUTER - the app or terminal tool runs on your machine, "
                 "so only your machine needs to reach this console (LAN or VPN is "
                 "enough)",
                 "ready", "Create an MCP connection below and paste the config it "
                 "shows you."))
    if _https and _fq:
        rows.append(("claude.ai / Cowork (cloud)",
                     "ANTHROPIC'S CLOUD - the browser product connects from their "
                     "servers, so this console must be public, on https",
                     "ready", "This console serves https on a public name: create an "
                     "MCP connection and give Cowork the endpoint and token."))
    elif _fq:
        rows.append(("claude.ai / Cowork (cloud)",
                     "ANTHROPIC'S CLOUD - the browser product connects from their "
                     "servers, so this console must be public, on https",
                     "step", "This box has a public name but the console is on plain "
                     "http - press the HTTPS button below first."))
    else:
        rows.append(("claude.ai / Cowork (cloud)",
                     "ANTHROPIC'S CLOUD - the browser product connects from their "
                     "servers, so this console must be public, on https",
                     "no", "Not on this console: it is not internet-facing, and by "
                     "design Vantage never tunnels a private console to the internet. "
                     "Use Claude Desktop or Code from inside the network instead."))
    rows.append(("Built-in chat (API key)",
                 "THIS CONSOLE ITSELF - no other software; the console calls "
                 "Anthropic over the internet with a key you paste",
                 "ready" if _haskey else "step",
                 "Key set - the chat below works." if _haskey else
                 "Paste an Anthropic API key in the API key tab below. Only for an "
                 "estate you are happy touches the internet - a deliberately offline "
                 "estate should not do this."))
    rows.append(("Resident agent, local model",
                 "A BOX ON THIS ESTATE - fully offline, a local model, no internet "
                 "at any point",
                 "soon", "The right answer for a disconnected estate. The one-press "
                 "install is designed and not yet built; today you stand the agent up "
                 "yourself and connect it like Claude Code."))
    doc.append("<div class=cap-matrix>")
    for label, where, st, what in rows:
        badge = {"ready": "<span class='mod-b ok'>available</span>",
                 "step": "<span class='mod-b drift'>one step away</span>",
                 "no": "<span class='mod-b abs'>not on this console</span>",
                 "soon": "<span class='mod-b abs'>designed, not built</span>"}[st]
        doc.append(f"<div class=cap-row><div class=cap-h><b>{e(label)}</b>{badge}</div>"
                   f"<div class=cap-where>connects from: {e(where)}</div>"
                   f"<div class=cap-what>{e(what)}</div></div>")
    doc.append("</div>")
    doc.append("<p class=meta>The rule behind the reds: an estate is either "
               "<b>connected</b> (you are happy it touches the internet) or "
               "<b>disconnected</b> (private and deliberately offline - local models "
               "only). Vantage never asks you to expose a privately hosted server to "
               "the internet.</p>")

    doc.append("<h2 class=sec-eye>3 · Set it up</h2>")
    doc.append("<div class=route-tabs>"
               "<button type=button class='route-tab on' data-r=mcp>MCP connection</button>"
               "<button type=button class=route-tab data-r=apikey>Built-in chat (API key)</button>"
               "<button type=button class=route-tab data-r=openclaw>Resident agent</button></div>")

    # MCP panel
    _tls_fqdn = next((str(t.get("fqdn") or "") for t in state.get("targets", [])
                      if t.get("fqdn")), "")
    doc.append("<div class=route-panel data-r=mcp>"
               "<p class=doct>Point an MCP-capable agent at this console. The agent connects "
               "over your own subscription; nothing is billed here. Give it a name and an "
               "autonomy level, and the console mints a token and shows the exact config to "
               "paste. The token is shown once.</p>"
               "<p class=meta><b>Claude Desktop and Claude Code</b> work from any machine "
               "that can reach this console's address. <b>claude.ai and Cowork custom "
               "connectors</b> connect from the cloud: they require <b>https</b> and a "
               "publicly reachable address.</p>"
               + (("<div class='banner drift'><b>This console is on plain http.</b><span> "
                   "The box already holds a trusted certificate from the build. "
                   "<button type=button id=httpson class=cred-refresh>Serve HTTPS with "
                   "this box's certificate</button> - the console restarts on https at "
                   "the same port (sign in again after).</span>"
                   f"<input type=hidden id=httpsfqdn value='{e(_tls_fqdn)}'></div>")
                  if (not tls_active() and _tls_fqdn) else "")
               + (("<p class=meta>Serving <b>https</b> - cloud connectors can use the "
                   "endpoint below as-is.</p>") if tls_active() else "")
               + "<p class=meta><b>Private console?</b> A cloud connector needs a public "
               "https front door, and there is no shortcut that does not widen your "
               "attack surface. The estate-native route - publishing a private "
               "console's MCP socket through a PUBLIC peer console you already run - "
               "is the designed next step on the Sync layer. Until it lands, connect "
               "from Claude Desktop or Claude Code on a machine inside the network "
               "instead; third-party tunnels exist but carry their own risk and are "
               "deliberately not recommended here.</p>"
               + "<form class=addform depcard data-route=mcp>"
               "<label class=fl>Name<input class=cn-name maxlength=40 "
               "placeholder='e.g. Cowork on my laptop'></label>"
               "<label class=fl>Autonomy<select class=cn-autonomy>"
               "<option value=observe>Observe - read only</option>"
               "<option value=propose selected>Propose - I confirm</option>"
               "<option value=act>Act - autonomous</option></select>"
               "<span class=hint>you can change this any time</span></label>"
               "<div class=fedpop-act><button type=submit class='a-go confirm'>"
               "Create connection</button></div></form>"
               "<div class=cn-result></div></div>")

    # API-key panel: paste a key, and the built-in chat works right here
    keycfg = load_agent_key()
    has_key = bool(keycfg.get("key"))
    kau = keycfg.get("autonomy", "propose")
    doc.append("<div class=route-panel data-r=apikey hidden>"
               "<p class=doct>Paste an Anthropic-compatible API key and the built-in chat "
               "below works - the assistant reads the estate and the vault and proposes or "
               "acts at the level you set. Runs on <b>your</b> key, your cost; the key is "
               "stored only on this box and never leaves it.</p>")
    doc.append("<form class=addform depcard id=keyform>"
               "<label class=fl>API key<input type=password id=ak-key autocomplete=off "
               + ("placeholder='key is set - leave blank to keep it'" if has_key
                  else "placeholder='sk-ant-...'")
               + "></label>"
               "<label class=fl>Model<input id=ak-model maxlength=60 value='"
               + e(keycfg.get("model", "claude-sonnet-4-5")) + "'>"
               "<span class=hint>any model your key can use</span></label>"
               "<label class=fl>Autonomy<select id=ak-autonomy>"
               + "".join(f"<option value='{a}'" + (" selected" if a == kau else "")
                         + f">{a}</option>" for a in AUTONOMY) + "</select></label>"
               "<div class=fedpop-act><button type=submit class='a-go confirm'>"
               + ("Update" if has_key else "Save key") + "</button>"
               + ("<button type=button id=ak-clear class=cred-del>Remove key</button>"
                  if has_key else "")
               + "<span id=ak-res class=lib-status role=status style='margin-left:10px'>"
               + ("key set · model " + e(keycfg.get("model", "")) if has_key else "")
               + "</span></div></form>")
    if has_key:
        doc.append("<div class=chatbox><div class=chat-head><b>Conversation</b>"
                   "<span class=meta> - kept on the console; a refresh loses nothing</span>"
                   "<span class=spacer></span>"
                   "<button type=button id=ak-new class=cred-del>New conversation</button></div>"
                   "<div class=chatlog id=ak-log>"
                   "<div class=meta>Ask about the estate. The assistant reads health, "
                   "servers and the vault before answering.</div></div>"
                   "<form class=chatbar id=ak-chatform><input id=ak-msg autocomplete=off "
                   "placeholder='Message the assistant…' required>"
                   "<button type=submit>Send</button></form></div>")
    doc.append("</div>")

    # OpenClaw panel
    doc.append("<div class=route-panel data-r=openclaw hidden>"
               "<p class=doct>Install a resident OpenClaw agent on a box - a standing "
               "assistant that lives with the estate and works even with the network closed, "
               "optionally on a local model. This is the pattern " + e(inst["agent_name"])
               + " runs on. It talks to the console over an MCP connection like any other, so "
               "its autonomy is set the same way.</p>"
               "<div class='banner drift'><b>Coming next.</b><span> The one-press OpenClaw "
               "install job is scoped (a gated build like Deploy). Until it lands, stand "
               "OpenClaw up yourself and connect it with an MCP token from the socket tab.</span>"
               "</div></div>")

    # PROPOSALS - the default posture, inline
    doc.append("<h2 class=sec-eye>4 · Connected now</h2>")
    if conns:
        doc.append("<div class=conns>")
        for c in conns:
            rlabel = {"mcp": "MCP socket", "apikey": "API key",
                      "openclaw": "OpenClaw"}.get(c.get("route"), c.get("route"))
            au = c.get("autonomy", "observe")
            opts = "".join(
                f"<option value='{a}'" + (" selected" if a == au else "") + f">{a}</option>"
                for a in AUTONOMY)
            doc.append(
                "<div class=conn data-id='" + e(c.get("id", "")) + "'>"
                "<div class=conn-h><b>" + e(c.get("name", "")) + "</b>"
                "<span class=conn-route>" + e(rlabel) + "</span></div>"
                "<div class=conn-meta>" + (f"{c.get('calls', 0)} calls" if c.get("calls")
                                           else "not used yet")
                + (f" · last {e(str(c.get('last_used'))[:16])}" if c.get("last_used") else "")
                + "</div>"
                "<div class=conn-acts><label class=conn-au>Autonomy"
                "<select class=cn-au>" + opts + "</select></label>"
                "<button type=button class='cn-revoke cred-del'>Revoke and remove</button></div></div>")
        doc.append("</div>")
    else:
        doc.append("<p class=doct>No agent connected yet. Set one up in section 3 above - it takes a minute, "
                   "and you can revoke it any time.</p>")

    # ADD CONNECTION - the three routes
    doc.append("<h2 class=sec-eye>5 · Watch it work</h2>")
    doc.append("<h3 class=sync-h>Proposals awaiting you</h3>")
    if not pending:
        doc.append("<p class=doct>Nothing to confirm. When a Propose-level agent suggests an "
                   "action it appears here as a ready-to-run console action.</p>")
    else:
        tgts = target_options(cfg, state)
        doc.append("<div class=agrid>")
        for p in pending:
            aid = p.get("aid")
            if aid in ACTIONS:
                doc.append("<div class=prop-cell>"
                           "<div class=prop-why-top>From <b>"
                           + e(str(p.get("source", "agent"))) + "</b> · "
                           + e(str(p.get("rationale", ""))[:200])
                           + " <button class=p-dismiss type=button data-id='"
                           + e(str(p.get("id", ""))) + "'>Dismiss</button></div>"
                           + action_form_html(aid, targets=tgts, prefill=p.get("args") or {})
                           + "</div>")
        doc.append("</div>")
        doc.append(f"<script>{ACTION_JS}</script>")

    # activity: watch the agent work
    doc.append("<h3 class=sync-h>Recent agent activity</h3>"
               "<div class=act-feed id=actfeed><div class=meta>Loading…</div></div>")

    doc.append(f"<script>{AGENT_HUB_JS}</script>")
    doc.append(footer_html(state, acts))
    return "".join(doc)

def federation_nodes(state, cfg):
    """Every managed TAK box that can federate, with its live federation state read from the
    checker's `federation` check (socket-only, no config read). address is where a partner would
    point a link. Only boxes the console can act on (in actions.json targets) can be dragged from."""
    targets_cfg = (cfg or {}).get("targets", {})
    fedgroups = load_fedgroups()
    nodes = []
    for t in state.get("targets", []):
        checks = t.get("checks") or []
        fed = next((c for c in checks if c.get("category") == "federation"), None)
        sw = t.get("software") or []
        is_tak = any(s.get("name") == "takserver" for s in sw) or fed is not None
        if not is_tak:
            continue
        detail = (fed or {}).get("detail", "")
        fed_on = fed is not None and "not enabled" not in detail
        try:
            federates = int((fed or {}).get("value") or 0)
        except (TypeError, ValueError):
            federates = 0
        name = t.get("name")
        label = t.get("label", name)
        grp = (fedgroups.get(name) or {})
        nodes.append({
            "name": name,
            "label": label,
            "short": label.split(" (")[0].strip() or name,
            "address": t.get("link_host") or t.get("fqdn") or name,
            "fed_on": fed_on,
            "federates": federates,
            "reachable": bool(t.get("reachable")),
            "managed": name in targets_cfg,
            "group": grp.get("group") or "",
            "group_ts": grp.get("ts") or "",
        })
    return nodes


def federation_map_data(state, cfg):
    """Nodes + edges for the map. Nodes are the managed TAK boxes, any unmanaged partner the
    console has federated to, AND any live federate the checker can see on the wire that nobody
    recorded - a partner federated by hand, or from the far side, still appears, because a map
    of the federation fabric that hides real links is worse than no map. Edges carry whether the
    source box currently reports live federates, so a link reads as configured or live."""
    nodes = federation_nodes(state, cfg)
    names = {n["name"] for n in nodes}
    by_addr = {}
    for n in nodes:
        aliases = [n.get("address"), n.get("label"), n.get("short"), n.get("name")]
        # a recorded link may dial a managed box by any of its addresses, including a
        # raw IP; resolve every alias so the edge lands on the box, not on a stranger
        # (found live: a link to the firmbase's tailnet IP drew as an external partner)
        aliases += list(_resolves_to(n.get("address")))
        for a in aliases:
            if a:
                by_addr[str(a).lower()] = n["name"]
    # Who each box is actually talking to, from the checker's socket-level peer list. Liveness is
    # attributed PER LINK from this: a box with one federate and five recorded links has one live
    # link, and the old "source reports federates > 0" rule drew all five as live. A map that
    # calls four dead links live is worse than one that admits it does not know.
    peers_by_src = {}
    for t in state.get("targets", []):
        if t.get("name") not in names:
            continue
        rec = next((c for c in (t.get("checks") or [])
                    if c.get("category") == "federation" and c.get("name") == "peers"), None)
        if rec is None:
            continue
        peers_by_src[t["name"]] = {x.strip().lower()
                                   for x in (rec.get("detail") or "").split() if x.strip()}
    unmanaged, edges = {}, []
    for lk in load_fedlinks():
        src = lk.get("source")
        if src not in names:
            continue
        addr = str(lk.get("address", "")).lower()
        to = by_addr.get(addr)
        if not to:
            uid = "ext:" + addr
            unmanaged.setdefault(uid, {
                "name": uid, "short": lk.get("partner") or lk.get("address"),
                "label": lk.get("partner") or lk.get("address"), "address": lk.get("address"),
                "fed_on": False, "federates": 0, "reachable": True, "managed": False,
                "unmanaged": True})
            to = uid
        src_node = next((n for n in nodes if n["name"] == src), None)
        to_node = next((n for n in nodes if n["name"] == to), None)
        dst_ips = _resolves_to(addr) | (_resolves_to((to_node or {}).get("address")) if to_node
                                        else set())
        src_ips = _resolves_to((src_node or {}).get("address"))
        if src in peers_by_src:
            live, basis = bool(dst_ips & peers_by_src[src]), "peers"
        elif to in peers_by_src:
            live, basis = bool(src_ips & peers_by_src[to]), "peers"
        elif src_node and src_node.get("federates", 0) > 0:
            # an older checker reports the count but not who: the box IS federated with
            # something, and this link is a candidate, but nothing here pins it down
            live, basis = True, "count"
        else:
            live, basis = False, "none"
        edges.append({"from": src, "to": to, "name": lk.get("partner") or lk.get("address"),
                      "address": lk.get("address"), "port": lk.get("port"), "ts": lk.get("ts"),
                      "live": live, "live_basis": basis})
    # Live peers from the checker (tak-health >= 1.4.0, socket-only): every established
    # federation conversation's remote address. A peer that is neither a managed box nor an
    # already-drawn partner becomes an external node with a live edge - unrecorded, but real.
    known_ips = {}
    for n in nodes:
        for ip in _resolves_to(n.get("address")):
            known_ips[ip] = n["name"]
    for u in unmanaged.values():
        for ip in _resolves_to(u.get("address")):
            known_ips.setdefault(ip, u["name"])
    edge_pairs = {(ed["from"], ed["to"]) for ed in edges}
    for src, peerset in peers_by_src.items():
        for peer in sorted(peerset):
            if not peer:
                continue
            to = known_ips.get(peer)
            if to is None:
                to = "fed:" + peer
                unmanaged.setdefault(to, {
                    "name": to, "short": peer, "label": f"federate at {peer}", "address": peer,
                    "fed_on": True, "federates": 0, "reachable": True, "managed": False,
                    "unmanaged": True, "unrecorded": True})
                known_ips[peer] = to
            if to == src or (src, to) in edge_pairs or (to, src) in edge_pairs:
                continue
            edge_pairs.add((src, to))
            edges.append({"from": src, "to": to, "name": "live federate", "address": peer,
                          "port": "", "ts": "", "live": True, "unrecorded": True})
    allnodes = nodes + list(unmanaged.values())
    pos = load_fedpos()
    for n in allnodes:
        if n["name"] in pos:
            try:
                n["px"], n["py"] = float(pos[n["name"]][0]), float(pos[n["name"]][1])
            except (TypeError, ValueError, IndexError):
                pass
    return allnodes, edges


def _pair_group(a, b):
    """What crosses this link, from what the console recorded. A group has to be shared on BOTH
    sides for anything to cross, so a group we set on one end only is not a green light - it is a
    half-done job, and the map says which half. For a partner we do not manage we can only ever
    speak for our own side, and we say that rather than implying we checked theirs."""
    ga, gb = (a.get("group") or ""), (b.get("group") or "")
    if a.get("managed") and b.get("managed"):
        if ga and gb and ga == gb:
            return {"group_state": "shared", "group": ga}
        if ga and gb:
            return {"group_state": "mismatch", "group": f"{ga} / {gb}"}
        if ga or gb:
            return {"group_state": "one-side", "group": ga or gb,
                    "group_side": (a if ga else b).get("short", "")}
        return {"group_state": "none", "group": ""}
    ours = a if a.get("managed") else b
    g = ours.get("group") or ""
    if g:
        return {"group_state": "our-side", "group": g, "group_side": ours.get("short", "")}
    return {"group_state": "none", "group": ""}


def federation_link_pairs(nodes, edges):
    """One record per PAIR of servers, not per recorded link. Two boxes that have each dialled the
    other are ONE federation between them, and drawing that as two lines on the same geometry is
    what made the old map illegible: two arrowheads, two labels, one on top of the other. Pairing
    also makes the honest answer to "why is that arrow one way" sayable - the arrow is who dialled,
    and one is enough, so a single arrow is a normal healthy link and not a half-built one.

    A link the checker only saw on the wire carries NO direction: an established socket does not
    say who opened it, so those draw with no arrowhead rather than a guessed one."""
    by_name = {n["name"]: n for n in nodes}
    order = {n["name"]: i for i, n in enumerate(nodes)}
    pairs = {}
    for e in edges:
        f, t = e.get("from"), e.get("to")
        if f not in by_name or t not in by_name or f == t:
            continue
        a, b = (f, t) if order.get(f, 0) <= order.get(t, 0) else (t, f)
        rec = pairs.get((a, b))
        if rec is None:
            rec = pairs[(a, b)] = {"a": a, "b": b, "a_short": by_name[a].get("short", a),
                                   "b_short": by_name[b].get("short", b),
                                   "a_to_b": False, "b_to_a": False, "wire_only": True,
                                   "live": False, "live_guess": False, "links": []}
        if e.get("unrecorded"):
            rec["live"] = True          # the checker only reports federates it can see connected
        else:
            rec["wire_only"] = False
            rec["a_to_b" if f == a else "b_to_a"] = True
            rec["live"] = rec["live"] or bool(e.get("live"))
            if e.get("live") and e.get("live_basis") == "count":
                rec["live_guess"] = True
            rec["links"].append({"from": f, "to": t, "name": e.get("name", ""),
                                 "address": e.get("address", ""), "port": e.get("port", ""),
                                 "ts": e.get("ts", ""), "live": bool(e.get("live"))})
    out = []
    for (a, b), rec in pairs.items():
        rec["dir"] = ("both" if rec["a_to_b"] and rec["b_to_a"]
                      else "a" if rec["a_to_b"] else "b" if rec["b_to_a"] else "none")
        rec.update(_pair_group(by_name[a], by_name[b]))
        rec["state"] = "live" if rec["live"] else "configured"
        out.append(rec)
    out.sort(key=lambda r: (order.get(r["a"], 0), order.get(r["b"], 0)))
    return out


# What a pair's group state means, in one place: the map legend, the link table and the popup all
# say the same words, because three phrasings of one fact is how an operator ends up unsure.
GROUP_WORDS = {
    "shared": ("carries {g}", "Both servers share group {g}, so events in it cross this link."),
    "our-side": ("{s} shares {g}", "This console set group {g} on {s}. The partner has to share "
                 "the same group at their end; we cannot see whether they have."),
    "one-side": ("only {s} shares {g}", "Group {g} is set on {s} but not on the other server, so "
                 "nothing crosses yet. Run Share federation group on both sides."),
    "mismatch": ("groups differ: {g}", "The two servers share different groups, so nothing "
                 "crosses. Both sides need the same group name."),
    "none": ("no group - carries nothing", "Neither side has a group recorded by this console. A "
             "federation link with no shared group is up but carries no events."),
}


def group_words(pair, long=False):
    tpl = GROUP_WORDS.get(pair.get("group_state", "none"), GROUP_WORDS["none"])[1 if long else 0]
    return tpl.replace("{g}", pair.get("group", "")).replace("{s}", pair.get("group_side", ""))


def render_federation(state):
    """The interactive federation map: the estate's TAK servers as nodes, one line per federated
    PAIR, and drag-a-link to federate two of them. A link is a gated action - dragging opens a
    confirm, nothing federates until you click - so the map is a face on federation-connect,
    federation-enable and federation-groups, not a way round them.

    The map answers three questions and is judged on them: what is federated with what, which way
    round it was made, and whether anything actually crosses. The third is the one that bites: a
    link with no shared group is up and carries nothing, and a picture that shows the line but not
    the group lets an operator believe a job is done when it is half done."""
    e = html.escape
    age = age_seconds(state.get("generated_at", ""))
    stale = age is None or age > STALE_AFTER
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    nodes, edges = federation_map_data(state, cfg)
    pairs = federation_link_pairs(nodes, edges)
    can_connect = "federation-connect" in acts
    can_enable = "federation-enable" in acts
    can_groups = "federation-groups" in acts

    doc = page_head("Federation — " + load_instance()["product_name"])
    doc.append(header_html(state, ev, age, "federation", crumb="Federation"))
    doc.append(f"<div class='vband {e(ev)}'><span class=vdot></span>"
               f"{e(estate_summary(state, ev, stale, age))}</div>")
    doc.append("<main id=main class=wrap>")
    doc.append(stale_banner(age, stale))
    doc.append("<section aria-label='Federation map'><div class=ah>"
               "<h2 class=title>Federation fabric</h2><span class=meta>Your TAK servers, the "
               "federation between them, and any external federate the servers can see on the "
               "wire. <b>Drag a server</b> to arrange the map - positions are saved for every "
               "operator. <b>Drag the ⚡ handle onto another server</b> to federate them, or onto "
               "empty space to reach a partner you do not manage. Click anything for its details. "
               "Nothing changes until you confirm.</span></div>")

    # Two things to get right, side by side, because operators conflate them: the link carries
    # the connection, the group decides what goes down it. Numbered so the order is obvious.
    doc.append("<div class=fedhow>"
               "<div class=fedhow-c><h3>1 &middot; Make the link</h3><ol>"
               "<li><b>Enable federation</b> on your server. One restart, opens port 9001.</li>"
               "<li><b>Drag the ⚡ handle</b> onto the partner and confirm. One restart. Only one "
               "side needs to do this - see <i>which way the arrow points</i> below.</li>"
               "<li><b>Trust each other's CA.</b> Each side installs the other's federation CA. "
               "The link goes live when both have.</li></ol></div>"
               "<div class=fedhow-c><h3>2 &middot; Decide what crosses</h3><ol>"
               "<li><b>Share federation group</b> on <b>both</b> servers, with the same group "
               "name (MilUX&harr;MilUX across this estate).</li>"
               "<li>Only clients in that group see the traffic that crosses.</li>"
               "<li>A live link with no shared group is connected and <b>carries nothing</b>. "
               "That is the usual reason a link looks fine and nobody sees anything.</li>"
               "</ol></div></div>")

    # Direction is the single most-asked question about this map, so it gets an answer on the
    # page rather than in someone's head. One arrow is a complete, healthy link.
    doc.append("<div class=feddir><b>Which way the arrow points.</b> An arrow shows <b>who dialled "
               "whom</b>: the server the link was configured on points at the partner it calls. "
               "<b>One arrow is a complete link.</b> Federation is not one-way - once the "
               "connection is up and both sides share a group, events cross in both directions. "
               "A double arrow just means both sides have configured a link, which works but is "
               "usually redundant. A line with <b>no arrow</b> is a federate the servers can see "
               "connected on the wire that was not set up from this console, so nobody here knows "
               "which side dialled.</div>")

    doc.append("<div class=fedlegend>"
               "<span class=fl-g>Servers</span>"
               "<span class=fl-i><span class='fl-dot on'></span>federation on</span>"
               "<span class=fl-i><span class='fl-dot off'></span>standalone (federation off)</span>"
               "<span class=fl-i><span class='fl-dot ext'></span>partner you do not manage</span>"
               "<span class=fl-i><span class='fl-dot unrec'></span>external federate, seen on the "
               "wire</span></div>")
    doc.append("<div class=fedlegend>"
               "<span class=fl-g>Links</span>"
               "<span class=fl-i><span class='fl-edge live'></span>live (connected now)</span>"
               "<span class=fl-i><span class='fl-edge'></span>configured (not connected)</span>"
               "<span class=fl-i><span class='fl-edge arr'></span>who dialled</span>"
               "<span class=fl-i><span class='fl-edge both'></span>both sides dialled</span>"
               "<span class=fl-i><span class=fl-num>2</span>federates connected right now</span>"
               "<span class=fl-i><span class='fl-chip ok'>MilUX</span>the group that crosses</span>"
               "<span class=fl-i><span class='fl-chip bad'>MilUX (one side)</span>set on one "
               "server only, so nothing crosses yet</span>"
               "<span class=fl-i><span class='fl-chip bad'>no group</span>connected, carries "
               "nothing</span></div>")

    if not nodes:
        doc.append("<div class=meta style='margin-top:8px'>No TAK servers in the estate yet.</div>")
    elif not can_connect:
        doc.append("<div class=meta style='margin-top:8px'>The map is read-only on this "
                   "console: connecting servers from here is not switched on. It arrives "
                   "when the federation action is enabled during enrolment.</div>")
    payload = {"nodes": nodes, "pairs": pairs, "canConnect": can_connect,
               "canEnable": can_enable, "canGroups": can_groups}
    doc.append('<div class=fedmap-wrap><svg id=fedmap role=img '
               "aria-label='Federation topology'></svg></div>")
    doc.append('<script type="application/json" id=feddata>'
               + json.dumps(payload).replace("<", "\\u003c") + "</script>")
    doc.append("<div id=fedpop class=fedpop hidden></div>")

    # The same facts as a table. A topology picture is good for shape and bad for detail, and
    # "what exactly is federated with what" is a detail question - so it gets a table, readable
    # by anyone who cannot or will not read the diagram.
    doc.append("<h2 class=sec-eye>Every federation link</h2>")
    if not pairs:
        doc.append("<p class=doct>Nothing is federated yet. Enable federation on a server, then "
                   "drag its ⚡ handle onto another to make the first link.</p>")
    else:
        doc.append("<div class=tablewrap><table class='dtable fedtbl'>"
                   "<thead><tr><th>Servers</th><th>Direction</th><th>Connection</th>"
                   "<th>What crosses</th><th>Where it dials</th></tr></thead><tbody>")
        dirwords = {"a": "{a} dialled {b}", "b": "{b} dialled {a}",
                    "both": "both sides dialled a link",
                    "none": "seen connected on the wire; who dialled is not known here"}
        for pr in pairs:
            # a one-way link always reads dialler first, whichever way the pair happens to be
            # stored: "Edge Kit ← Deployed" is a puzzle, "Deployed → Edge Kit" is a sentence
            left, right = pr["a_short"], pr["b_short"]
            if pr["dir"] == "b":
                left, right = right, left
            arrow = "&harr;" if pr["dir"] == "both" else "&mdash;" if pr["dir"] == "none" else "&rarr;"
            dw = dirwords[pr["dir"]].replace("{a}", pr["a_short"]).replace("{b}", pr["b_short"])
            gs = pr["group_state"]
            chip = "ok" if gs == "shared" else "warn" if gs == "our-side" else "bad"
            dials = " · ".join(f"{lk['address']}:{lk['port']}" for lk in pr["links"]) or "—"
            if pr["live"] and pr.get("live_guess"):
                conn = ("<span class=fl-cfg title='The server reports federates connected, but "
                        "this checker is too old to say which link they are on.'>live?</span>")
            elif pr["live"]:
                conn = "<span class=fl-live>live</span>"
            else:
                conn = "<span class=fl-cfg>configured</span>"
            doc.append(f"<tr><td><b>{e(left)}</b> "
                       f"<span class=fedtbl-ar>{arrow}</span> "
                       f"<b>{e(right)}</b></td>"
                       f"<td class=fedtbl-m>{e(dw)}</td>"
                       f"<td>{conn}</td>"
                       f"<td><span class='fl-chip {chip}' "
                       f"title='{e(group_words(pr, long=True))}'>{e(group_words(pr))}</span></td>"
                       f"<td class=fedtbl-m>{e(dials)}</td></tr>")
        doc.append("</tbody></table></div>")
        doc.append("<p class=doct>“What crosses” is what <b>this console</b> set with "
                   "<code>federation-groups</code>. A group set by hand on a server, or by "
                   "someone else's console, is real but is not recorded here - run <b>Share "
                   "federation group</b> from the console once and the map will know it.</p>")

    doc.append("<h2 class=sec-eye>What the map shows</h2>"
               "<p class=doct>A line is a federation between two servers. It reads <b>live</b> "
               "when the source server currently reports connected federates, and "
               "<b>configured</b> until then. Dragging a link fills in "
               "<code>federation-connect</code> and asks you to confirm; the box opens its "
               "firewall for the federation port, edits CoreConfig and restarts once. A standalone "
               "server is enabled first. Group mapping is a separate, deliberate step, so nothing "
               "crosses a link until you map it. Every run is in the Operations audit log.</p>")
    doc.append(f"<script>{FEDERATION_JS}</script>")
    doc.append(footer_html(state, acts))
    return "".join(doc)


# 1.11.0 the native chat panel. Each send is one POST to /api/sam/chat; the console relays it to
# Sam over the loopback gateway as a paired device and returns his reply. Replies are rendered
# with textContent, never innerHTML - Sam's words are data, not markup.
SAMCHAT_JS = """
(function(){
  var f=document.getElementById('scform'); if(!f) return;
  var th=document.getElementById('scthread'), inp=document.getElementById('scinput'),
      btn=document.getElementById('scsend'), empty=document.getElementById('scempty');
  function add(cls,text){var d=document.createElement('div');d.className='scmsg '+cls;
    d.textContent=text;th.appendChild(d);th.scrollTop=th.scrollHeight;return d;}
  f.addEventListener('submit',function(ev){
    ev.preventDefault();
    var t=inp.value.trim(); if(!t||btn.disabled) return;
    if(empty){empty.remove();empty=null;}
    add('me',t); inp.value='';
    var wait=add('wait','Sam is thinking\\u2026');
    btn.disabled=true; inp.disabled=true;
    fetch('/api/sam/chat',{method:'POST',headers:{'Content-Type':'application/json'},
                           body:JSON.stringify({text:t})})
      .then(function(r){return r.json().then(function(j){return {code:r.status,j:j};});})
      .then(function(x){wait.remove();
        if(x.code===200){add('sam',x.j.reply||'');}
        else{add('err',x.j.message||x.j.error||'Sam did not answer.');}})
      .catch(function(e){wait.remove();add('err','Could not reach the console. '+e);})
      .finally(function(){btn.disabled=false;inp.disabled=false;inp.focus();});
  });
})();
"""


# ---------- Networks: the Meshtastic TAK gateway (Spec 001, card 6189) -------------------------
# The bearer layer, distinct from Federation (TAK-to-TAK): networks that devices ride. First
# member: a Meshtastic LoRa mesh bridged into TAK by the vendored gateway (ADR-002). Channels
# are minted here - name plus 256-bit PSK - and held in the console's writable state; the PSK
# is rendered ONLY inside the join QR image, never in page HTML, a URL or the audit trail.
MESH_CHANNELS = os.environ.get("VANTAGE_CONSOLE_MESH_CHANNELS",
                               "/var/lib/vantage-console/agent/mesh-channels.json")
# Where each gateway box SITS (Spec 003). The mesh nodes carry their own GPS and place
# themselves; the box does not - a Heltec V4 has no GPS, and the machine it is plugged into
# has no idea where it is. So the operator places it, once, and the console remembers, the
# same way it remembers federation-map positions. An unplaced box is drawn from its nodes'
# centre and SAID to be unplaced: an inferred position that claims to be surveyed is the
# kind of lie a map should never tell.
MESH_POS = os.environ.get("VANTAGE_CONSOLE_MESH_POS",
                          "/var/lib/vantage-console/agent/mesh-positions.json")
# A node heard within this window is live on the map; older is stale but still drawn, because
# "was here 40 minutes ago" is information and hiding it is not.
MESH_FRESH_SECONDS = 15 * 60
# The regions the console will program. A LoRa region is a legal setting, so the set is
# deliberate: the two the reference estate actually operates in. Extending it is one line
# here plus the deploy action's pattern - do it region by region, on purpose.
MESH_REGIONS = {"EU_868": 3, "US": 1}          # meshtastic RegionCode enum values
MESH_MODEM_SHORT_FAST = 6                       # ModemPreset enum: the estate's preset


def _pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb_len(tag, payload):
    return _pb_varint((tag << 3) | 2) + _pb_varint(len(payload)) + payload


def _pb_uint(tag, n):
    return _pb_varint(tag << 3) + _pb_varint(n)


def mesh_channel_url(name, psk, region):
    """The meshtastic.org join URL: a hand-encoded ChannelSet protobuf (settings + lora_config),
    base64url, no padding. Field numbers verified against meshtastic/protobufs (apponly.proto,
    channel.proto, config.proto). position_precision is 32 - full precision - because the
    estate shipped a 5 km grid by accident once (LESSONS 20)."""
    module = _pb_uint(1, 32)
    settings = (_pb_len(2, psk) + _pb_len(3, name.encode())
                + _pb_len(7, module))
    lora = (_pb_uint(1, 1) + _pb_uint(2, MESH_MODEM_SHORT_FAST)
            + _pb_uint(7, MESH_REGIONS[region]))
    chset = _pb_len(1, settings) + _pb_len(2, lora)
    return "https://meshtastic.org/e/#" + base64.urlsafe_b64encode(chset).decode().rstrip("=")


def _pb_read_varint(b, i):
    n = shift = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7


def mesh_channel_decode(url):
    """(name, psk, region) from a meshtastic join URL, or None. The inverse of
    mesh_channel_url, tolerant of URLs other tools made: repeated settings take the
    primary (first), unknown fields are skipped, base64 padding is optional. Only what
    the product mints is adopted: a named channel with a 256-bit key in a supported
    region - a preset or 128-bit channel is refused rather than half-supported."""
    try:
        frag = url.split("#", 1)[1]
        blob = base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4))
        first_settings, lora = None, None
        i = 0
        while i < len(blob):
            key, i = _pb_read_varint(blob, i)
            tag, wire = key >> 3, key & 7
            if wire == 2:
                ln, i = _pb_read_varint(blob, i)
                val = blob[i:i + ln]
                i += ln
                if tag == 1 and first_settings is None:
                    first_settings = val
                elif tag == 2:
                    lora = val
            elif wire == 0:
                _, i = _pb_read_varint(blob, i)
            elif wire == 5:
                i += 4
            else:
                return None
        if first_settings is None:
            return None
        name, psk = "", b""
        i = 0
        while i < len(first_settings):
            key, i = _pb_read_varint(first_settings, i)
            tag, wire = key >> 3, key & 7
            if wire == 2:
                ln, i = _pb_read_varint(first_settings, i)
                val = first_settings[i:i + ln]
                i += ln
                if tag == 2:
                    psk = val
                elif tag == 3:
                    name = val.decode("utf-8", "replace")
            elif wire == 0:
                _, i = _pb_read_varint(first_settings, i)
            elif wire == 5:
                i += 4
            else:
                return None
        region_code = None
        if lora:
            i = 0
            while i < len(lora):
                key, i = _pb_read_varint(lora, i)
                tag, wire = key >> 3, key & 7
                if wire == 0:
                    v, i = _pb_read_varint(lora, i)
                    if tag == 7:
                        region_code = v
                elif wire == 2:
                    ln, i = _pb_read_varint(lora, i)
                    i += ln
                elif wire == 5:
                    i += 4
                else:
                    return None
        rmap = {v: k for k, v in MESH_REGIONS.items()}
        if len(psk) != 32 or not re.fullmatch(r"[A-Za-z0-9_-]{1,11}", name) \
                or region_code not in rmap:
            return None
        return name, psk, rmap[region_code]
    except Exception:
        return None


def networks_channel_adopt(data, client):
    """Adopt an EXISTING network's channel from its join URL, so a running mesh comes
    under Vantage without a single device rescanning (Spec 001 AC9). The URL is the one
    moment the key transits a request - body, never a URL parameter, behind the operator
    session - and it is never echoed or audited. The console then renders its own QR for
    the channel: same name, key and region, position precision normalised to full."""
    url = str((data or {}).get("url", ""))
    if len(url) > 1024 or not url.startswith("https://meshtastic.org/e/#"):
        return 400, {"error": "paste the channel's meshtastic.org/e/# join link"}
    dec = mesh_channel_decode(url)
    if not dec:
        return 400, {"error": "that link does not decode to a channel this console "
                              "manages: a named channel (up to 11 characters), 256-bit "
                              "key, region " + " or ".join(sorted(MESH_REGIONS))}
    name, psk, region = dec
    channels = load_mesh_channels()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = channels.get(name) or {"created": now}
    rec.update({"psk_b64": base64.b64encode(psk).decode(), "region": region,
                "adopted": now})
    channels[name] = rec
    try:
        save_mesh_channels(channels)
    except OSError as ex:
        return 500, {"error": f"could not write the channel store: {ex}"[:200]}
    audit({"action": "mesh-channel-adopt", "target": name, "result": "OK",
           "client": client})
    return 200, {"ok": True, "name": name, "region": region, "psk_stored": True,
                 "adopted": True}


def networks_channel_delete(data, client):
    """Forget a channel: the console drops the name and key, so it can no longer render
    the QR or apply the channel to a gateway. Devices already on the channel keep talking
    to each other - deleting here revokes nothing on the air, and the confirm says so.
    First live use found the gap: create existed, delete did not (Matt, 30 Aug 2026)."""
    name = str((data or {}).get("name", ""))
    if not re.match(r"^[A-Za-z0-9_-]{1,11}$", name):
        return 400, {"error": "bad channel name"}
    channels = load_mesh_channels()
    if name not in channels:
        return 400, {"error": f"no channel called {name}"}
    del channels[name]
    try:
        save_mesh_channels(channels)
    except OSError as ex:
        return 500, {"error": f"could not write the channel store: {ex}"[:200]}
    audit({"action": "mesh-channel-delete", "target": name, "result": "OK",
           "client": client})
    return 200, {"ok": True, "name": name, "deleted": True}


def load_mesh_channels():
    try:
        with open(MESH_CHANNELS) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_mesh_channels(channels):
    os.makedirs(os.path.dirname(MESH_CHANNELS), exist_ok=True)
    tmp = MESH_CHANNELS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(channels, fh, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, MESH_CHANNELS)


def mesh_channel_url_for(name):
    """The join URL for a stored channel, or None. The only reader of the stored PSK."""
    ch = load_mesh_channels().get(name)
    if not ch:
        return None
    try:
        psk = base64.b64decode(ch.get("psk_b64", ""))
        if len(psk) != 32 or ch.get("region") not in MESH_REGIONS:
            return None
        return mesh_channel_url(name, psk, ch["region"])
    except Exception:
        return None


def networks_channel_create(data, client):
    """Create a channel, or rotate an existing one's PSK. The response says the PSK was
    stored; it never carries it."""
    name = str((data or {}).get("name", ""))
    region = str((data or {}).get("region", ""))
    if not re.match(r"^[A-Za-z0-9_-]{1,11}$", name):
        return 400, {"error": "channel name: 1 to 11 characters, letters, digits, - and _"}
    if region not in MESH_REGIONS:
        return 400, {"error": "region must be one of: " + ", ".join(sorted(MESH_REGIONS))}
    channels = load_mesh_channels()
    rotated = name in channels
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = channels.get(name) or {"created": now}
    rec.update({"psk_b64": base64.b64encode(os.urandom(32)).decode(),
                "region": region})
    if rotated:
        rec["rotated"] = now
    channels[name] = rec
    try:
        save_mesh_channels(channels)
    except OSError as ex:
        return 500, {"error": f"could not write the channel store: {ex}"[:200]}
    audit({"action": "mesh-channel-rotate" if rotated else "mesh-channel-create",
           "target": name, "result": "OK", "client": client})
    return 200, {"ok": True, "name": name, "region": region, "psk_stored": True,
                 "rotated": rotated}


def networks_channel_qr(name):
    """The join QR as a PNG, rendered by qrencode on the console box (the /eud pattern).
    Returns PNG bytes, or None when the channel is unknown or qrencode is absent. The QR
    payload embeds the PSK - that is how Meshtastic joining works - so this endpoint sits
    behind the operator session like every /api path."""
    url = mesh_channel_url_for(name)
    if not url:
        return None
    try:
        png = subprocess.run(["qrencode", "-t", "PNG", "-s", "6", "-m", "2", "-o", "-", url],
                             capture_output=True, timeout=10).stdout
    except Exception:
        png = b""
    return png or None


# ---------- the mesh map (Spec 003): where the network actually is ----------------------------
def load_mesh_pos():
    try:
        with open(MESH_POS) as fh:
            d = json.load(fh)
        return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_mesh_pos(box, lat, lon):
    """Place a gateway box on the map. Estate state, not a browser preference: stored
    server-side so another operator, another browser and a refresh all see the same map."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", str(box or "")):
        return False
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    pos = load_mesh_pos()
    pos[str(box)] = {"lat": round(lat, 6), "lon": round(lon, 6),
                     "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        os.makedirs(os.path.dirname(MESH_POS), exist_ok=True)
        tmp = MESH_POS + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(pos, fh, indent=2)
        os.replace(tmp, MESH_POS)
        return True
    except OSError:
        return False


def _snr_band(snr):
    """Meshtastic's own rule of thumb for a LoRa link, in three words an operator can act on."""
    if snr is None:
        return "unknown"
    return "good" if snr >= 5 else ("fair" if snr >= -7 else "poor")


def mesh_map_data(state, box):
    """Everything the map draws for one gateway box, or None when that box runs no mesh.

    Three honesty rules earn their place here, because a map is believed in a way a table is
    not. A node the gateway has never had a position for is NOT plotted - it is listed as
    reporting without a position, which is a real and different state from absent. The link
    from gateway to node is drawn only when we know where BOTH ends are, and a multi-hop node
    gets a link marked with its hop count rather than a route: Meshtastic tells us how many
    hops a packet took, never which nodes relayed it, so the path between the ends is unknown
    and the map must not invent one. And an unplaced box is centred on its nodes so the
    operator sees something, with placed=False so the page can say the centre is inferred."""
    t = next((x for x in state.get("targets", []) if x.get("name") == box), None)
    m = (t or {}).get("mesh") or {}
    if not t or not m:
        return None
    now = datetime.now(timezone.utc)
    nodes, unplaced, unheard = [], [], []
    for raw in (m.get("nodes") or [])[:250]:
        if not isinstance(raw, dict):
            continue
        heard = str(raw.get("heard") or "")
        age = age_seconds(heard) if heard else None
        try:
            hops = int(raw["hops"]) if raw.get("hops") is not None else None
        except (TypeError, ValueError):
            hops = None
        try:
            snr = float(raw["snr"]) if raw.get("snr") is not None else None
        except (TypeError, ValueError):
            snr = None
        # Three states, not two. A node this gateway has HEARD is on the mesh. A node it has
        # only read out of the radio's stored database has never been heard by it at all -
        # it may be switched off, or long gone, or renamed. Filing that under "no GPS fix"
        # would be a quiet lie about a device that may not be there (found live 31 Aug: a
        # tracker renamed months ago, plus a nodedb entry for one that was never on).
        n = {"id": str(raw.get("id", ""))[:16], "name": str(raw.get("name", ""))[:32],
             "battery": raw.get("battery") or 0, "heard": heard, "age": age,
             "fresh": age is not None and age <= MESH_FRESH_SECONDS,
             "heard_here": bool(raw.get("heard_here", True)) if "heard_here" in raw
             else bool(heard),
             "snr": snr, "band": _snr_band(snr), "hops": hops}
        if not n["heard_here"]:
            unheard.append(n)
            continue
        try:
            lat, lon = float(raw["lat"]), float(raw["lon"])
        except (TypeError, ValueError, KeyError):
            unplaced.append(n)
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            unplaced.append(n)
            continue
        n["lat"], n["lon"] = lat, lon
        nodes.append(n)
    saved = (load_mesh_pos().get(box) or {})
    try:
        glat, glon, placed = float(saved["lat"]), float(saved["lon"]), True
    except (TypeError, ValueError, KeyError):
        placed = False
        glat = sum(n["lat"] for n in nodes) / len(nodes) if nodes else None
        glon = sum(n["lon"] for n in nodes) / len(nodes) if nodes else None
    gateway = {"box": box, "label": t.get("label", box), "lat": glat, "lon": glon,
               "placed": placed, "placed_ts": saved.get("ts", ""),
               "state": ("no radio" if not m.get("radio_present")
                         else "flowing" if m.get("last_forwarded") else "quiet"),
               "last_forwarded": m.get("last_forwarded") or "",
               "nodes_seen": m.get("nodes_seen", 0)}
    links = []
    if glat is not None and glon is not None:
        for n in nodes:
            links.append({"id": n["id"], "lat": n["lat"], "lon": n["lon"],
                          "hops": n["hops"], "band": n["band"], "snr": n["snr"],
                          "fresh": n["fresh"],
                          # direct means the gateway's own radio heard it: one link we can
                          # draw as a real line. Anything else is "reached, via unknown".
                          "direct": n["hops"] == 0,
                          "known": n["hops"] is not None})
    # the gateway can report a node count without a node list - an older gateway that has
    # not been re-cut. Saying so is the difference between "no map yet" and "mesh is empty".
    reports_nodes = isinstance(m.get("nodes"), list)
    return {"gateway": gateway, "nodes": nodes, "unplaced": unplaced,
            "unheard": unheard, "links": links,
            "reports_nodes": reports_nodes,
            "generated_at": state.get("generated_at", ""),
            "now": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


def mesh_boxes(state):
    """The boxes that run a mesh gateway, in estate order. The map is per box: one gateway,
    one radio, one mesh - an estate-wide overlay of three separate meshes would say that
    nodes on different radios can hear each other, and they cannot."""
    return [t.get("name") for t in state.get("targets", [])
            if (t.get("mesh") or {}) and t.get("name")]


def networks_map(box, state):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", str(box or "")):
        return 400, {"error": "bad box name"}
    data = mesh_map_data(state, box)
    if data is None:
        return 404, {"error": f"no mesh gateway on a box called '{box}'"}
    return 200, data


def networks_gwpos(data, client):
    """Place (or move) a gateway box on the map."""
    box = str((data or {}).get("box", ""))
    if not save_mesh_pos(box, (data or {}).get("lat"), (data or {}).get("lon")):
        return 400, {"error": "box, lat and lon: a known box name and a real position"}
    audit({"action": "mesh-gateway-place", "target": box, "result": "OK", "client": client})
    return 200, {"ok": True, "box": box}


def render_networks(state):
    """The Networks page: mesh status per box, the map of each mesh, the channel manager,
    deploy, and the kit guide - what to buy and how to stand it up, on the page and not in
    anyone's head."""
    e = html.escape
    age = age_seconds(state.get("generated_at", ""))
    stale = age is None or age > STALE_AFTER
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    act_targets = set((cfg or {}).get("targets", {}).keys())
    targets = state.get("targets", [])
    channels = load_mesh_channels()

    doc = page_head("Networks — " + load_instance()["product_name"])
    doc.append(header_html(state, ev, age, "networks", crumb="Networks"))
    doc.append(f"<div class='vband {e(ev)}'><span class=vdot></span>"
               f"{e(estate_summary(state, ev, stale, age))}</div>")
    doc.append("<main id=main class=wrap>")
    doc.append(stale_banner(age, stale))

    # -- mesh status: what the heartbeat proves, box by box. Output, not process: a unit
    # can be active with a dead radio or an empty mesh, so the row says what actually flowed.
    doc.append("<section aria-label='Meshtastic mesh'><div class=ah>"
               "<h2 class=title>Meshtastic mesh</h2><span class=meta>Low-bandwidth LoRa "
               "radio mesh bridged into TAK: trackers and phones on the mesh appear as "
               "markers on every authenticated client in the gateway's filter group. No "
               "internet, no SIM, no infrastructure.</span></div>")
    rows = []
    for t in targets:
        m = t.get("mesh") or {}
        if not m:
            continue
        name = t.get("name", "")
        fwd = m.get("last_forwarded")
        radio_ok = bool(m.get("radio_present"))
        st = ("flowing" if fwd else ("no radio" if not radio_ok else "quiet"))
        # Each box's row opens onto its own map. The row stays the summary it always was -
        # the operator who only wants to know whether packets flow reads it and moves on -
        # and the map is one click under it for the operator who wants to SEE the mesh.
        rows.append("<tr class=mesh-row><td>" + e(t.get("label", name)) + "</td>"
                    "<td>" + e(m.get("gateway", "?")) + "</td>"
                    "<td>" + ("present" if radio_ok else "MISSING") + "</td>"
                    "<td>" + e(str(fwd or "never")) + "</td>"
                    "<td>" + e(str(m.get("nodes_seen", 0))) + "</td>"
                    "<td>" + e(st) + "</td>"
                    "<td class=mesh-mapcell><button type=button class=mesh-toggle "
                    f"data-box='{e(name)}' aria-expanded=false "
                    f"aria-controls='meshmap-{e(name)}'>"
                    "<span class=chev></span>Map</button></td></tr>")
        rows.append(f"<tr class=mesh-maprow id='meshmap-{e(name)}' hidden>"
                    "<td colspan=7><div class=meshmap-panel "
                    f"data-box='{e(name)}'></div></td></tr>")
    if rows:
        doc.append("<div class=tablewrap><table class='dtable mesh-table'>"
                   "<tr><th>Box</th><th>Gateway</th><th>Radio</th><th>Last packet "
                   "forwarded</th><th>Nodes seen</th><th>Mesh</th><th></th></tr>"
                   + "".join(rows) + "</table></div>")
    else:
        doc.append("<p class=doct>No box runs a mesh gateway yet. Deploy one below - the "
                   "kit guide at the foot of the page says what to plug in first.</p>")
    doc.append("</section>")

    # -- channels: minted here, applied to gateways, joined by scanning. The QR embeds the
    # PSK (that is how Meshtastic joining works); the page itself never carries it.
    doc.append("<section aria-label='Channels'><div class=ah>"
               "<h2 class=title>Channels</h2><span class=meta>A channel is a name and a "
               "256-bit key. Devices join by scanning its QR in the Meshtastic app; the "
               "gateway joins when you deploy it or apply the channel. Rotating a channel "
               "mints a new key: devices on the old one drop until they scan the new "
               "QR.</span></div>")
    if channels:
        for name in sorted(channels):
            ch = channels[name]
            doc.append(
                "<div class=meshchan><div class=mc-meta>"
                f"<b>{e(name)}</b><span class=hint>region {e(ch.get('region', '?'))}"
                + (" · rotated " + e(str(ch.get("rotated", ""))[:10])
                   if ch.get("rotated") else
                   " · created " + e(str(ch.get("created", ""))[:10])) + "</span>"
                f"<button type=button class=mc-rotate data-name='{e(name)}' "
                f"data-region='{e(ch.get('region', ''))}'>Rotate key</button>"
                f"<button type=button class='mc-rotate mc-del' data-name='{e(name)}'>"
                "Delete</button></div>"
                f"<img class=mc-qr alt='Join QR for channel {e(name)}' "
                f"src='/api/networks/channel/qr?name={e(name)}' "
                "onerror=\"this.replaceWith('QR needs qrencode on the console box')\">"
                "</div>")
    else:
        doc.append("<p class=doct>No channels yet. The first deploy needs one.</p>")
    doc.append("<div class=mc-new><label class=fl>Channel name"
               "<input id=mc-name maxlength=11 placeholder='e.g. OPS-MESH'></label>"
               "<label class=fl>Region<select id=mc-region>"
               + "".join(f"<option>{e(r)}</option>" for r in sorted(MESH_REGIONS)) +
               "</select><span class=hint>a legal setting: the region of the country you "
               "operate in. Re-check before transmitting abroad, and re-apply on "
               "return.</span></label>"
               "<button type=button class=a-go id=mc-create>Create channel</button>"
               "<span id=mc-res class=lib-status role=status></span></div>")
    doc.append("<div class=mc-new><label class=fl>Adopt an existing channel"
               "<input id=mc-adopt type=password autocomplete=off "
               "placeholder='https://meshtastic.org/e/#...'>"
               "<span class=hint>already running a mesh? Paste its channel link (the "
               "Meshtastic app: channel > share, or the QR's URL) and this console takes "
               "it over - no device rescans, and the deploy below can target it</span>"
               "</label>"
               "<button type=button class=a-go id=mc-adoptgo>Adopt channel</button>"
               "<span id=mc-adoptres class=lib-status role=status></span></div>")
    doc.append("</section>")

    # -- deploy: the gateway to a box of the operator's choice, as a gated job
    can_deploy = "deploy-mesh-gateway" in acts
    doc.append("<section aria-label='Deploy gateway'><div class=ah>"
               "<h2 class=title>Deploy a gateway</h2><span class=meta>Plug the radio into "
               "the box first. The deploy installs the gateway in its own environment, "
               "points it at the radio, creates the TAK input with its filter group, and "
               "starts the service. TAK Server restarts once.</span></div>")
    bundles = []
    try:
        bundles = sorted(f for f in os.listdir(os.path.join(STORE_ROOT, "mesh-gateway"))
                         if f.endswith(".tgz"))
    except OSError:
        pass
    if not can_deploy:
        doc.append("<p class=doct>The deploy-mesh-gateway action is not enabled on this "
                   "console. Re-run enrolment on a box to add it, then return here.</p>")
    elif not channels:
        doc.append("<p class=doct>Create a channel above first - the gateway joins it as "
                   "part of the deploy.</p>")
    elif not bundles:
        doc.append("<p class=doct>No gateway bundle on the shelf. Cut one with "
                   "cut-bundle.sh (it vendors the patched gateway and its wheels), then "
                   "upload it to <a href='/store'>Store</a> > mesh-gateway and return "
                   "here.</p>")
    else:
        opts = "".join(f"<option>{e(t.get('name', ''))}</option>" for t in targets
                       if t.get("name") in act_targets)
        chopts = "".join(f"<option>{e(n)}</option>" for n in sorted(channels))
        bopts = "".join(f"<option>{e(b)}</option>" for b in bundles)
        doc.append(
            "<div class=mc-new>"
            "<label class=fl>Box<select id=md-target>" + opts + "</select></label>"
            "<label class=fl>Radio serial device<input id=md-serial "
            "placeholder='/dev/serial/by-id/usb-...'>"
            "<span class=hint>on the box: ls -l /dev/serial/by-id/ - use the by-id path, "
            "never ttyACM0, those renumber on reboot</span></label>"
            "<label class=fl>Region<select id=md-region>"
            + "".join(f"<option>{e(r)}</option>" for r in sorted(MESH_REGIONS))
            + "</select></label>"
            "<label class=fl>Channel<select id=md-channel>" + chopts + "</select></label>"
            "<label class=fl>TAK filter group<input id=md-group value='mesh' maxlength=40>"
            "<span class=hint>only clients whose certificate carries this group see mesh "
            "markers - set at creation, the input manager cannot add it later</span></label>"
            "<label class=fl>Gateway bundle<select id=md-bundle>" + bopts + "</select>"
            "<span class=hint>from Store > mesh-gateway; pushed to the box and verified "
            "by hash at both ends</span></label>"
            "<button type=button class='a-go confirm' id=md-go>Deploy gateway</button>"
            "<span id=md-res class=lib-status role=status></span>"
            "<pre id=md-log class=deplog hidden></pre></div>")
    doc.append("</section>")

    # -- the kit guide: full instructions and the recommended hardware, on the page
    doc.append(
        "<section aria-label='Kit guide'><div class=ah>"
        "<h2 class=title>Kit guide</h2><span class=meta>What to buy and how to stand a "
        "mesh up, end to end.</span></div>"
        "<h3 class=mc-h>Recommended hardware</h3>"
        "<ul class=mc-list>"
        "<li><b>Gateway radio: Heltec V4</b> (ESP32, USB-C). Proven on the reference "
        "estate. One free USB port on the box and a USB-C data cable - check the cable "
        "carries data, not just charge.</li>"
        "<li><b>Trackers: Seeed T1000-E</b> (card-sized, GPS, days of battery). Run "
        "Meshtastic firmware <b>2.6.11</b>: 2.7.x has a GPS-detection regression on this "
        "hardware. Verify GPS on one device before rolling any new firmware to the "
        "fleet.</li>"
        "<li><b>Phones and EUDs</b>: install the Meshtastic app, scan the channel QR "
        "above, pair a radio over Bluetooth or USB.</li>"
        "<li><b>Antennas</b>: use the fitted 868 MHz antenna or better. Gateway transmit "
        "power defaults low for the bench; raise it for the field within your region's "
        "ERP cap (14 dBm in EU_868).</li></ul>"
        "<h3 class=mc-h>Standing it up</h3>"
        "<ol class=mc-list>"
        "<li>Plug the gateway radio into the box. Find its stable path: "
        "<code>ls -l /dev/serial/by-id/</code>.</li>"
        "<li>Create a channel above, in your region.</li>"
        "<li>Deploy the gateway to the box: radio path, region, channel, filter group. "
        "TAK Server restarts once.</li>"
        "<li>On each tracker and phone: Meshtastic app, scan the channel QR, set the "
        "same region.</li>"
        "<li>Prove it end to end: a marker from a tracker on a TAK client that signed in "
        "normally. The mesh table above shows the last packet forwarded - a running "
        "gateway that has never forwarded is quiet, not healthy.</li></ol>"
        "</section>")

    doc.append("</main>")
    # The map's configuration, as data rather than as generated JavaScript: the key and the
    # freshness window are read out of a JSON blob the page cannot be tricked into executing.
    inst = load_instance()
    doc.append("<script type=application/json id=meshmap-cfg>"
               + json.dumps({"key": inst.get("maps_key", ""),
                             "fresh": MESH_FRESH_SECONDS}).replace("<", "\\u003c")
               + "</script>")
    doc.append(f"<script>{NETWORKS_JS}</script>")
    doc.append("</body></html>")
    return "".join(doc)


NETWORKS_JS = r"""
(function(){
  function J(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(b)}).then(function(r){return r.json().then(function(j){
      return {code:r.status,j:j};});});}
  var cr=document.getElementById('mc-create');
  if(cr)cr.onclick=function(){
    var res=document.getElementById('mc-res');
    J('/api/networks/channel',{name:(document.getElementById('mc-name').value||'').trim(),
                               region:document.getElementById('mc-region').value})
      .then(function(x){if(x.code===200){location.reload();}
        else{res.textContent=x.j.error||'failed';}})
      .catch(function(){res.textContent='could not reach the console';});};
  var ad=document.getElementById('mc-adoptgo');
  if(ad)ad.onclick=function(){
    var res=document.getElementById('mc-adoptres');
    var f=document.getElementById('mc-adopt');
    J('/api/networks/channel/adopt',{url:(f.value||'').trim()})
      .then(function(x){if(x.code===200){f.value='';location.reload();}
        else{res.textContent=x.j.error||'failed';}})
      .catch(function(){res.textContent='could not reach the console';});};
  Array.prototype.forEach.call(document.querySelectorAll('.mc-rotate'),function(b){
    b.onclick=function(){
      if(!confirm('Rotate the key for "'+b.dataset.name+'"? Devices on the old key drop '
                  +'until they scan the new QR, and the gateway needs the channel '
                  +'re-applied.'))return;
      J('/api/networks/channel',{name:b.dataset.name,region:b.dataset.region})
        .then(function(x){if(x.code===200){location.reload();}else{alert(x.j.error||'failed');}});};});
  Array.prototype.forEach.call(document.querySelectorAll('.mc-del'),function(b){
    b.onclick=function(){
      if(!confirm('Delete channel "'+b.dataset.name+'" from this console? Devices on it '
                  +'keep working - this only forgets the key here, so the QR and the '
                  +'apply action are gone. There is no undo.'))return;
      J('/api/networks/channel/delete',{name:b.dataset.name})
        .then(function(x){if(x.code===200){location.reload();}else{alert(x.j.error||'failed');}});};});
  var go=document.getElementById('md-go');
  if(go)go.onclick=function(){
    var res=document.getElementById('md-res'), log=document.getElementById('md-log');
    var target=document.getElementById('md-target').value;
    var inputs={serial:(document.getElementById('md-serial').value||'').trim(),
                region:document.getElementById('md-region').value,
                channel:document.getElementById('md-channel').value,
                filter_group:(document.getElementById('md-group').value||'').trim(),
                bundle_file:document.getElementById('md-bundle').value};
    if(!confirm('Deploy the Meshtastic TAK gateway to '+target+': radio '+inputs.serial
                +', region '+inputs.region+', channel '+inputs.channel
                +'. TAK Server restarts once - a brief outage.'))return;
    go.disabled=true;res.textContent='starting…';
    J('/api/action/deploy-mesh-gateway',{target:target,inputs:inputs,confirm:true})
      .then(function(x){
        if(x.code!==200){res.textContent=x.j.error||'refused';go.disabled=false;return;}
        res.textContent='running (job '+x.j.job+')';log.hidden=false;
        var poll=setInterval(function(){
          fetch('/api/job/'+x.j.job).then(function(r){return r.json();}).then(function(j){
            if(j.log)log.textContent=j.log;log.scrollTop=log.scrollHeight;
            if(j.status&&j.status!=='running'){clearInterval(poll);go.disabled=false;
              res.textContent=(j.status==='done')?'done - check the mesh table after the '
                +'next health sweep':'failed - read the log';}});},3000);})
      .catch(function(){res.textContent='could not reach the console';go.disabled=false;});};

  // ---- the mesh map (Spec 003) ---------------------------------------------------------
  // One map per gateway box, opened from that box's row. Google Maps where a key is set and
  // the browser can reach Google; a self-contained plan view otherwise, because a console on
  // a closed network still has to be able to SEE its mesh. Both draw the same three things:
  // where the gateway is, where the nodes are, and what the gateway can actually hear.
  var CFG={};
  try{CFG=JSON.parse(document.getElementById('meshmap-cfg').textContent)||{};}catch(e){}
  // idle -> loading -> ready | failed | authfail | nokey
  var gmapsState=CFG.key?'idle':'nokey';
  var gmapsWaiting=[];
  var gmapsDrawn=[];   // panels already drawn with Google, so a late failure can redraw them

  // A REJECTED key is not the same as no key and not the same as no network: Google's script
  // loads perfectly, calls our callback, and only then refuses to draw - leaving its own grey
  // "didn't load correctly" box where the mesh should be. gm_authFailure is Google's documented
  // signal for exactly that, and it can arrive after we have already drawn. Found by looking at
  // the page with a bad key: without this, a mistyped key or a referrer restriction that does
  // not include this console silently costs the operator the map.
  window.gm_authFailure=function(){fallBackEveryPanel();};

  function toPlan(panel,data){
    var canvas=panel.querySelector('.meshmap-canvas');
    if(!canvas)return;
    canvas.innerHTML='';
    planView(canvas,data);
    noteFallback(panel);}

  function fallBackEveryPanel(){
    gmapsState='authfail';
    var q=gmapsDrawn; gmapsDrawn=[];
    q.forEach(function(entry){toPlan(entry.panel,entry.data);});}

  // Did a basemap actually appear? That is the only question worth asking, and 'tilesloaded'
  // is Google's own answer to it. Everything else we tried was unreliable: gm_authFailure is
  // DELETED by the API as it registers, so it cannot be counted on to fire; and Google's grey
  // error box appears late, at no fixed moment, and on a rejected key sometimes not at all -
  // measured across runs on this page, one run left a blank map with no error box whatsoever.
  //
  // tilesloaded covers every way this fails with one check - bad key, a referrer restriction
  // that does not list this console, quota, no route to Google, tiles blocked upstream -
  // because all of them end the same way: no basemap. No tiles inside the window means the
  // operator gets the plan view and a sentence naming the likely cause, never a blank rectangle
  // with lines floating on it.
  function watchGoogleDrew(map,panel,data){
    var settled=false;
    var give_up=setTimeout(function(){
      if(settled)return;
      settled=true;
      fallBackEveryPanel();
      toPlan(panel,data);},6000);
    try{
      google.maps.event.addListenerOnce(map,'tilesloaded',function(){
        settled=true; clearTimeout(give_up);});
    }catch(e){ /* no event bus means no map; the timeout below still rescues the panel */ }}

  function fallbackWords(){
    if(gmapsState==='nokey')return 'plan view: no Google Maps key set (Customize > Maps key)';
    if(gmapsState==='authfail')return 'plan view: Google rejected this Maps key - check the key '
      +'and that its referrer restriction allows this console';
    return 'plan view: Google Maps did not load - this browser has no route to it';}

  function noteFallback(panel){
    var n=panel.querySelector('.meshmap-note');
    if(n&&n.textContent.indexOf('plan view')<0)n.textContent=n.textContent+' · '+fallbackWords();}

  function loadGoogleMaps(cb){
    if(gmapsState==='ready'){cb(true);return;}
    if(gmapsState==='failed'||gmapsState==='nokey'){cb(false);return;}
    gmapsWaiting.push(cb);
    if(gmapsState==='loading')return;
    gmapsState='loading';
    var done=function(ok){
      gmapsState=ok?'ready':'failed';
      var q=gmapsWaiting;gmapsWaiting=[];
      q.forEach(function(f){f(ok);});};
    // A blocked or absent network makes the script tag fire onerror; some proxies instead
    // serve something that never calls back, so a timeout is the second guard. Either way
    // the map falls back rather than sitting on a blank grey square for ever.
    var to=setTimeout(function(){if(gmapsState==='loading')done(false);},8000);
    window.__meshmapReady=function(){clearTimeout(to);done(true);};
    var sc=document.createElement('script');
    sc.async=true;
    sc.src='https://maps.googleapis.com/maps/api/js?key='+encodeURIComponent(CFG.key)
          +'&callback=__meshmapReady';
    sc.onerror=function(){clearTimeout(to);done(false);};
    document.head.appendChild(sc);}

  function esc(t){var d=document.createElement('div');d.textContent=(t==null?'':String(t));
    return d.innerHTML;}
  function ago(sec){
    if(sec==null)return 'never';
    if(sec<90)return Math.round(sec)+'s ago';
    if(sec<5400)return Math.round(sec/60)+' min ago';
    return Math.round(sec/3600)+' h ago';}
  function linkStyle(l){
    // green solid = the gateway's own radio heard it. Amber dashed = it got here, over that
    // many hops, by a route Meshtastic does not report. Grey dotted = the firmware did not
    // say. The line NEVER implies a path it does not know.
    if(l.direct)return {colour:'#2f6b2a',dash:null,weight:2.4};
    if(l.known)return {colour:'#A35C17',dash:[6,6],weight:1.8};
    return {colour:'#6A6A63',dash:[2,5],weight:1.5};}
  function linkWords(l){
    if(l.direct)return 'heard direct by the gateway';
    if(l.known)return l.hops+' hop'+(l.hops===1?'':'s')+' away, route not reported';
    return 'hop count not reported by this firmware';}

  function nodeTitle(n){
    var bits=[(n.name||n.id)];
    if(n.snr!=null)bits.push('SNR '+n.snr+' ('+n.band+')');
    if(n.battery)bits.push(n.battery+'%');
    bits.push('heard '+ago(n.age));
    return bits.join(' · ');}

  function legend(d){
    var h='<div class=meshmap-legend>'
      +'<span><i class=gw></i>gateway</span>'
      +'<span><i class=node></i>node heard recently</span>'
      +'<span><i class="node stale"></i>node gone quiet</span>'
      +'<span><i class=direct></i>direct</span>'
      +'<span><i class=hop></i>multi-hop (route unknown)</span>';
    if(d.links.some(function(l){return !l.known;}))
      h+='<span><i class=unk></i>hops not reported</span>';
    return h+'</div>';}

  // ---- the plan view: no key, no internet, still a usable picture ------------------------
  // Positions are real lat/lon projected flat onto the panel with a scale bar, not a
  // basemap. It says what it is - no terrain, no roads - so nobody mistakes it for one.
  function planView(el,d){
    var pts=d.nodes.slice();
    var gw=d.gateway;
    if(gw.lat!=null)pts=pts.concat([{lat:gw.lat,lon:gw.lon,gw:true}]);
    if(!pts.length){el.innerHTML='<div class=meshmap-empty>Nothing to plot yet.</div>';return;}
    var lats=pts.map(function(p){return p.lat;}),lons=pts.map(function(p){return p.lon;});
    var la0=Math.min.apply(null,lats),la1=Math.max.apply(null,lats);
    var lo0=Math.min.apply(null,lons),lo1=Math.max.apply(null,lons);
    var mid=(la0+la1)/2, kx=Math.max(Math.cos(mid*Math.PI/180),0.01);
    // pad, and never divide by zero when every node sits on one spot
    var spanY=Math.max((la1-la0),0.0009), spanX=Math.max((lo1-lo0)*kx,0.0009);
    var pad=0.18;
    la0=la0-spanY*pad; la1=la1+spanY*pad;
    lo0=lo0-spanX*pad/kx; lo1=lo1+spanX*pad/kx;
    if(la1-la0<1e-9){la0-=0.0005;la1+=0.0005;}
    if(lo1-lo0<1e-9){lo0-=0.0005;lo1+=0.0005;}
    var W=1000,H=380;
    var sx=function(lon){return (lon-lo0)/(lo1-lo0)*W;};
    var sy=function(lat){return H-(lat-la0)/(la1-la0)*H;};
    var sv=['<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio=xMidYMid'
            +' style="width:100%;height:100%" role=img aria-label="Plan view of the mesh">'];
    if(gw.lat!=null)d.links.forEach(function(l){
      var st=linkStyle(l);
      sv.push('<line x1='+sx(gw.lon).toFixed(1)+' y1='+sy(gw.lat).toFixed(1)
        +' x2='+sx(l.lon).toFixed(1)+' y2='+sy(l.lat).toFixed(1)
        +' stroke="'+st.colour+'" stroke-width='+(st.weight*1.4)
        +(st.dash?' stroke-dasharray="'+st.dash.map(function(v){return v*1.6;}).join(' ')+'"':'')
        +' />');});
    d.nodes.forEach(function(n){
      sv.push('<circle cx='+sx(n.lon).toFixed(1)+' cy='+sy(n.lat).toFixed(1)+' r=7 fill="'
        +(n.fresh?'#2f6b2a':'#6A6A63')+'" stroke="#fff" stroke-width=2><title>'
        +esc(nodeTitle(n))+'</title></circle>');
      sv.push('<text x='+(sx(n.lon)+11).toFixed(1)+' y='+(sy(n.lat)+4).toFixed(1)
        +' font-size=13 font-family="ui-monospace,monospace" fill="#8f9682">'
        +esc(n.name||n.id)+'</text>');});
    if(gw.lat!=null){
      sv.push('<rect x='+(sx(gw.lon)-8).toFixed(1)+' y='+(sy(gw.lat)-8).toFixed(1)
        +' width=16 height=16 fill="#B5B171" stroke="#fff" stroke-width=2><title>'
        +esc(gw.label+' (gateway)')+'</title></rect>');}
    // scale bar: without one, a flat projection invites guessing at distance
    var metresPerPx=((lo1-lo0)*kx*111320)/W;
    var target=W*0.22*metresPerPx, pow=Math.pow(10,Math.floor(Math.log(target)/Math.LN10));
    var nice=[1,2,5,10].map(function(m){return m*pow;}).filter(function(v){return v<=target*1.5;});
    var barM=nice[nice.length-1]||target, barPx=barM/metresPerPx;
    sv.push('<line x1=20 y1='+(H-20)+' x2='+(20+barPx).toFixed(1)+' y2='+(H-20)
      +' stroke="#8f9682" stroke-width=2 />'
      +'<text x=20 y='+(H-27)+' font-size=12 font-family="ui-monospace,monospace" fill="#8f9682">'
      +(barM>=1000?(barM/1000)+' km':Math.round(barM)+' m')+'</text>');
    sv.push('</svg>');
    el.className='meshmap-canvas plan';
    el.innerHTML=sv.join('');}

  function googleView(el,d,panel){
    // returns the map, so the caller can check whether it ever drew a tile
    var gw=d.gateway;
    var centre=(gw.lat!=null)?{lat:gw.lat,lng:gw.lon}
              :(d.nodes.length?{lat:d.nodes[0].lat,lng:d.nodes[0].lon}:{lat:0,lng:0});
    var map=new google.maps.Map(el,{center:centre,zoom:14,mapTypeId:'hybrid',
      streetViewControl:false,fullscreenControl:true,mapTypeControl:true});
    var info=new google.maps.InfoWindow();
    var bounds=new google.maps.LatLngBounds();
    if(gw.lat!=null){
      var gm=new google.maps.Marker({position:{lat:gw.lat,lng:gw.lon},map:map,
        title:gw.label+' (gateway)',zIndex:99,
        icon:{path:google.maps.SymbolPath.CIRCLE,scale:9,fillColor:'#B5B171',fillOpacity:1,
              strokeColor:'#ffffff',strokeWeight:2}});
      gm.addListener('click',function(){
        info.setContent('<div class=gm-pop><b>'+esc(gw.label)+'</b><br>gateway · '+esc(gw.state)
          +'<br>'+(gw.placed?'placed by an operator':'position inferred from its nodes')
          +'</div>');info.open(map,gm);});
      bounds.extend(gm.getPosition());
      d.links.forEach(function(l){
        var st=linkStyle(l);
        var opts={path:[{lat:gw.lat,lng:gw.lon},{lat:l.lat,lng:l.lon}],map:map,
          strokeColor:st.colour,strokeOpacity:st.dash?0:0.9,strokeWeight:st.weight};
        if(st.dash)opts.icons=[{icon:{path:'M 0,-1 0,1',strokeOpacity:0.9,
          strokeColor:st.colour,strokeWeight:st.weight,scale:3},offset:'0',repeat:'12px'}];
        new google.maps.Polyline(opts);});}
    d.nodes.forEach(function(n){
      var mk=new google.maps.Marker({position:{lat:n.lat,lng:n.lon},map:map,title:nodeTitle(n),
        icon:{path:google.maps.SymbolPath.CIRCLE,scale:6,
              fillColor:n.fresh?'#2f6b2a':'#6A6A63',fillOpacity:1,
              strokeColor:'#ffffff',strokeWeight:1.5}});
      var lk=d.links.filter(function(l){return l.id===n.id;})[0];
      mk.addListener('click',function(){
        info.setContent('<div class=gm-pop><b>'+esc(n.name||n.id)+'</b><br>'
          +esc(n.id)+'<br>heard '+esc(ago(n.age))
          +(n.snr!=null?'<br>SNR '+esc(n.snr)+' ('+esc(n.band)+')':'')
          +(n.battery?'<br>battery '+esc(n.battery)+'%':'')
          +(lk?'<br>'+esc(linkWords(lk)):'')+'</div>');
        info.open(map,mk);});
      bounds.extend(mk.getPosition());});
    if(!bounds.isEmpty()){
      map.fitBounds(bounds,48);
      // one point makes fitBounds zoom to the building; pull back to something readable
      google.maps.event.addListenerOnce(map,'idle',function(){
        if(map.getZoom()>17)map.setZoom(17);});}
    // click to place the gateway - the same job as the lat/lon boxes, done on the map
    map.addListener('click',function(ev){
      var la=ev.latLng.lat().toFixed(6),lo=ev.latLng.lng().toFixed(6);
      var f=panel.querySelector('.mm-lat'),g=panel.querySelector('.mm-lon');
      if(f&&g){f.value=la;g.value=lo;
        var st=panel.querySelector('.mm-posres');
        if(st)st.textContent='picked '+la+', '+lo+' - Save to place the gateway';}});
    return map;}

  function drawPanel(panel,d){
    var gw=d.gateway,e=esc;
    var note;
    if(!d.reports_nodes)
      note='This gateway reports a node count but no node list, so there is nothing to plot. '
          +'It is running a build cut before the map: re-cut and redeploy the gateway bundle.';
    else if(!d.nodes.length&&!d.unplaced.length)
      note='No nodes seen yet. A node appears here once the gateway has heard it.';
    else if(!d.nodes.length)
      note='Nodes are being heard, but none has reported a GPS position yet.';
    else
      note=d.nodes.length+' node'+(d.nodes.length===1?'':'s')+' on the map'
          +(gw.placed?'':' · gateway position inferred from its nodes, not surveyed');
    var h='<div class=meshmap-head><h3>'+e(gw.label)+' mesh</h3>'
      +'<span class=meshmap-note>'+e(note)+'</span></div>'
      +'<div class=meshmap-canvas></div>';
    if(d.nodes.length||gw.lat!=null)h+=legend(d);
    if(d.unplaced.length){
      h+='<div class=meshmap-nodes><div class=nolo><b>'+d.unplaced.length+' node'
        +(d.unplaced.length===1?'':'s')+' reporting without a position:</b> '
        +d.unplaced.map(function(n){return e(n.name||n.id)+' ('+e(ago(n.age))+')';}).join(', ')
        +'. They are on the mesh; their GPS has not fixed, or their firmware is not sending '
        +'position.</div></div>';}
    // A THIRD state, and worth its own sentence: the gateway knows of these only because the
    // radio's stored node database mentioned them when it started. It has not heard them. They
    // may be switched off, out of range, or gone - and the name shown is whatever the radio had
    // cached, which can be an old one.
    if((d.unheard||[]).length){
      h+='<div class=meshmap-nodes><div class=nolo><b>'+d.unheard.length+' in the radio\'s '
        +'database, not heard:</b> '
        +d.unheard.map(function(n){return e(n.name||n.id);}).join(', ')
        +'. The gateway has not heard these since it started - switched off, out of range, or '
        +'no longer in service. Names come from the radio\'s stored list and may be out of '
        +'date.</div></div>';}
    h+='<div class=meshmap-place>'
      +'<label class=fl>Gateway latitude<input class=mm-lat inputmode=decimal placeholder="51.5017" value="'
      +(gw.placed?e(gw.lat):'')+'"></label>'
      +'<label class=fl>Longitude<input class=mm-lon inputmode=decimal placeholder="-0.1246" value="'
      +(gw.placed?e(gw.lon):'')+'"></label>'
      +'<button type=button class="a-go mm-save">'+(gw.placed?'Move gateway':'Place gateway')
      +'</button><span class="lib-status mm-posres" role=status></span></div>';
    panel.innerHTML=h;
    var canvas=panel.querySelector('.meshmap-canvas');
    var plottable=d.nodes.length||gw.lat!=null;
    if(!plottable){
      canvas.className='meshmap-canvas plan';
      canvas.innerHTML='<div class=meshmap-empty>'+e(note)+'</div>';}
    else loadGoogleMaps(function(ok){
      // gmapsState can have turned to authfail while this panel waited its turn
      if(ok&&gmapsState==='ready'){
        try{
          var gmap=googleView(canvas,d,panel);
          gmapsDrawn.push({panel:panel,data:d});
          watchGoogleDrew(gmap,panel,d);
          return;}
        catch(err){}}
      planView(canvas,d);
      noteFallback(panel);});
    var save=panel.querySelector('.mm-save');
    if(save)save.onclick=function(){
      var st=panel.querySelector('.mm-posres');
      st.textContent='saving…';
      J('/api/networks/gwpos',{box:panel.dataset.box,
        lat:panel.querySelector('.mm-lat').value,lon:panel.querySelector('.mm-lon').value})
        .then(function(x){
          if(x.code===200){st.textContent='placed';loadPanel(panel,true);}
          else{st.textContent=x.j.error||'failed';}})
        .catch(function(){st.textContent='could not reach the console';});};}

  function loadPanel(panel,force){
    if(panel.dataset.loaded==='1'&&!force)return;
    panel.dataset.loaded='1';
    panel.innerHTML='<div class=meshmap-head><span class=meshmap-note>loading the mesh…'
                   +'</span></div>';
    fetch('/api/networks/map?box='+encodeURIComponent(panel.dataset.box))
      .then(function(r){return r.json().then(function(j){return {code:r.status,j:j};});})
      .then(function(x){
        if(x.code!==200){panel.innerHTML='<div class=meshmap-head><span class=meshmap-note>'
          +esc(x.j.error||'could not read this mesh')+'</span></div>';return;}
        drawPanel(panel,x.j);})
      .catch(function(){panel.innerHTML='<div class=meshmap-head><span class=meshmap-note>'
        +'could not reach the console</span></div>';});}

  Array.prototype.forEach.call(document.querySelectorAll('.mesh-toggle'),function(b){
    b.onclick=function(){
      var row=document.getElementById(b.getAttribute('aria-controls'));
      if(!row)return;
      var open=row.hidden;
      row.hidden=!open;
      b.setAttribute('aria-expanded',open?'true':'false');
      if(open)loadPanel(row.querySelector('.meshmap-panel'));};});
})();
"""


def render_chat(state):
    """Chat with Sam, natively in the console. The console is a paired device on Sam's OpenClaw
    gateway (sam_client.py, loopback), so the conversation works on a closed network and needs no
    browser pairing. Sam's own control UI stays linked as the fallback. Anything Sam should DO
    still comes back to the Agent page as a gated proposal - chat carries words, never keys."""
    e = html.escape
    age = age_seconds(state.get("generated_at", ""))
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    ready = sam_ready()
    doc = page_head("Chat with " + load_instance()["agent_name"] + " — " + load_instance()["product_name"])
    an = html.escape(load_instance()["agent_name"])
    doc.append(header_html(state, ev, age, "chat", crumb=an))
    doc.append("<main id=main class=wrap>")
    doc.append(f"<section aria-label='{an}'><div class=ah>"
               f"<h2 class=title>{an}</h2><span class=meta>{an} is the estate's OpenClaw "
               "agent. Ask about the tactical network or say what you need. When "
               "something needs doing, it lands below as a gated proposal for you to "
               "confirm. Runs on Claude, and on the local model when the network is "
               "closed.</span></div>")
    # merged Agent view: the pending proposals ride on this page, so tasking and the
    # confirmations that follow live in one place
    pending = [p for p in load_proposals() if p.get("status") == "pending"]
    if pending:
        cfg = load_actions_config()
        tgts = target_options(cfg, state)
        doc.append(f"<div class=proposals-h><b>{len(pending)} proposal"
                   f"{'s' if len(pending) != 1 else ''} awaiting your confirmation</b> "
                   "<span class=meta>from " + an + " - read each and confirm, or dismiss."
                   "</span></div><div class=agrid>")
        for p in pending:
            aid = p.get("aid")
            if aid in ACTIONS:
                doc.append(action_form_html(aid, targets=tgts, prefill=p.get("args") or {}))
                doc.append(f"<div class=meta style='margin:-6px 0 8px'>Rationale: "
                           f"{e(str(p.get('rationale', ''))[:200])} "
                           f"<button class=p-dismiss type=button data-id='{e(str(p.get('id', '')))}'>"
                           "Dismiss</button></div>")
        doc.append("</div>")
        doc.append(f"<script>{ACTION_JS}</script>")
    doc.append(f"<div class=schat id=schat data-paired={1 if ready else 0}>"
               "<div class=scthread id=scthread role=log aria-live=polite aria-label='Conversation with Sam'>"
               "<div class=scempty id=scempty>No messages yet. Say hello - a reply can take a "
               "little while, as the agent reads the estate before answering.</div></div>"
               "<form class=scbar id=scform>"
               "<input id=scinput maxlength=8000 autocomplete=off aria-label='Message to Sam' "
               "placeholder='Message Sam…' required>"
               "<button id=scsend type=submit>Send</button></form>")
    if ready:
        doc.append("<div class=scnote>Relayed over the loopback gateway on the NUC as a paired "
                   "device, so this works on a closed network. The conversation is not stored by "
                   "the console; the audit records that a chat happened, not what was said.</div>")
    else:
        doc.append("<div class='scnote unpaired'><b>Not paired yet.</b> The console is not paired "
                   "with Sam, so messages cannot be relayed. Run <code>bring-sam-online.sh</code> "
                   "once on the NUC (it pairs the console's device and stores the gateway token), "
                   "then reload this page.</div>")
    doc.append("</div>")
    doc.append("<div class=samcard>"
               f"<a class=samgo href='{e(SAM_CHAT_URL)}' target=_blank rel=noopener>Open Sam's own chat</a>"
               f"<div class=meta>The fallback: opens <code>{e(SAM_CHAT_URL)}</code> on the tailnet, "
               "Sam's own OpenClaw window. First visit pairs your browser with Sam (device auth); "
               "after that it just opens.</div></div>")
    doc.append("<h2 class=sec-eye>How it fits together</h2>"
               "<p class=doct>The panel above talks to Sam through the console: one POST per "
               "message, relayed to his OpenClaw gateway over loopback by the console's paired "
               "device client. The keys and the audit stay on this side; Sam holds neither. When "
               "he wants to act on the estate, his proposal lands on the Agent page and a human "
               "confirms it - the chat never carries an approval.</p>")
    doc.append(f"<script>{SAMCHAT_JS}</script>")
    doc.append(footer_html(state, acts))
    return "".join(doc)


# 1.19.0 the Store: the filestore as a file manager over everything that gets deployed -
# install packages, mission packs, map packs. Browse folders, upload, download, move, delete.
STORE_JS = """
(function(){
  var root=document.getElementById('storepage'); if(!root) return;
  var AREA=root.dataset.area||'store', VAULT=AREA==='vault';
  var DLBASE=VAULT?'/vault/file/':'/store/file/';
  var TXT=['.md','.txt','.json','.yaml','.yml','.csv','.xml','.html','.conf'];
  function isText(n){n=n.toLowerCase();return TXT.some(function(e){return n.slice(-e.length)===e;});}
  function $(s){return root.querySelector(s);}
  function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
  function mb(b){b=+b||0;return b<1024?b+' B':b<1048576?(b/1024).toFixed(0)+' KB':(b/1048576).toFixed(b<10485760?1:0)+' MB';}
  var GLYPH={
    folder:'<svg class=st-ico viewBox="0 0 16 16"><path d="M1.5 4c0-.8.7-1.5 1.5-1.5h3l1.5 1.7h6c.8 0 1.5.7 1.5 1.5v6.8c0 .8-.7 1.5-1.5 1.5H3c-.8 0-1.5-.7-1.5-1.5z" fill="#B5B171"/></svg>',
    pkg:'<svg class=st-ico viewBox="0 0 16 16"><path d="M8 1.5 14 4.5v7L8 14.5 2 11.5v-7z" fill="#113308"/><path d="M8 1.5 14 4.5 8 7.5 2 4.5z" fill="#586F7C"/><path d="M8 7.5v7" stroke="#F7F6EB" stroke-width="1"/></svg>',
    zip:'<svg class=st-ico viewBox="0 0 16 16"><rect x="3" y="1.5" width="10" height="13" rx="1.5" fill="#586F7C"/><path d="M8 2v9" stroke="#F7F6EB" stroke-width="1.4" stroke-dasharray="1.4 1.1"/></svg>',
    img:'<svg class=st-ico viewBox="0 0 16 16"><rect x="1.5" y="2.5" width="13" height="11" rx="1.5" fill="#586F7C"/><circle cx="5.4" cy="6.2" r="1.3" fill="#F7F6EB"/><path d="M3 12.5 7 8l3 3 2-2 3 3.5z" fill="#D2C78D"/></svg>',
    doc:'<svg class=st-ico viewBox="0 0 16 16"><path d="M4 1.5h5.5L13 5v9.5H4z" fill="#F7F6EB" stroke="#586F7C" stroke-width="1"/><path d="M9.5 1.5V5H13" fill="none" stroke="#586F7C" stroke-width="1"/><path d="M6 8h5M6 10h5M6 12h3.5" stroke="#586F7C" stroke-width="1"/></svg>',
    map:'<svg class=st-ico viewBox="0 0 16 16"><path d="M1.5 3.5 6 2l4 1.5L14.5 2v10.5L10 14 6 12.5l-4.5 1.5z" fill="#B5B171"/><path d="M6 2v10.5M10 3.5V14" stroke="#113308" stroke-width="1"/></svg>',
    gen:'<svg class=st-ico viewBox="0 0 16 16"><path d="M4 1.5h5.5L13 5v9.5H4z" fill="#F7F6EB" stroke="#586F7C" stroke-width="1"/><path d="M9.5 1.5V5H13" fill="none" stroke="#586F7C" stroke-width="1"/></svg>'};
  function kindOf(n){n=n.toLowerCase();
    if(/[.]deb$/.test(n))return['pkg','Debian package'];
    if(/[.]apk$/.test(n))return['pkg','Android app'];
    if(/[.](zip|tgz|gz|tar)$/.test(n))return['zip','Archive'];
    if(/[.](png|jpg|jpeg|gif|svg|webp)$/.test(n))return['img','Image'];
    if(/[.](mbtiles|kml|kmz|gpx)$/.test(n))return['map','Map data'];
    if(/[.](md|txt)$/.test(n))return['doc','Text'];
    if(/[.]pdf$/.test(n))return['doc','PDF'];
    if(/[.](json|xml|cfg|conf|pref)$/.test(n))return['doc','Configuration'];
    return['gen','Document'];}
  function fdate(iso){ if(!iso)return'\u2014';
    var d=new Date(iso); if(isNaN(d))return (iso||'').slice(0,10);
    var now=new Date(), t0=new Date(now.getFullYear(),now.getMonth(),now.getDate());
    var hm=('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
    if(d>=t0)return'Today at '+hm;
    if(d>=new Date(t0-864e5))return'Yesterday at '+hm;
    var M=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();}
  var sortK='name', sortDir=1, selected=null, lastRows=[];
  function msg(cls,t){var r=$('.st-res');r.className='a-res '+cls;r.textContent=t;}
  function ico(name){var n=name.toLowerCase();
    return n.slice(-4)==='.deb'?'📦':(n.slice(-8)==='.mbtiles'||name.indexOf('map')>=0?'🗺️':(n.slice(-4)==='.zip'?'💼':'📄'));}
  var cur='', folders=[], rootFolders=[], rootChips=[];
  function crumb(){
    var parts=cur?cur.split('/'):[], html='<a href=# data-p="">Store</a>', acc='';
    parts.forEach(function(seg){acc=acc?acc+'/'+seg:seg;html+=' <span class=st-sep>/</span> <a href=# data-p="'+esc(acc)+'">'+esc(seg)+'</a>';});
    var c=$('.st-crumb'); c.innerHTML=html;
    c.querySelectorAll('a').forEach(function(a){a.onclick=function(e){e.preventDefault();load(a.dataset.p);};});
  }
  function load(path){
    cur=path||''; crumb();
    fetch('/api/store?path='+encodeURIComponent(cur)+'&area='+AREA).then(function(r){return r.json();}).then(function(j){
      if(j.error){msg('error',j.error);return;}
      folders=(j.folders||[]).map(function(f){return f.name;});
      if(!cur) rootFolders=folders.slice();
      // the shelves are permanent tabs, like a Finder sidebar: always visible, the
      // current one lit; counts refresh whenever the root listing is fetched
      if(!cur) rootChips=(j.folders||[]).map(function(f){return {name:f.name,items:f.items};});
      var top=cur?cur.split('/')[0]:'';
      var fol=$('.st-folders');
      fol.innerHTML=rootChips.map(function(f){
        return '<button type=button class="st-folder'+(f.name===top?' on':'')+'" '
          +'data-p="'+esc(f.name)+'">'+GLYPH.folder
          +' <span class=st-fname>'+esc(f.name)+'</span>'
          +'<span class=st-fmeta>'+f.items+' item'+(f.items===1?'':'s')+'</span></button>';
      }).join('');
      fol.querySelectorAll('.st-folder').forEach(function(b){b.onclick=function(){
        select(null);load(b.dataset.p===top&&cur===b.dataset.p?'':b.dataset.p);};});
      lastRows=(j.folders&&cur?j.folders.map(function(f){
        return {name:f.name,path:(cur?cur+'/':'')+f.name,dir:true,items:f.items,
                kind:'Folder',bytes:-1,mtime:''};}):[]).concat((j.files||[]).map(function(f){
        var k=kindOf(f.name);
        return {name:f.name,path:f.path,dir:false,glyph:k[0],
                kind:k[1]+(f.arch?' \u00b7 '+f.arch:''),bytes:+f.bytes||0,
                mtime:f.modified||''};}));
      renderRows();
      var fw=$('.st-fetch-wrap'); if(fw)fw.style.display=(cur==='tak-server')?'block':'none';
    });
  }
  function renderRows(){
    var body=$('.st-tbody');
    var rows=lastRows.slice().sort(function(a,b){
      if(a.dir!==b.dir)return a.dir?-1:1;
      var va,vb;
      if(sortK==='size'){va=a.bytes;vb=b.bytes;}
      else if(sortK==='mtime'){va=a.mtime;vb=b.mtime;}
      else if(sortK==='kind'){va=a.kind;vb=b.kind;}
      else{va=a.name.toLowerCase();vb=b.name.toLowerCase();}
      return (va<vb?-1:va>vb?1:0)*sortDir;});
    root.querySelectorAll('.st-h').forEach(function(h){
      h.textContent=h.textContent.replace(/ [\u25b4\u25be]$/,'');
      if(h.dataset.k===sortK)h.textContent+=sortDir===1?' \u25b4':' \u25be';});
    body.innerHTML=rows.length?rows.map(function(r){
      var icon=r.dir?GLYPH.folder:GLYPH[r.glyph];
      return '<tr class="st-row'+(selected===r.path?' sel':'')+'" data-p="'+esc(r.path)+'" '
        +'data-dir="'+(r.dir?1:0)+'" data-n="'+esc(r.name)+'">'
        +'<td class=st-name>'+icon+' '+esc(r.name)+'</td>'
        +'<td class=st-kind>'+esc(r.kind)+'</td>'
        +'<td class=lib-num>'+(r.dir?(r.items+' item'+(r.items===1?'':'s')):mb(r.bytes))+'</td>'
        +'<td class=lib-date>'+esc(fdate(r.mtime))+'</td></tr>';
    }).join(''):'<tr><td colspan=4 class=cred-empty>This folder is empty. Drag a file in, or use choose files above.</td></tr>';
    body.querySelectorAll('.st-row').forEach(function(tr){
      tr.onclick=function(){select(selected===tr.dataset.p?null:tr.dataset.p);};
      tr.ondblclick=function(){
        if(tr.dataset.dir==='1'){select(null);load(tr.dataset.p);}
        else{location.href='/store/file/'+tr.dataset.p.split('/').map(encodeURIComponent).join('/');}
      };});
    select(selected&&rows.some(function(r){return r.path===selected;})?selected:null,true);
  }
  function select(path,soft){
    selected=path;
    root.querySelectorAll('.st-row').forEach(function(tr){
      tr.classList.toggle('sel',tr.dataset.p===selected);});
    var tb=$('.st-toolbar'); if(!tb)return;
    if(!selected){tb.hidden=true;return;}
    var row=lastRows.filter(function(r){return r.path===selected;})[0];
    if(!row){tb.hidden=true;return;}
    tb.hidden=false;
    $('.st-selname').textContent=row.name+(row.dir?' (folder)':'');
    var dl=$('.st-tb-dl');
    dl.style.display=row.dir?'none':'';
    if(!row.dir)dl.href='/store/file/'+row.path.split('/').map(encodeURIComponent).join('/');
    var ed=$('.st-tb-edit'); if(ed)ed.style.display=(!row.dir&&isText(row.name))?'':'none';
    if(ed)ed.onclick=function(){openEditor(row.path);};
    var mv=$('.st-tb-move');
    mv.style.display=row.dir?'none':'';
    var dsts=VAULT?[''].concat(rootFolders):['tak-server','mission-packs','map-packs','software'];
    mv.innerHTML='<option value="">Move to\u2026</option>'+dsts.filter(function(d){return d!==cur;})
      .map(function(d){return '<option value="'+esc(d)+'">'+(d||'(top level)')+'</option>';}).join('');
    mv.onchange=function(){if(mv.value!=='')api('/api/store/move',{from:row.path,to:mv.value},'Moved.');};
    $('.st-tb-del').onclick=function(){
      if(!confirm('Delete '+row.name+'?'))return;
      api('/api/store/delete',{path:row.path},'Deleted.');select(null);};
  }
  root.querySelectorAll('.st-h').forEach(function(h){h.onclick=function(){
    if(sortK===h.dataset.k)sortDir=-sortDir;else{sortK=h.dataset.k;sortDir=1;}
    renderRows();};});
  function api(url,body,ok){
    body.area=AREA;
    fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
      .then(function(x){msg(x.code===200?'ok':'error',x.code===200?ok:(x.j.error||'failed'));load(cur);});
  }
  function upload(file,done){
    var bar=$('.st-bar'); bar.hidden=false; bar.value=0;
    var x=new XMLHttpRequest(); x.open('POST','/api/store/upload');
    x.setRequestHeader('X-Path',cur); x.setRequestHeader('X-Filename',file.name);
    x.setRequestHeader('X-Area',AREA);
    var asel=$('.st-arch'); x.setRequestHeader('X-Arch',asel?asel.value:'amd64');
    x.upload.onprogress=function(ev){if(ev.lengthComputable)bar.value=ev.loaded/ev.total*100;};
    x.onload=function(){bar.hidden=true; var j={}; try{j=JSON.parse(x.responseText);}catch(e){}
      done(x.status===200, x.status===200?file.name:(file.name+': '+(j.error||'failed')));};
    x.onerror=function(){bar.hidden=true;done(false,file.name+': upload failed');};
    x.send(file);
  }
  function uploadMany(files){
    if(!cur&&!VAULT){msg('error','Open a folder first - files live in a category, not the root.');return;}
    var list=[].slice.call(files); if(!list.length)return;
    var i=0, ok=0, errs=[];
    (function next(){
      if(i>=list.length){
        msg(errs.length?'error':'ok',
          ok+' of '+list.length+' uploaded'+(errs.length?'. '+errs.join('; '):'.'));
        load(cur); return;
      }
      msg('','Uploading '+(i+1)+' of '+list.length+': '+list[i].name+'…');
      upload(list[i++],function(good,note){ if(good)ok++; else errs.push(note); next(); });
    })();
  }
  var drop=$('.st-drop');
  ['dragenter','dragover'].forEach(function(ev){drop.addEventListener(ev,function(e){e.preventDefault();drop.classList.add('over');});});
  ['dragleave','drop'].forEach(function(ev){drop.addEventListener(ev,function(e){e.preventDefault();drop.classList.remove('over');});});
  drop.addEventListener('drop',function(e){uploadMany(e.dataTransfer.files);});
  $('.st-pick').onchange=function(){uploadMany(this.files);this.value='';};
  $('.st-newfolder').onclick=function(){var n=prompt('New folder name:'); if(n)api('/api/store/mkdir',{path:cur,name:n},'Folder created.');};
  if($('.st-fetch'))$('.st-fetch').onclick=function(){
    var url=$('.st-url').value.trim(); if(!url){msg('error','Paste a URL first.');return;}
    msg('','Fetching\u2026'); var log=$('.st-log'); log.textContent='';
    fetch('/api/library/fetch',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:url,arch:($('.st-arch')?$('.st-arch').value:'amd64')})})
      .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
      .then(function(x){ if(x.code!==200){msg('error',x.j.error||'did not start');return;}
        var poll=setInterval(function(){fetch('/api/job/'+x.j.job).then(function(r){return r.json();}).then(function(j){
          log.textContent=j.log||''; log.scrollTop=log.scrollHeight;
          if(j.status==='running'){msg('','Fetching\u2026');return;}
          clearInterval(poll); msg(j.status==='done'?'ok':'error',j.status==='done'?'In the library.':'Fetch failed - see the log.'); load(cur);
        }).catch(function(){});},2000);});
  };
  var edPath=null;
  function openEditor(path, fresh){
    var ed=$('.st-editor'); if(!ed)return;
    function show(content){
      edPath=path; ed.hidden=false;
      $('.st-ed-path').textContent=path;
      $('.st-ed-text').value=content;
      $('.st-ed-text').focus();
      ed.scrollIntoView({block:'nearest'});
    }
    if(fresh){show('');return;}
    fetch('/api/vault/read?path='+encodeURIComponent(path)).then(function(r){return r.json();})
      .then(function(j){ if(j.error){msg('error',j.error);return;} show(j.content); });
  }
  if($('.st-ed-save'))$('.st-ed-save').onclick=function(){
    fetch('/api/vault/save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:edPath,content:$('.st-ed-text').value})})
      .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
      .then(function(x){msg(x.code===200?'ok':'error',
        x.code===200?(edPath+' '+x.j.status+'. It will sync onward.'):(x.j.error||'save failed'));
        if(x.code===200)load(cur);});
  };
  if($('.st-ed-cancel'))$('.st-ed-cancel').onclick=function(){$('.st-editor').hidden=true;edPath=null;};
  if($('.st-newnote'))$('.st-newnote').onclick=function(){
    var n=prompt('New note filename (e.g. orders.md):'); if(!n)return;
    if(!/[.][A-Za-z0-9]{1,10}$/.test(n)) n=n+'.md';
    openEditor((cur?cur+'/':'')+n, true);
  };
  var ob=root.querySelector('.onto-build');
  if(ob)ob.onclick=function(){
    var ta=root.querySelector('.onto-json'), res=root.querySelector('.onto-res');
    var tree; try{tree=JSON.parse(ta.value);}catch(e){
      res.innerHTML='<div class="a-res error">That is not valid JSON: '+esc(e.message)+'</div>';return;}
    if(typeof tree!=='object'||Array.isArray(tree)){
      res.innerHTML='<div class="a-res error">The top level must be an object of folders.</div>';return;}
    res.innerHTML='<div class=a-res>Building\u2026</div>';
    fetch('/api/vault/scaffold',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ontology:'custom',tree:tree})})
    .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
    .then(function(x){
      if(x.code!==200){res.innerHTML='<div class="a-res error">'+esc(x.j.error||'failed')+'</div>';return;}
      var made=(x.j.created||[]).length,sk=(x.j.skipped||[]).length;
      res.innerHTML='<div class="a-res ok">Created '+made+' item'+(made===1?'':'s')
        +(sk?', skipped '+sk:'')+'.</div>';load('');
    }).catch(function(){res.innerHTML='<div class="a-res error">could not reach the console</div>';});
  };
  root.querySelectorAll('.onto-go').forEach(function(b){b.onclick=function(){
    var card=b.closest('.onto'), k=card.getAttribute('data-k'),
        res=root.querySelector('.onto-res');
    if(!confirm('Scaffold the "'+card.querySelector('b').textContent+'" structure into the vault?'))return;
    res.innerHTML='<div class=a-res>Building\u2026</div>';
    fetch('/api/vault/scaffold',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ontology:k})})
    .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
    .then(function(x){
      if(x.code!==200){res.innerHTML='<div class="a-res error">'+esc(x.j.error||'failed')+'</div>';return;}
      var made=(x.j.created||[]).length, sk=(x.j.skipped||[]).length;
      res.innerHTML='<div class="a-res ok">Created '+made+' item'+(made===1?'':'s')
        +(sk?', skipped '+sk+' that already existed':'')+'. Open a folder to fill it.</div>';
      load('');
    }).catch(function(){res.innerHTML='<div class="a-res error">could not reach the console</div>';});
  };});
  load('');
})();
"""


SETUP_JS = """
(function(){
  var wiz=document.getElementById('wizard'); if(!wiz) return;
  function $(s){return wiz.querySelector(s);}
  function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
  function J(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}).then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});});}
  function msg(el,cls,text){el.className='a-res '+cls;el.textContent=text;}
  var S={key:null,dest:null,tested:false,pkg:null,debOnBox:false};
  function keyname(){return ($('#wz-name').value.trim()||'x').toLowerCase().replace(/[^a-z0-9-]/g,'-').slice(0,20)+'-boot';}
  // the push lands in the admin user's home - /root only exists for root, and a
  // passwordless-sudo user cannot write there (failed live, 30 Aug 2026)
  function remoteHome(){var u=($('#wz-user').value.trim()||'root');
    return (u==='root')?'/root':'/home/'+u;}
  function unlock(n){wiz.querySelectorAll('.wz-step').forEach(function(st,i){
    st.classList.toggle('locked', i+1>n);});}

  // step 1: access
  // the placement instructions follow the Admin user field: root's keys live in
  // /root/.ssh, anyone else's in their own home - telling everyone "as root" cost
  // an operator a failed test and a support round-trip (30 Aug 2026)
  function renderPubHint(){
    var k=$('#wz-pub').textContent; if(!k) return;
    var u=($('#wz-user').value.trim()||'root');
    var a=($('#wz-addr').value.trim()||'the-box');
    var path=(u==='root')?'/root/.ssh/authorized_keys':'/home/'+u+'/.ssh/authorized_keys';
    // the command carries its own target: an operator moving between four machines
    // pasted the bare version into the wrong shell (30 Aug 2026), and a command
    // that names its destination cannot land on the wrong box
    $('#wz-pubhint').innerHTML='Run this one line from any machine that reaches the box - '
      +'it places the key for <b>'+esc(u)+'</b> in <code>'+esc(path)+'</code>:';
    $('#wz-pubcmd').textContent='ssh '+u+'@'+a+' \\'mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
      +'echo "'+k+'" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys\\'';
  }
  $('#wz-user').addEventListener('input',function(){renderPubHint();
    if(S.pkg&&!S.debOnBox)$('#wz-deb').value=remoteHome()+'/'+S.pkg.file;});
  $('#wz-addr').addEventListener('input',renderPubHint);
  $('#wz-copycmd').onclick=function(){
    // navigator.clipboard exists only on secure contexts, and this console is
    // routinely served over plain http on a tailnet - the execCommand path is
    // the one that actually runs in the field (found live, 30 Aug 2026)
    var t=$('#wz-pubcmd').textContent, b=this;
    function done(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy the command';},1500);}
    function legacy(){
      var ta=document.createElement('textarea');ta.value=t;
      ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);
      ta.focus();ta.select();
      var ok=false; try{ok=document.execCommand('copy');}catch(e){}
      document.body.removeChild(ta);
      if(ok){done();return;}
      var sel=window.getSelection(),rg=document.createRange();
      rg.selectNodeContents($('#wz-pubcmd'));sel.removeAllRanges();sel.addRange(rg);
      b.textContent='Select + copy manually';
    }
    if(navigator.clipboard&&window.isSecureContext)
      navigator.clipboard.writeText(t).then(done).catch(legacy);
    else legacy();
  };
  $('#wz-genkey').onclick=function(){
    var r=$('#wz-keyres'); msg(r,'','Generating\\u2026');
    J('/api/setup/keygen',{name:keyname()}).then(function(x){
      if(x.code!==200){msg(r,'error',x.j.error||'failed');return;}
      S.key=keyname();
      $('#wz-pub').textContent=x.j.pubkey;
      renderPubHint();
      $('#wz-pubwrap').hidden=false;
      msg(r,'ok','Key ready. Run the command on the new box, then test.');
    });
  };
  function adoptKey(body,doneNote){
    var r=$('#wz-keyres'); msg(r,'','Adopting key\\u2026');
    fetch('/api/setup/upload-key',{method:'POST',headers:{'X-Keyname':keyname()},body:body})
      .then(function(resp){return resp.json();}).then(function(){
        return J('/api/setup/userkey',{name:keyname()});
      }).then(function(x){
        if(x.code!==200){msg(r,'error',x.j.error||'key not adopted');return;}
        S.key=keyname();
        $('#wz-keytext').value=''; $('#wz-pastewrap').hidden=true;
        msg(r,'ok',doneNote+' Test the connection.');
      });
  }
  $('#wz-upkey').onchange=function(){
    var f=this.files[0]; if(!f) return;
    adoptKey(f,'Key adopted (and the uploaded copy shredded).');
  };
  $('#wz-pastekey').onclick=function(){
    var w=$('#wz-pastewrap'); w.hidden=!w.hidden;
    if(!w.hidden) $('#wz-keytext').focus();
  };
  $('#wz-adoptkey').onclick=function(){
    // normalise what password managers hand over: stray whitespace around the block,
    // CRLF line endings, and a missing final newline all make ssh reject the key
    var t=$('#wz-keytext').value.replace(/\\r\\n?/g,'\\n').replace(/^\\s+/,'');
    if(t && !/\\n$/.test(t)) t+='\\n';
    var r=$('#wz-keyres');
    if(t.indexOf('PRIVATE KEY-----')<0){
      msg(r,'error','That does not look like a private key (no BEGIN/END PRIVATE KEY lines). '
        +'In 1Password, copy the private key itself, not the public key or fingerprint.');
      return;
    }
    adoptKey(t,'Key adopted (pasted text cleared, server copy shredded).');
  };
  $('#wz-selfbox').onclick=function(){
    var host=location.hostname||'this-box';
    $('#wz-name').value=$('#wz-name').value.trim()||'first-server';
    var r=$('#wz-keyres');
    msg(r,'','Preparing this box\u2026');
    J('/api/setup/self-target',{name:keyname()}).then(function(x){
      if(x.code!==200){msg(r,'error',x.j.error||'could not prepare this box');return;}
      S.key=keyname();
      $('#wz-user').value='root'; $('#wz-addr').value='127.0.0.1';
      if(!$('#wz-name').value.trim()||$('#wz-name').value==='first-server')
        $('#wz-name').value=(x.j.host||'first-server').toLowerCase().replace(/[^a-z0-9-]/g,'-').slice(0,24);
      if(!$('#wz-label').value.trim()) $('#wz-label').value=x.j.host||'This box';
      if(!$('#wz-fqdn').value.trim()&&/[a-z].*\\./i.test(host)&&!/^[0-9.]+$/.test(host))
        $('#wz-fqdn').value=host;
      msg(r,'ok','This box is the target. Testing\u2026');
      $('#wz-test').onclick();
    });
  };
  if(/[?&]self=1/.test(location.search)) setTimeout(function(){$('#wz-selfbox').onclick();},300);

  $('#wz-test').onclick=function(){
    var dest=($('#wz-user').value.trim()||'root')+'@'+$('#wz-addr').value.trim();
    var r=$('#wz-testres');
    function go(){
      msg(r,'','Testing '+esc(dest)+'\\u2026');
      J('/api/setup/test',{name:S.key,dest:dest}).then(function(x){
        if(x.code!==200){
          msg(r,'error',x.j.error||'unreachable');
          if(/host key/i.test(x.j.error||'')){
            var b=document.createElement('button');b.type='button';b.className='cred-refresh';
            b.style.marginTop='8px';b.textContent='The box was rebuilt - forget the old key and test again';
            b.onclick=function(){b.disabled=true;
              J('/api/setup/forget-host',{name:keyname(),host:$('#wz-addr').value.trim()})
                .then(function(){go();});};
            r.appendChild(document.createElement('br'));r.appendChild(b);
          }
          return;
        }
        S.dest=dest;S.tested=true;
        msg(r,'ok','Connected: '+x.j.host+' ('+x.j.os+') as '+x.j.user
          +(x.j.addr?' at '+x.j.addr:'')+'.');
        var a=$('#wz-addr').value.trim();
        if(!$('#wz-fqdn').value.trim()&&/[a-z].*\\./i.test(a)&&!/^[0-9.]+$/.test(a))
          $('#wz-fqdn').value=a;
        unlock(2); loadLib(); depSave();
      });
    }
    if(S.key){go();return;}
    // the page may have been reloaded after a key was adopted; ask before refusing
    J('/api/setup/have',{name:keyname()}).then(function(x){
      if(x.code===200&&x.j.held){
        S.key=keyname();
        msg($('#wz-keyres'),'ok','Using the bootstrap key already held for this name.');
        go();
      }else{
        msg(r,'error','Generate, upload or paste a key first.');
      }
    });
  };

  // step 2: package library
  function loadLib(){
    fetch('/api/library').then(function(r){return r.json();}).then(function(j){
      var pk=j.packages||[], el=$('#wz-lib');
      el.innerHTML = pk.length? pk.map(function(p){
        return '<label class=wz-pkg><input type=radio name=wzpkg value="'+esc(p.file)+'" data-sha="'+esc(p.sha256)+'">'
          +'<span class=cred-name>'+esc(p.file)+'</span><span class=cred-meta>'+esc(p.arch)+' \\u00b7 '
          +(p.bytes/1048576).toFixed(0)+' MB \\u00b7 '+esc((p.uploaded||'').slice(0,10))+'</span></label>';
      }).join('') : '<div class=cred-empty>The library is empty - upload a .deb below.</div>';
      el.querySelectorAll('input[name=wzpkg]').forEach(function(i){
        if(S.pkg&&i.value===S.pkg.file){i.checked=true;unlock(3);}
        i.onchange=function(){S.pkg={file:i.value,sha:i.dataset.sha};S.debOnBox=false;
          $('#wz-deb').value=remoteHome()+'/'+i.value; unlock(3); depSave();};
      });
    });
  }
  $('#wz-updeb').onchange=function(){
    var f=this.files[0]; if(!f) return;
    var bar=$('#wz-upbar'), r=$('#wz-upres'); bar.hidden=false;
    var x=new XMLHttpRequest();
    x.open('POST','/api/library/upload');
    x.setRequestHeader('X-Filename',f.name);
    x.setRequestHeader('X-Arch',$('#wz-arch').value);
    x.upload.onprogress=function(ev){if(ev.lengthComputable)bar.value=ev.loaded/ev.total*100;};
    x.onload=function(){
      bar.hidden=true;
      var j={}; try{j=JSON.parse(x.responseText);}catch(e){}
      if(x.status!==200){msg(r,'error',j.error||'upload failed');return;}
      msg(r,'ok',j.file+' in the library ('+(j.bytes/1048576).toFixed(0)+' MB).');
      loadLib();
    };
    x.onerror=function(){bar.hidden=true;msg(r,'error','upload failed');};
    x.send(f);
  };
  $('#wz-onbox').onchange=function(){
    S.debOnBox=this.checked; if(this.checked){S.pkg=null;unlock(3);} };

  // step 3 unlocks step 4+5 when the required fields hold
  wiz.addEventListener('input',function(){
    var need=['wz-fqdn','wz-email'], ok=true;
    need.forEach(function(id){if(!$('#'+id).value.trim())ok=false;});
    if(ok&&(S.pkg||S.debOnBox)) unlock(5);
  });

  // step 4: credential rows
  function passVis(){$('#wz-passwrap').hidden=!wiz.querySelector('.wz-credrow');}
  function addCredRow(user,group){
    var row=document.createElement('div'); row.className='wz-credrow';
    row.innerHTML='<input class=wzc-user placeholder=callsign maxlength=40>'
      +'<input class=wzc-group placeholder="group, e.g. blue-team" maxlength=40>'
      +'<input class=wzc-pass type=password placeholder="password (blank = auto)" maxlength=64 autocomplete=off>'
      +'<button type=button class=wzc-del>\\u00d7</button>';
    row.querySelector('.wzc-user').value=user||'';
    if(group)row.querySelector('.wzc-group').value=group;
    row.querySelector('.wzc-del').onclick=function(){row.remove();passVis();depSave();};
    $('#wz-creds').appendChild(row); passVis();
  }
  $('#wz-addcred').onclick=function(){addCredRow('');};

  // step 5: run
  function wzGoLabel(){var d=$('#wz-dry'),g=$('#wz-go');
    if(d&&g)g.textContent=d.checked?'Preview the build (dry run)':'Build the server for real';}
  var wzd=$('#wz-dry'); if(wzd){wzd.addEventListener('change',wzGoLabel);wzGoLabel();}
  $('#wz-go').onclick=function(){
    var r=$('#wz-runres'), log=$('#wz-log');
    var creds=[].map.call(wiz.querySelectorAll('.wz-credrow'),function(row){
      var pw=row.querySelector('.wzc-pass');
      return {user:row.querySelector('.wzc-user').value.trim(),
              group:row.querySelector('.wzc-group').value.trim(),
              password_b64:(pw&&pw.value)?btoa(pw.value):''};
    }).filter(function(c){return c.user&&c.group;});
    if(creds.length&&!$('#wz-pass').value){
      msg(r,'error','Credentials need the operator passphrase - step 4.');
      $('#wz-passwrap').hidden=false; $('#wz-pass').focus();
      $('#wz-go').disabled=false; return;
    }
    var body={name:S.key,dest:S.dest,estate_name:$('#wz-name').value.trim(),
      label:$('#wz-label').value.trim(),profile:$('#wz-profile').value,
      deb_file:S.pkg?S.pkg.file:'', deb_sha256:S.pkg?S.pkg.sha:'',
      provision:{fqdn:$('#wz-fqdn').value.trim(),ca_pass:($('#wz-capass')?$('#wz-capass').value:''),
        le_email:$('#wz-email').value.trim(),
        org:$('#wz-org').value.trim(),org_unit:$('#wz-orgunit').value.trim(),
        country:$('#wz-country').value.trim(),state:$('#wz-state').value.trim(),
        city:$('#wz-city').value.trim(),deb:$('#wz-deb').value.trim(),
        components:$('#wz-components').value.trim(),
        dry_run:$('#wz-dry').checked?'1':'0'},
      creds:creds,passphrase:$('#wz-pass').value,confirm:true,
      with_console:($('#wz-console')&&$('#wz-console').checked)?'1':'0'};
    var eo=$('#wz-enrolonly'); if(eo&&eo.checked){body.enrol_only='1';body.creds=[];}
    $('#wz-go').disabled=true; msg(r,'','Starting\\u2026'); log.textContent='';
    J('/api/setup/run',body).then(function(x){
      if(x.code!==200){$('#wz-go').disabled=false;msg(r,'error',x.j.error||'did not start');return;}
      watchJob(x.j.job);
    });
  };
  function showCreds(j){
    if(j.creds&&j.creds.length){
      var out=j.creds.map(function(c){
        return '<div class=cred-enrol><div class="a-res ok">'+esc(c.user)+' ('+esc(c.group)+')</div>'
          +(c.png?'<img class=cred-qr alt="QR for '+esc(c.user)+'" src="data:image/png;base64,'+c.png+'">':'')
          +'<div class=cred-lines><code>'+esc(c.itak||'')+'</code></div></div>';
      }).join('');
      $('#wz-credout').innerHTML=out;
    }
  }
  function jobVerdict(j){
    if(j.status!=='done')return ['error','Failed (rc '+j.rc+') - the log says where. The bootstrap key is kept for the retry.'];
    if(j.dry)return ['ok','Dry run finished cleanly. Read the log, untick dry run, run again - the key is held for the live run.'];
    return ['ok','Finished. The box is enrolled; credentials below.'];
  }
  function watchJob(job){
    var r=$('#wz-runres'), log=$('#wz-log');
    $('#wz-go').disabled=true;
    var poll=setInterval(function(){
      fetch('/api/job/'+job).then(function(rr){return rr.json();}).then(function(j){
        log.textContent=j.log||'';log.scrollTop=log.scrollHeight;
        if(j.status==='running'){msg(r,'','Running\\u2026');return;}
        clearInterval(poll);$('#wz-go').disabled=false;
        var v=jobVerdict(j); msg(r,v[0],v[1]);
        showCreds(j);
      }).catch(function(){});
    },2500);
  }
  /* 1.26.0 (card 6172): a deployment is a named server-side record, not page state.
     Everything the operator types autosaves into it (debounced, no secrets), the
     strip above the wizard lists the records, and clicking one reopens it. */
  var depT=null;
  function depState(){
    return {name:$('#wz-name').value.trim(),label:$('#wz-label').value.trim(),
      address:$('#wz-addr').value.trim(),user:$('#wz-user').value.trim(),
      profile:$('#wz-profile').value,
      deb_file:S.pkg?S.pkg.file:'',deb_sha256:S.pkg?S.pkg.sha:'',
      fqdn:$('#wz-fqdn').value.trim(),le_email:$('#wz-email').value.trim(),
      org:$('#wz-org').value.trim(),org_unit:$('#wz-orgunit').value.trim(),
      country:$('#wz-country').value.trim(),state:$('#wz-state').value.trim(),
      city:$('#wz-city').value.trim(),components:$('#wz-components').value.trim(),
      cred_rows:[].map.call(wiz.querySelectorAll('.wz-credrow'),function(row){
        return {user:row.querySelector('.wzc-user').value.trim(),
                group:row.querySelector('.wzc-group').value.trim()};
      }).filter(function(c){return c.user;})};
  }
  function depSave(){
    clearTimeout(depT);
    depT=setTimeout(function(){
      var d=depState();
      if(!/^[a-z0-9-]{1,24}$/.test(d.name))return;
      J('/api/setup/deployment',d).then(function(){loadDeps();});
    },800);
  }
  function depRestore(d){
    $('#wz-name').value=d.name||'';$('#wz-label').value=d.label||'';
    $('#wz-addr').value=d.address||'';$('#wz-user').value=d.user||'root';
    $('#wz-profile').value=d.profile||'cloud';
    $('#wz-fqdn').value=d.fqdn||'';$('#wz-email').value=d.le_email||'';
    $('#wz-org').value=d.org||'MilUX';$('#wz-orgunit').value=d.org_unit||'TAK';
    $('#wz-country').value=d.country||'GB';$('#wz-state').value=d.state||'England';
    $('#wz-city').value=d.city||'';$('#wz-components').value=d.components||'';
    wiz.querySelectorAll('.wzcomp').forEach(function(c){
      c.checked=(d.components||'').split(',').indexOf(c.value)>=0;});
    wiz.querySelectorAll('.wz-credrow').forEach(function(r){r.remove();});
    (d.cred_rows||[]).forEach(function(c){addCredRow(c.user,c.group);});
    if(d.deb_file&&d.deb_sha256){S.pkg={file:d.deb_file,sha:d.deb_sha256};
      $('#wz-deb').value=remoteHome()+'/'+d.deb_file;}
    passVis();
    msg($('#wz-testres'),'','Deployment reopened - press Test the connection to '
      +'continue; a bootstrap key already held for this name is found automatically.');
    unlock(1);
  }
  function loadDeps(){
    fetch('/api/setup/deployments').then(function(r){return r.json();}).then(function(j){
      var deps=j.deployments||{},names=Object.keys(deps).sort();
      var el=document.getElementById('wz-deps');
      if(!names.length){el.hidden=true;return;}
      el.innerHTML='';
      names.forEach(function(n){
        var d=deps[n],b=document.createElement('div');b.className='wz-dep';
        b.innerHTML='<span><b>'+esc(n)+'</b>'+(d.label?' \\u00b7 '+esc(d.label):'')
          +'</span><span class=meta>saved '+esc((d.updated||'').replace('T',' ').slice(0,16))+'Z</span>'
          +'<button type=button class=wzd-del title="Delete this deployment record">\\u00d7</button>';
        b.addEventListener('click',function(ev){
          if(ev.target.classList.contains('wzd-del')){
            if(!confirm("Delete the deployment record '"+n+"'? The saved form goes; "
              +"boxes, jobs and logs are untouched."))return;
            J('/api/setup/deployment/delete',{name:n}).then(loadDeps);
            return;
          }
          depRestore(d);
        });
        el.appendChild(b);
      });
      el.hidden=false;
    }).catch(function(){});
  }
  function syncComps(){
    var v=[].map.call(wiz.querySelectorAll('.wzcomp:checked'),function(c){return c.value;}).join(',');
    $('#wz-components').value=v; depSave();
  }
  wiz.querySelectorAll('.wzcomp').forEach(function(c){c.addEventListener('change',syncComps);});
  wiz.addEventListener('input',depSave);
  loadDeps();
  unlock(1);
  /* 1.25.0: a build must never be invisible because the browser moved on. On load,
     find the newest setup job: reattach live to a running one, or show the last
     one's outcome and log. */
  fetch('/api/setup/last-job').then(function(rr){return rr.json();}).then(function(lj){
    if(!lj.job)return;
    wiz.querySelectorAll('.wz-step').forEach(function(st,i){if(i===4)st.classList.remove('locked');});
    var r=$('#wz-runres');
    if(lj.status==='running'){
      msg(r,'','Reattached: setup of '+esc(lj.target)+' started '+esc((lj.started||'').slice(11,16))+'Z is still running.');
      watchJob(lj.job);
    }else{
      fetch('/api/job/'+lj.job).then(function(rr2){return rr2.json();}).then(function(j){
        $('#wz-log').textContent=j.log||'';
        var v=jobVerdict(j);
        msg(r,v[0],'Last setup ('+esc(lj.target)+', '+esc((lj.started||'').replace('T',' ').slice(0,16))+'Z): '+v[1]);
        showCreds(j);
      }).catch(function(){});
    }
  }).catch(function(){});
})();
"""

# 1.13.0 the deploy page. One POST starts the job; the page polls the job log every 2.5s and
# streams it into the pre, so a twenty-minute install reads as a running build, not a spinner.
INFRATAK_JS = r"""
(function(){
  var f=document.getElementById('itform'); if(!f) return;
  var log=document.getElementById('itlog'), st=document.getElementById('itstatus'),
      btn=document.getElementById('itgo'), poll=null;
  function setst(cls,txt){st.className='depstatus '+cls;st.textContent=txt;}
  function watch(job){
    poll=setInterval(function(){
      fetch('/api/job/'+job).then(function(r){return r.json();}).then(function(j){
        log.textContent=j.log||''; log.scrollTop=log.scrollHeight;
        if(j.status==='running'){setst('run','Running on '+j.target+'…');return;}
        clearInterval(poll); btn.disabled=false;
        setst(j.status==='done'?'ok':'err',
          j.status==='done'?'Finished. Read the log; log in at :5001, then enrol the box to monitor it.'
          :'Failed (rc '+j.rc+'). The log says why.');
      }).catch(function(){});
    },2500);
  }
  f.addEventListener('submit',function(ev){ev.preventDefault();
    var tgt=document.getElementById('ittarget').value,
        ref=document.getElementById('itref').value.trim(),
        dry=document.getElementById('itdry').checked?'1':'0';
    btn.disabled=true; log.textContent=''; setst('run','Starting…');
    fetch('/api/action/provision-infratak',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({target:tgt,inputs:{ref:ref,dry_run:dry},confirm:true})})
      .then(function(r){return r.json().then(function(j){return {code:r.status,j:j};});})
      .then(function(x){if(x.code===200&&x.j.job){watch(x.j.job);}
        else{btn.disabled=false;setst('err','Did not start: '+(x.j.error||'unknown error'));}})
      .catch(function(e){btn.disabled=false;setst('err','Could not reach the console. '+e);});
  });
})();
"""


DEPLOY_TABS_JS = """
(function(){
  var tabs=document.querySelectorAll('.dtab'); if(!tabs.length) return;
  var flows=document.querySelectorAll('.dflow');
  function show(fid){
    var found=false;
    flows.forEach(function(f){ var on=f.id==='flow-'+fid; f.hidden=!on; if(on) found=true; });
    if(!found){ fid=tabs[0].dataset.flow; flows.forEach(function(f){ f.hidden=f.id!=='flow-'+fid; }); }
    tabs.forEach(function(t){ t.classList.toggle('active', t.dataset.flow===fid); });
    try{ history.replaceState(null,'','#'+fid); }catch(e){}
  }
  tabs.forEach(function(t){ t.addEventListener('click',function(){ show(t.dataset.flow); }); });
  show((location.hash||'').replace('#','') || tabs[0].dataset.flow);
})();
"""


DEPLOY_JS = """
(function(){
  var f=document.getElementById('depform'); if(!f) return;
  var log=document.getElementById('deplog'), st=document.getElementById('depstatus'),
      btn=document.getElementById('depgo'), poll=null;
  function setst(cls,txt){st.className='depstatus '+cls;st.textContent=txt;}
  function watch(job){
    poll=setInterval(function(){
      fetch('/api/job/'+job).then(function(r){return r.json();}).then(function(j){
        log.textContent=j.log||'';log.scrollTop=log.scrollHeight;
        if(j.status==='running'){setst('run','Running on '+j.target+'\\u2026');return;}
        clearInterval(poll);btn.disabled=false;
        if(j.status==='done'){setst('ok','Finished cleanly. Read the log, then enrol the server to the estate.');}
        else{setst('err','Failed (rc '+j.rc+'). The last lines of the log say why.');}
      }).catch(function(){});
    },2500);
  }
  f.addEventListener('submit',function(ev){
    ev.preventDefault();
    var inputs={},miss=null;
    f.querySelectorAll('[data-k]').forEach(function(el){
      var v=(el.type==='checkbox')?(el.checked?'1':'0'):el.value.trim();
      inputs[el.dataset.k]=v;
      if(el.required&&!v)miss=el.dataset.k;
    });
    if(miss){setst('err','Fill in '+miss+' first.');return;}
    var tgt=document.getElementById('deptarget').value;
    btn.disabled=true;log.textContent='';setst('run','Starting\\u2026');
    fetch('/api/action/provision-server',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({target:tgt,inputs:inputs,confirm:true})})
      .then(function(r){return r.json().then(function(j){return {code:r.status,j:j};});})
      .then(function(x){
        if(x.code===200&&x.j.job){watch(x.j.job);}
        else{btn.disabled=false;setst('err','Did not start: '+(x.j.error||'unknown error'));}})
      .catch(function(e){btn.disabled=false;setst('err','Could not reach the console. '+e);});
  });
})();
"""

_DD_INST = load_instance()
DEPLOY_DEFAULTS = {"org": _DD_INST["org"], "org_unit": _DD_INST["org_unit"],
                   "country": _DD_INST["country"], "state": _DD_INST["state"],
                   "city": "Andover", "components": ""}


# 1.21.0 the Vault viewer: the console's vault page rebuilt in the image of the standalone
# Vault Viewer (code/vault-viewer, v1.1.0) - a collapsible file tree on the left, rendered
# markdown on the right with working wikilinks, tables, tasks and frontmatter, and edit in
# place. The renderer is the viewer's own, adapted from the File System Access API to this
# console's HTTP endpoints; management (upload, move, delete, new) rides the same gated APIs
# as before. Every save flows outward by the vault's own sync.
VAULT_VIEW_JS = r"""
(function(){
var root=document.getElementById('vaultpage'); if(!root) return;
var $=function(s){return root.querySelector(s);};
function el(t,c){var e=document.createElement(t); if(c)e.className=c; return e;}
function escapeHtml(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function escAttr(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');}
function msg(cls,t){var r=$('.vv-res');if(!r)return;r.className='a-res '+cls;r.textContent=t;}
var S={tree:null, mdPaths:[], allPaths:[], current:null, curFolder:'', editing:false, dirty:false};
function fileUrl(p){return '/vault/file/'+p.split('/').map(encodeURIComponent).join('/');}
function baseName(p){return p.replace(/.*\//,'').replace(/\.md$/i,'');}

/* ---------- markdown (lifted from Vault Viewer v1.1.0) ---------- */
function parseFrontmatter(src){
  var m=/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(src);
  if(!m) return {fm:null, body:src};
  return {fm:m[1], body:src.slice(m[0].length)};
}
function renderFrontmatter(fm){
  if(!fm) return '';
  var rows=[];
  fm.split(/\r?\n/).forEach(function(line){
    var mm=/^([A-Za-z0-9_\- ]+):\s*(.*)$/.exec(line);
    if(mm){rows.push([mm[1],mm[2]]);}
    else if(/^\s*-\s+/.test(line)&&rows.length){
      var item=line.replace(/^\s*-\s+/,'').trim();
      var last=rows[rows.length-1];
      last[1]=last[1]?last[1]+', '+item:item;
    }
  });
  if(!rows.length) return '';
  var h='<details class=vv-fm><summary>Properties</summary><table>';
  rows.forEach(function(r){h+='<tr><td>'+escapeHtml(r[0])+'</td><td>'+escapeHtml(r[1])+'</td></tr>';});
  return h+'</table></details>';
}
function splitRow(line){
  var s=line.trim().replace(/^\|/,'').replace(/\|$/,'');
  var cells=[],cur='',esc=false,i,ch;
  for(i=0;i<s.length;i++){ch=s[i];
    if(esc){cur+=ch;esc=false;continue;}
    if(ch==='\\'){cur+=ch;esc=true;continue;}
    if(ch==='|'){cells.push(cur.trim());cur='';}else cur+=ch;}
  cells.push(cur.trim());
  return cells;
}
function resolveNote(target){
  var t=target.replace(/#.*$/,'').trim(); if(!t)return null;
  var cands=[t,t+'.md'],i;
  for(i=0;i<cands.length;i++){ if(S.mdPaths.indexOf(cands[i])>=0) return cands[i]; }
  var lc=(/\.md$/i.test(t)?t:t+'.md').toLowerCase();
  for(i=0;i<S.mdPaths.length;i++){ if(S.mdPaths[i].toLowerCase()===lc) return S.mdPaths[i]; }
  var bare=t.replace(/.*\//,'').replace(/\.md$/i,'').toLowerCase();
  var hits=S.mdPaths.filter(function(p){return baseName(p).toLowerCase()===bare;});
  if(hits.length===1) return hits[0];
  if(hits.length>1){
    var want=t.replace(/\.md$/i,'').toLowerCase();
    var better=hits.filter(function(p){return p.toLowerCase().replace(/\.md$/,'').slice(-want.length)===want;});
    return better[0]||hits[0];
  }
  return null;
}
function resolveAsset(target){
  var t=decodeURIComponent(target).trim(), lc=t.toLowerCase(),i;
  for(i=0;i<S.allPaths.length;i++){ if(S.allPaths[i].toLowerCase()===lc) return S.allPaths[i]; }
  var wb=t.replace(/.*\//,'').toLowerCase();
  for(i=0;i<S.allPaths.length;i++){ if(S.allPaths[i].replace(/.*\//,'').toLowerCase()===wb) return S.allPaths[i]; }
  return null;
}
function wikilink(inner){
  var parts=inner.split('|'), target=parts[0].trim();
  var display=(parts[1]?parts[1].trim():null)||target.replace(/#\^?/,' › ');
  var path=resolveNote(target);
  if(path) return '<a class=wikilink data-path="'+escAttr(path)+'">'+escapeHtml(display)+'</a>';
  return '<a class="wikilink unresolved" title="Note not found">'+escapeHtml(display)+'</a>';
}
function stripTitle(url){url=url.trim();var m=/^(.*?)\s+"[^"]*"$/.exec(url);return m?m[1].trim():url;}
function mdLink(t,url){
  if(/\.md(#|$)/i.test(url)&&!/^https?:/i.test(url)){
    var path=resolveNote(decodeURIComponent(url.replace(/#.*$/,'')));
    if(path) return '<a class=wikilink data-path="'+escAttr(path)+'">'+t+'</a>';
  }
  return '<a href="'+escAttr(url)+'" target=_blank rel=noopener>'+t+'</a>';
}
function imageTag(alt,url){
  if(/^https?:|^data:/i.test(url)) return '<img src="'+escAttr(url)+'" alt="'+escAttr(alt)+'">';
  var p=resolveAsset(url);
  if(p) return '<img src="'+escAttr(fileUrl(p))+'" alt="'+escAttr(alt)+'">';
  return '<span class=vv-missing>⚠ '+escapeHtml(url)+'</span>';
}
function embedPlaceholder(inner){
  var name=inner.split('|')[0].split('#')[0].trim();
  var size=(inner.split('|')[1]||'').trim();
  if(/\.(png|jpe?g|gif|webp|svg|bmp|avif)$/i.test(name)){
    var p=resolveAsset(name), sz=/^\d+$/.test(size)?' width="'+size+'"':'';
    if(p) return '<img src="'+escAttr(fileUrl(p))+'"'+sz+' alt="'+escAttr(name)+'">';
    return '<span class=vv-missing>⚠ '+escapeHtml(name)+'</span>';
  }
  var np=resolveNote(name);
  if(np) return '<div class=vv-embed data-embed="'+escAttr(np)+'"><p class=cred-empty>Loading '+escapeHtml(name)+'…</p></div>';
  return '<div class=vv-embed><p class=cred-empty>⚠ Missing '+escapeHtml(name)+'</p></div>';
}
function inline(text){
  if(text==null) return '';
  var codes=[];
  text=escapeHtml(text);
  text=text.replace(/`([^`]+)`/g,function(m,c){codes.push(c);return ' \u0001'+(codes.length-1)+' ';});
  text=text.replace(/!\[\[([^\]]+)\]\]/g,function(m,i){return embedPlaceholder(i);});
  text=text.replace(/\[\[([^\]]+)\]\]/g,function(m,i){return wikilink(i);});
  text=text.replace(/!\[([^\]]*)\]\(([^)]+)\)/g,function(m,a,u){return imageTag(a,stripTitle(u));});
  text=text.replace(/\[([^\]]+)\]\(([^)]+)\)/g,function(m,t,u){return mdLink(t,stripTitle(u));});
  text=text.replace(/&lt;(https?:\/\/[^\s&]+)&gt;/g,function(m,u){return '<a href="'+escAttr(u)+'" target=_blank rel=noopener>'+u+'</a>';});
  text=text.replace(/\*\*([^*]+?)\*\*/g,'<strong>$1</strong>');
  text=text.replace(/(^|[^\w])__([^_]+?)__(?=[^\w]|$)/g,'$1<strong>$2</strong>');
  text=text.replace(/\*([^*\s][^*]*?)\*/g,'<em>$1</em>');
  text=text.replace(/(^|[^\w])_([^_]+?)_(?=[^\w]|$)/g,'$1<em>$2</em>');
  text=text.replace(/~~([^~]+?)~~/g,'<del>$1</del>');
  text=text.replace(/==([^=]+?)==/g,'<mark>$1</mark>');
  text=text.replace(/(^|\s)#([A-Za-z][A-Za-z0-9_\/-]*)/g,function(m,pre,tag){return pre+'<span class=vv-tag>#'+tag+'</span>';});
  text=text.replace(/  \n/g,'<br>\n').replace(/\n/g,' ');
  text=text.replace(/ \u0001(\d+) /g,function(m,n){return '<code>'+codes[n]+'</code>';});
  return text;
}
function renderTaskOrInline(text){
  var tm=/^\[([ xX\/\-])\]\s+([\s\S]*)$/.exec(text);
  if(tm){var ck=/[xX]/.test(tm[1])?' checked':'';
    return '<input type=checkbox disabled'+ck+'> '+inline(tm[2]);}
  return inline(text);
}
function renderList(lines){
  var items=lines.map(function(l){
    var m=/^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(l);
    if(m) return {indent:m[1].replace(/\t/g,'    ').length, ordered:/\d/.test(m[2]), text:m[3], cont:false};
    return {indent:9999, text:l.trim(), cont:true};
  });
  var merged=[];
  items.forEach(function(it){
    if(it.cont&&merged.length){merged[merged.length-1].text+='\n'+it.text;}
    else if(!it.cont){merged.push(it);}
  });
  var idx=0;
  function build(minIndent){
    if(idx>=merged.length) return '';
    var ordered=merged[idx].ordered;
    var html=ordered?'<ol>':'<ul>';
    while(idx<merged.length&&merged[idx].indent>=minIndent){
      var cur=merged[idx];
      if(cur.indent>minIndent){html+=build(cur.indent);continue;}
      idx++;
      var li=renderTaskOrInline(cur.text);
      var cls=/^\[([ xX\/\-])\]\s+/.test(cur.text)?' class=task':'';
      var children='';
      if(idx<merged.length&&merged[idx].indent>cur.indent){children=build(merged[idx].indent);}
      html+='<li'+cls+'>'+li+children+'</li>';
    }
    return html+(ordered?'</ol>':'</ul>');
  }
  return build(merged.length?merged[0].indent:0);
}
function blocks(src){
  var lines=src.replace(/\r\n/g,'\n').split('\n');
  var out=[],i=0,line,m;
  while(i<lines.length){
    line=lines[i];
    if(/^\s*$/.test(line)){i++;continue;}
    m=/^(\s*)(```|~~~)(.*)$/.exec(line);
    if(m){var marker=m[2],lang=m[3].trim(),code=[];i++;
      while(i<lines.length&&!new RegExp('^\\s*'+marker).test(lines[i])){code.push(lines[i]);i++;}
      i++;
      out.push('<pre><code'+(lang?' class="lang-'+escAttr(lang)+'"':'')+'>'+escapeHtml(code.join('\n'))+'</code></pre>');
      continue;}
    m=/^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if(m){var lvl=m[1].length;out.push('<h'+lvl+'>'+inline(m[2])+'</h'+lvl+'>');i++;continue;}
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)){out.push('<hr>');i++;continue;}
    if(/^\s*>/.test(line)){var buf=[];
      while(i<lines.length&&/^\s*>/.test(lines[i])){buf.push(lines[i].replace(/^\s*>\s?/,''));i++;}
      out.push('<blockquote>'+blocks(buf.join('\n'))+'</blockquote>');continue;}
    if(line.indexOf('|')>=0&&i+1<lines.length&&/^\s*\|?[\s:|\-]+\|?\s*$/.test(lines[i+1])&&/-/.test(lines[i+1])){
      var header=splitRow(line);
      var align=splitRow(lines[i+1]).map(function(c){
        var l=c.charAt(0)===':',r=c.charAt(c.length-1)===':';
        return l&&r?'center':r?'right':l?'left':'';});
      i+=2;
      var rows=[];
      while(i<lines.length&&lines[i].indexOf('|')>=0&&!/^\s*$/.test(lines[i])){rows.push(splitRow(lines[i]));i++;}
      var t='<table><thead><tr>';
      header.forEach(function(c,j){t+='<th'+(align[j]?' style="text-align:'+align[j]+'"':'')+'>'+inline(c)+'</th>';});
      t+='</tr></thead><tbody>';
      rows.forEach(function(r){t+='<tr>';r.forEach(function(c,j){t+='<td'+(align[j]?' style="text-align:'+align[j]+'"':'')+'>'+inline(c)+'</td>';});t+='</tr>';});
      out.push(t+'</tbody></table>');continue;}
    if(/^(\s*)([-*+]|\d+[.)])\s+/.test(line)){var lbuf=[];
      while(i<lines.length&&(/^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i])||(/^\s+\S/.test(lines[i])&&lbuf.length))){
        if(/^\s*$/.test(lines[i]))break;
        lbuf.push(lines[i]);i++;}
      out.push(renderList(lbuf));continue;}
    var para=[];
    while(i<lines.length&&!/^\s*$/.test(lines[i])
      &&!/^(\s*)(```|~~~)/.test(lines[i])&&!/^#{1,6}\s/.test(lines[i])
      &&!/^\s*>/.test(lines[i])&&!/^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i])
      &&!/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i])){para.push(lines[i]);i++;}
    out.push('<p>'+inline(para.join('\n').trim())+'</p>');
  }
  return out.join('\n');
}
function parseMarkdown(src){
  var r=parseFrontmatter(src);
  return renderFrontmatter(r.fm)+blocks(r.body);
}
function resolveEmbeds(container,depth){
  if(depth>2) return;
  [].forEach.call(container.querySelectorAll('.vv-embed[data-embed]'),function(box){
    var p=box.dataset.embed; box.removeAttribute('data-embed');
    fetch('/api/vault/read?path='+encodeURIComponent(p)).then(function(r){return r.json();})
      .then(function(j){
        if(j.error){box.innerHTML='<p class=cred-empty>⚠ '+escapeHtml(j.error)+'</p>';return;}
        box.innerHTML='<div class=vv-embed-t>'+escapeHtml(baseName(p))+'</div>'+parseMarkdown(j.content);
        wireLinks(box); resolveEmbeds(box,depth+1);
      });
  });
}
function wireLinks(container){
  [].forEach.call(container.querySelectorAll('a.wikilink[data-path]'),function(a){
    a.onclick=function(){openNote(a.dataset.path,true);};
  });
}

/* ---------- tree ---------- */
function renderTree(){
  var tree=$('.vv-tree'); tree.innerHTML='';
  tree.appendChild(treeNode(S.tree,''));
}
function treeNode(node,parentPath){
  var ul=el('ul');
  (node.folders||[]).forEach(function(dir){
    var li=el('li','folder collapsed');
    var row=el('div','row');
    var tw=el('span','twirl'); tw.textContent='▾';
    var ico=el('span','ico'); ico.textContent='📁';
    row.appendChild(tw);row.appendChild(ico);row.appendChild(document.createTextNode(dir.name));
    row.onclick=function(){li.classList.toggle('collapsed');selectFolder(dir.path,row);};
    row.draggable=true;
    row.addEventListener('dragstart',function(ev){
      ev.stopPropagation();
      ev.dataTransfer.setData('text/vault-path',dir.path);
      ev.dataTransfer.effectAllowed='move';});
    function hasPath(ev){var t=ev.dataTransfer.types;
      return (t.contains?t.contains('text/vault-path'):[].indexOf.call(t,'text/vault-path')>=0);}
    row.addEventListener('dragover',function(ev){
      if(hasPath(ev)){ev.preventDefault();row.classList.add('dragover');}});
    row.addEventListener('dragleave',function(){row.classList.remove('dragover');});
    row.addEventListener('drop',function(ev){
      ev.preventDefault();ev.stopPropagation();row.classList.remove('dragover');
      var from=ev.dataTransfer.getData('text/vault-path'); if(!from||from===dir.path)return;
      api('/api/store/move',{from:from,to:dir.path},'Moved into '+dir.name+'.');});
    li.appendChild(row);
    li.appendChild(treeNode(dir,dir.path));
    ul.appendChild(li);
  });
  (node.files||[]).forEach(function(f){
    var li=el('li','file'); li.dataset.path=f.path;
    var row=el('div','row');
    row.draggable=true;
    row.addEventListener('dragstart',function(ev){
      ev.dataTransfer.setData('text/vault-path',f.path);
      ev.dataTransfer.effectAllowed='move';});
    var sp=el('span','twirl');
    var ico=el('span','ico'); ico.textContent=/\.md$/i.test(f.name)?'📄':'📎';
    row.appendChild(sp);row.appendChild(ico);row.appendChild(document.createTextNode(f.name.replace(/\.md$/i,'')));
    row.onclick=function(){
      if(/\.md$|\.txt$|\.json$|\.ya?ml$|\.csv$|\.xml$|\.html$|\.conf$/i.test(f.name)) openNote(f.path,false);
      else window.location=fileUrl(f.path);
    };
    li.appendChild(row);
    ul.appendChild(li);
  });
  return ul;
}
function selectFolder(path,row){
  S.curFolder=path;
  S.current=path;S.currentIsDir=true;
  [].forEach.call(root.querySelectorAll('.vv-tree .row.selfolder'),function(r){r.classList.remove('selfolder');});
  if(row)row.classList.add('selfolder');
  $('.vv-loc').textContent=path||'(top level)';
  $('.vv-del').disabled=!path; $('.vv-ren').disabled=!path;
  if($('.vv-mp'))$('.vv-mp').disabled=!path;
  $('.vv-edit').disabled=true; $('.vv-dl').disabled=true; $('.vv-mv').disabled=true;
  var kg=root.querySelector('.kv-graph');
  if(kg&&kg.open)gvLoad();
  var sc=root.querySelector('.kv-scope');
  if(sc)sc.textContent=path?('scope: '+path):'scope: whole vault';
}
function markActive(path){
  [].forEach.call(root.querySelectorAll('.vv-tree .file.active'),function(e){e.classList.remove('active');});
  var li=root.querySelector('.vv-tree .file[data-path="'+path.replace(/["\\]/g,'\\$&')+'"]');
  if(li){li.classList.add('active');
    var p=li.parentElement;
    while(p&&!p.classList.contains('vv-tree')){if(p.classList&&p.classList.contains('folder'))p.classList.remove('collapsed');p=p.parentElement;}
    li.scrollIntoView({block:'nearest'});}
}

/* ---------- open / edit ---------- */
function guardDirty(){
  if(S.editing&&S.dirty) return confirm('Discard unsaved changes?');
  return true;
}
function openNote(path,reveal){
  if(!guardDirty()) return;
  S.editing=false;S.dirty=false;
  fetch('/api/vault/read?path='+encodeURIComponent(path)).then(function(r){return r.json();})
    .then(function(j){
      if(j.error){msg('error',j.error);return;}
      S.current=path;S.currentIsDir=false;
      S.curFolder=path.indexOf('/')>=0?path.replace(/\/[^\/]*$/,''):'';
      $('.vv-loc').textContent=S.curFolder||'(top level)';
      $('.vv-ren').disabled=false; if($('.vv-mp'))$('.vv-mp').disabled=true;
      var main=$('.vv-main');
      main.innerHTML='<div class=vv-crumb>'+escapeHtml(path)+'</div><div class="md vv-md">'+parseMarkdown(j.content)+'</div>';
      main.scrollTop=0;
      wireLinks(main); resolveEmbeds(main,0);
      markActive(path);
      $('.vv-edit').disabled=false; $('.vv-dl').disabled=false; $('.vv-del').disabled=false; $('.vv-mv').disabled=false; $('.vv-ren').disabled=false;
      S.raw=j.content;
    });
}
function enterEdit(){
  if(!S.current) return;
  S.editing=true;S.dirty=false;
  var main=$('.vv-main');
  main.innerHTML='<div class=vv-crumb>'+escapeHtml(S.current)+' — editing</div>'
    +'<textarea class=vv-ta spellcheck=false></textarea>'
    +'<div class=vv-edbar><button type=button class="vv-save cred-refresh">Save</button>'
    +'<button type=button class="vv-cancel cred-del">Cancel</button></div>';
  var ta=main.querySelector('.vv-ta'); ta.value=S.raw; ta.focus();
  ta.oninput=function(){S.dirty=true;};
  main.querySelector('.vv-save').onclick=function(){
    fetch('/api/vault/save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:S.current,content:ta.value})})
      .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
      .then(function(x){
        if(x.code!==200){msg('error',x.j.error||'save failed');return;}
        msg('ok',S.current+' saved. It will sync onward.');
        S.dirty=false; openNote(S.current,false);
      });
  };
  main.querySelector('.vv-cancel').onclick=function(){if(guardDirty()){S.editing=false;S.dirty=false;openNote(S.current,false);}};
}
function api(url,body,ok){
  body.area='vault';
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
    .then(function(x){msg(x.code===200?'ok':'error',x.code===200?ok:(x.j.error||'failed'));
      if(x.code===200)reload(); return x;});
}
var _vloc=root.querySelector('.vv-locwrap')||root.querySelector('.vv-loc');
if(_vloc){
  _vloc.addEventListener('dragover',function(ev){
    var t=ev.dataTransfer.types;
    if(t.contains?t.contains('text/vault-path'):[].indexOf.call(t,'text/vault-path')>=0){
      ev.preventDefault();_vloc.classList.add('dragover');}});
  _vloc.addEventListener('dragleave',function(){_vloc.classList.remove('dragover');});
  _vloc.addEventListener('drop',function(ev){
    ev.preventDefault();_vloc.classList.remove('dragover');
    var from=ev.dataTransfer.getData('text/vault-path'); if(!from)return;
    api('/api/store/move',{from:from,to:''},'Moved to the top level.');});
}
function reload(){
  var kg=root.querySelector('.kv-graph'); if(kg&&kg.open)gvLoad();
  fetch('/api/vault/tree').then(function(r){return r.json();}).then(function(t){
    S.tree=t; S.mdPaths=[]; S.allPaths=[];
    (function walk(n){(n.files||[]).forEach(function(f){S.allPaths.push(f.path);
      if(/\.md$/i.test(f.name))S.mdPaths.push(f.path);});
      (n.folders||[]).forEach(walk);})(t);
    renderTree();
    if(S.current&&S.allPaths.indexOf(S.current)>=0) markActive(S.current);
  });
}

/* ---------- toolbar ---------- */
$('.vv-new').onclick=function(){
  var base='New note', n=base+'.md', i=2;
  while(S.allPaths.indexOf((S.curFolder?S.curFolder+'/':'')+n)>=0){n=base+' '+(i++)+'.md';}
  var p=(S.curFolder?S.curFolder+'/':'')+n;
  fetch('/api/vault/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:p,content:''})})
    .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
    .then(function(x){if(x.code!==200){msg('error',x.j.error||'failed');return;}
      msg('ok','Note created - name it with Rename, write it with Edit.');
      reload();setTimeout(function(){openNote(p,true);enterEdit();},300);});
};
$('.vv-ren').onclick=function(){
  if(!S.current)return;
  var w=$('.vv-renwrap'), inp=$('.vv-reninp');
  w.hidden=false;
  inp.value=S.currentIsDir?S.current.replace(/.*\//,''):S.current.replace(/.*\//,'').replace(/\.md$/i,'');
  inp.focus(); inp.select();
};
function doRename(){
  var to=$('.vv-reninp').value.trim(); if(!to){$('.vv-renwrap').hidden=true;return;}
  fetch('/api/vault/rename',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({from:S.current,to:to})})
    .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
    .then(function(x){$('.vv-renwrap').hidden=true;
      if(x.code!==200){msg('error',x.j.error||'rename failed');return;}
      msg('ok','Renamed.');S.current=x.j.path;
      if(S.currentIsDir){S.curFolder=x.j.path;$('.vv-loc').textContent=x.j.path;reload();}
      else{reload();setTimeout(function(){openNote(x.j.path,true);},300);}});
}
$('.vv-renok').onclick=doRename;
$('.vv-reninp').addEventListener('keydown',function(e){
  if(e.key==='Enter'){e.preventDefault();doRename();}
  if(e.key==='Escape'){$('.vv-renwrap').hidden=true;}});
function ontoBuild(k, prefix, res){
  res.innerHTML='<div class=a-res>Building\u2026</div>';
  fetch('/api/vault/scaffold',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ontology:k,prefix:prefix})})
  .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
  .then(function(x){
    if(x.code!==200){res.innerHTML='<div class="a-res error">'+escapeHtml(x.j.error||'failed')+'</div>';return;}
    var made=(x.j.created||[]).length, sk=(x.j.skipped||[]).length;
    res.innerHTML='<div class="a-res ok">Created "'+escapeHtml(prefix)+'" - '+made+' item'
      +(made===1?'':'s')+(sk?', left '+sk+' already-existing alone':'')
      +'. Open a note and press Edit.</div>';
    reload();
  }).catch(function(){res.innerHTML='<div class="a-res error">could not reach the console</div>';});
}
root.querySelectorAll('.onto-go').forEach(function(b){b.onclick=function(){
  var card=b.closest('.onto'), res=$('.onto-res'),
      pn=card.querySelector('.onto-name'), k=card.getAttribute('data-k');
  if(pn && pn.hidden===false){
    var name=pn.querySelector('input').value.trim();
    if(!name){pn.querySelector('input').focus();return;}
    pn.hidden=true; b.textContent='Start from this';
    ontoBuild(k, name, res); return;
  }
  // first click: reveal a name field so the deployment is never dumped into the root
  if(!pn){
    pn=document.createElement('div'); pn.className='onto-name';
    pn.innerHTML='<label class=fl>Name this '+
      (k==='exercise'?'exercise':k==='blank'?'section':'deployment')+
      '<input maxlength=60 placeholder="e.g. Op Nightjar"></label>';
    b.parentNode.insertBefore(pn, b);
    b.textContent='Create';
    var inp=pn.querySelector('input'); inp.focus();
    inp.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();b.click();}});
  } else { pn.hidden=false; b.textContent='Create'; pn.querySelector('input').focus(); }
};});
var ob=root.querySelector('.onto-build');
if(ob)ob.onclick=function(){
  var ta=root.querySelector('.onto-json'), res=$('.onto-res');
  var tree; try{tree=JSON.parse(ta.value);}catch(e){
    res.innerHTML='<div class="a-res error">That is not valid JSON: '+escapeHtml(e.message)+'</div>';return;}
  if(typeof tree!=='object'||Array.isArray(tree)){
    res.innerHTML='<div class="a-res error">The top level must be an object of folders.</div>';return;}
  res.innerHTML='<div class=a-res>Building\u2026</div>';
  fetch('/api/vault/scaffold',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ontology:'custom',tree:tree})})
  .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
  .then(function(x){
    if(x.code!==200){res.innerHTML='<div class="a-res error">'+escapeHtml(x.j.error||'failed')+'</div>';return;}
    res.innerHTML='<div class="a-res ok">Created '+(x.j.created||[]).length+' items.</div>';reload();
  }).catch(function(){res.innerHTML='<div class="a-res error">could not reach the console</div>';});
};
$('.vv-newf').onclick=function(){
  var n=prompt('New folder name (in '+(S.curFolder||'top level')+'):'); if(!n)return;
  api('/api/store/mkdir',{path:S.curFolder,name:n},'Folder created.');
};
$('.vv-edit').onclick=enterEdit;
$('.vv-dl').onclick=function(){if(S.current)window.location=fileUrl(S.current);};
if($('.vv-mp'))$('.vv-mp').onclick=function(){
  if(!S.current||!S.currentIsDir)return;
  var folder=S.current, dflt=folder.replace(/.*\//,'');
  var name=prompt('Mission pack name (shown in ATAK):', dflt);
  if(name===null)return; name=(name||dflt).trim(); if(!name)return;
  msg('','Building mission pack\u2026');
  fetch('/api/vault/mission-pack',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({folder:folder,name:name})})
  .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
  .then(function(x){
    if(x.code!==200){msg('error',x.j.error||'failed');return;}
    msg('ok','Built '+x.j.pack+' ('+x.j.files+' files, '+x.j.size_mb+'MB) in File store > '
      +'mission-packs. Devices can pull it once you place it on a server.');
  }).catch(function(){msg('error','could not reach the console');});
};
$('.vv-del').onclick=function(){
  if(!S.current)return;
  var was=S.current, wasDir=S.currentIsDir;
  api('/api/store/delete',{path:was},'Deleted.').then(function(x){
    if(x.code!==200)return;
    if(wasDir){S.curFolder='';$('.vv-loc').textContent='(top level)';}
    S.current=null;S.currentIsDir=false;
    $('.vv-main').innerHTML='<div class=vv-welcome>Select a note from the tree.</div>';
    var r=$('.vv-res');
    if(!r){reload();return;}
    r.className='a-res ok';
    r.innerHTML='Deleted '+escapeHtml(was)
      +(wasDir&&x.j.notes?' ('+x.j.notes+' notes, all recoverable)':'')+'. ';
    if(x.j.trash){var u=document.createElement('button');u.type='button';u.className='cred-refresh';
      u.textContent='Undo';u.onclick=function(){
        fetch('/api/vault/restore',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({file:x.j.trash})})
        .then(function(rr){return rr.json().then(function(j){return{code:rr.status,j:j};});})
        .then(function(y){msg(y.code===200?'ok':'error',
          y.code===200?('Restored '+y.j.restored+'.'):(y.j.error||'restore failed'));reload();});};
      r.appendChild(u);}
  });
};
$('.vv-mv').onclick=function(){
  if(!S.current)return;
  var to=prompt('Move '+S.current+' to folder (blank = top level):',S.curFolder); if(to===null)return;
  api('/api/store/move',{from:S.current,to:to.trim()},'Moved.').then(function(x){
    if(x.code===200){var nb=S.current.replace(/.*\//,'');S.current=(to.trim()?to.trim()+'/':'')+nb;
      setTimeout(function(){openNote(S.current,true);},300);}
  });
};
function uploadFiles(files){
  files=[].slice.call(files); if(!files.length)return;
  var i=0,ok=0,errs=[];
  (function next(){
    if(i>=files.length){msg(errs.length?'error':'ok',ok+' of '+files.length+' uploaded'+(errs.length?'. '+errs.join('; '):'.'));reload();return;}
    var f=files[i++]; msg('','Uploading '+f.name+'…');
    var x=new XMLHttpRequest(); x.open('POST','/api/store/upload');
    x.setRequestHeader('X-Path',S.curFolder); x.setRequestHeader('X-Filename',f.name);
    x.setRequestHeader('X-Area','vault');
    x.onload=function(){if(x.status===200)ok++;else{var j={};try{j=JSON.parse(x.responseText);}catch(e){}errs.push(f.name+': '+(j.error||'failed'));}next();};
    x.onerror=function(){errs.push(f.name+': failed');next();};
    x.send(f);
  })();
}
$('.vv-up').onchange=function(){uploadFiles(this.files);this.value='';};
var panes=root.querySelector('.vv-panes');
if(panes){
  ['dragenter','dragover'].forEach(function(ev){panes.addEventListener(ev,function(e){
    if([].slice.call(e.dataTransfer.types).indexOf('Files')>=0){e.preventDefault();panes.classList.add('dropready');}});});
  ['dragleave','drop'].forEach(function(ev){panes.addEventListener(ev,function(e){panes.classList.remove('dropready');});});
  panes.addEventListener('drop',function(e){
    if(e.dataTransfer.files&&e.dataTransfer.files.length){e.preventDefault();uploadFiles(e.dataTransfer.files);}});
}
window.addEventListener('beforeunload',function(e){if(S.editing&&S.dirty){e.preventDefault();e.returnValue='';}});
/* ---------- the graph: interactive force layout ---------- */
var GV={nodes:[],edges:[],scope:'',hover:null,drag:null,raf:0};
function gvVar(n,f){var v=getComputedStyle(document.documentElement).getPropertyValue(n).trim();return v||f;}
function gvPalette(){return {
  root:'#A35C17', folder:gvVar('--gold','#B5B171'), note:gvVar('--ok','#113308'),
  line:gvVar('--line','#dcd8c3'), link:'#A35C17',
  ink:getComputedStyle(document.body).color,
  mute:gvVar('--mute','#586F7C'), card:gvVar('--card','#fff')};}
function gvRadius(k){return k==='root'?13:k==='folder'?9:6;}
function gvSize(){var cv=root.querySelector('.kv-canvas');if(!cv)return[0,0];
  var w=cv.clientWidth||cv.parentElement.clientWidth||820;cv.width=w;return[w,cv.height];}
function gvSeed(){
  var W=gvSize()[0],H=root.querySelector('.kv-canvas').height,i,idx={};
  GV.nodes.forEach(function(n,ix){idx[n.id]=ix;
    if(n.x==null){var a=ix*2.399963,r=Math.min(W,H)*0.32*Math.sqrt((ix+1)/GV.nodes.length);
      n.x=W/2+r*Math.cos(a);n.y=H/2+r*Math.sin(a);}n.vx=0;n.vy=0;});
  GV.idx=idx;GV.settle=90;
}
function gvStep(){
  var N=GV.nodes,E=GV.edges,W=gvSize()[0],H=root.querySelector('.kv-canvas').height,i,j,idx=GV.idx;
  for(i=0;i<N.length;i++)for(j=i+1;j<N.length;j++){
    var a=N[i],b=N[j],dx=b.x-a.x,dy=b.y-a.y,d2=dx*dx+dy*dy+0.01,d=Math.sqrt(d2);
    var f=1900/d2;dx/=d;dy/=d;
    if(a!==GV.drag){a.vx-=dx*f;a.vy-=dy*f;} if(b!==GV.drag){b.vx+=dx*f;b.vy+=dy*f;}}
  E.forEach(function(e){var a=N[idx[e.a]],b=N[idx[e.b]];if(!a||!b)return;
    var dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)+0.01;
    var rest=e.kind==='link'?135:78,f=(d-rest)*0.03;dx/=d;dy/=d;
    if(a!==GV.drag){a.vx+=dx*f;a.vy+=dy*f;} if(b!==GV.drag){b.vx-=dx*f;b.vy-=dy*f;}});
  var cx=W/2,cy=H/2;
  N.forEach(function(n){ if(n===GV.drag)return;
    n.vx+=(cx-n.x)*0.002;n.vy+=(cy-n.y)*0.002;
    n.x+=Math.max(-9,Math.min(9,n.vx));n.y+=Math.max(-9,Math.min(9,n.vy));
    n.vx*=0.82;n.vy*=0.82;
    n.x=Math.max(24,Math.min(W-24,n.x));n.y=Math.max(20,Math.min(H-20,n.y));});
}
function gvNeighbours(n){var set={};if(!n)return set;set[n.id]=1;
  GV.edges.forEach(function(e){if(e.a===n.id)set[e.b]=1;if(e.b===n.id)set[e.a]=1;});return set;}
function gvDraw(){
  var cv=root.querySelector('.kv-canvas');if(!cv)return;var ctx=cv.getContext('2d');
  var W=cv.width,H=cv.height,P=gvPalette(),idx=GV.idx||{};
  ctx.clearRect(0,0,W,H);
  if(!GV.nodes.length){ctx.fillStyle=P.mute;ctx.font='13px sans-serif';
    ctx.fillText('Nothing here yet - start from a template, or pick a folder in the tree.',22,42);return;}
  var near=gvNeighbours(GV.hover);
  GV.edges.forEach(function(e){var a=GV.nodes[idx[e.a]],b=GV.nodes[idx[e.b]];if(!a||!b)return;
    var lit=GV.hover&&(e.a===GV.hover.id||e.b===GV.hover.id);
    ctx.beginPath();ctx.moveTo(a.x,a.y);
    var mx=(a.x+b.x)/2,my=(a.y+b.y)/2-Math.min(26,Math.abs(b.x-a.x)*0.14);
    ctx.quadraticCurveTo(mx,my,b.x,b.y);
    if(e.kind==='link'){ctx.strokeStyle=P.link;ctx.lineWidth=lit?2.6:1.7;ctx.globalAlpha=lit?1:0.7;}
    else{ctx.strokeStyle=P.line;ctx.lineWidth=lit?1.8:1.1;ctx.globalAlpha=GV.hover&&!lit?0.3:0.75;}
    ctx.stroke();ctx.globalAlpha=1;});
  ctx.font='500 12px "IBM Plex Mono",ui-monospace,monospace';
  GV.nodes.forEach(function(n){
    var dim=GV.hover&&!near[n.id];
    var r=gvRadius(n.kind)+(n===GV.hover?2:0);
    ctx.globalAlpha=dim?0.28:1;
    ctx.beginPath();ctx.arc(n.x,n.y,r,0,7);
    ctx.fillStyle=n.kind==='root'?P.root:n.kind==='folder'?P.folder:P.note;
    ctx.fill();
    if(n.kind!=='note'){ctx.lineWidth=1.5;ctx.strokeStyle=P.card;ctx.stroke();}
    var show=(!GV.hover||!dim||n===GV.hover);
    if(show){var tw=ctx.measureText(n.label).width;
      ctx.globalAlpha=dim?0.28:0.92;
      ctx.fillStyle=P.card;
      var lx=n.x+r+5,ly=n.y-8;
      ctx.fillRect(lx-3,ly,tw+6,16);
      ctx.fillStyle=P.ink;ctx.fillText(n.label,lx,ly+12);}
    ctx.globalAlpha=1;});
}
function gvTick(){ if(GV.settle>0){gvStep();GV.settle--;} gvDraw();
  GV.raf=(GV.settle>0||GV.drag)?requestAnimationFrame(gvTick):0;}
function gvKick(){ if(!GV.raf)GV.raf=requestAnimationFrame(gvTick); }
function gvColor(k){var P=gvPalette();return k==='folder'?P.folder:k==='root'?P.root:P.note;}
function gvLoad(scope){
  if(scope===undefined)scope=S.curFolder||'';
  GV.scope=scope;GV.hover=null;GV.drag=null;
  fetch('/api/vault/graph?folder='+encodeURIComponent(scope))
  .then(function(r){return r.json();}).then(function(g){
    GV.nodes=(g.nodes||[]).map(function(n){return {id:n.id,label:n.label,kind:n.kind,x:null,y:null};});
    GV.edges=g.edges||[];gvSeed();gvKick();
    var sc=root.querySelector('.kv-scope');
    if(sc)sc.textContent=scope?('scope: '+scope):'scope: whole vault';
  }).catch(function(){});
}
var gvReset=root.querySelector('.kv-graph-all');
if(gvReset)gvReset.onclick=function(){gvLoad('');};
var kvg=root.querySelector('.kv-graph');
if(kvg){kvg.addEventListener('toggle',function(){if(kvg.open)gvLoad();});
  if(location.hash==='#graph'){kvg.open=true;gvLoad();}}
function gvAt(ev){var cv=root.querySelector('.kv-canvas'),rc=cv.getBoundingClientRect();
  var x=ev.clientX-rc.left,y=ev.clientY-rc.top,hit=null,best=400;
  GV.nodes.forEach(function(n){var dx=n.x-x,dy=n.y-y,d=dx*dx+dy*dy;
    var rr=gvRadius(n.kind)+8;if(d<rr*rr&&d<best){best=d;hit=n;}});
  return {x:x,y:y,hit:hit};}
var kvc=root.querySelector('.kv-canvas');
if(kvc){
  kvc.addEventListener('mousemove',function(ev){
    if(GV.drag){var a=gvAt(ev);GV.drag.x=a.x;GV.drag.y=a.y;gvKick();return;}
    var h=gvAt(ev).hit;
    if(h!==GV.hover){GV.hover=h;kvc.style.cursor=h?'pointer':'default';gvDraw();}});
  kvc.addEventListener('mousedown',function(ev){var a=gvAt(ev);if(a.hit){GV.drag=a.hit;GV.dragMoved=false;gvKick();}});
  window.addEventListener('mouseup',function(){ if(GV.drag){GV.drag=null;GV.settle=Math.max(GV.settle,20);gvKick();}});
  kvc.addEventListener('mousemove',function(){if(GV.drag)GV.dragMoved=true;});
  kvc.addEventListener('click',function(ev){
    if(GV.dragMoved){GV.dragMoved=false;return;}
    var h=gvAt(ev).hit;if(!h)return;
    if(h.kind==='note')openNote(h.id,true);
    else if(h.kind==='folder')gvLoad(h.id);
    else if(h.kind==='root'&&GV.scope)
      gvLoad(GV.scope.indexOf('/')>=0?GV.scope.replace(/\/[^\/]*$/,''):'');
  });
}
reload();
})();
"""


SYNC_JS = r"""
(function(){
var root=document.getElementById('syncpage'); if(!root) return;
function escapeHtml(x){var d=document.createElement('div');d.textContent=x==null?'':x;return d.innerHTML;}
function reload(){location.reload();}
function syncApi(url,body,el,ok){
  el.className='a-res';el.textContent='Working\u2026';
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)})
  .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
  .then(function(x){
    el.className='a-res '+(x.code===200?'ok':'error');
    el.textContent=x.code===200?ok(x.j):(x.j.error||'failed');
    return x;});
}
root.querySelectorAll('.sync-peer[data-id]').forEach(function(card){
  var id=card.getAttribute('data-id'), res=card.querySelector('.sp-res');
  var pull=card.querySelector('.sp-pull');
  if(pull)pull.onclick=function(){
    syncApi('/api/peers/pull',{id:id},res,function(j){
      return 'Estate refreshed: '+j.servers+' server(s), vault folders: '
        +((j.vault_folders||[]).join(', ')||'none');
    }).then(function(x){if(x.code===200)setTimeout(function(){location.reload();},900);});
  };
  var vp=card.querySelector('.sp-vpull');
  if(vp)vp.onclick=function(){
    var f=card.querySelector('.sp-folder').value; if(!f){res.textContent='pull the estate first';return;}
    syncApi('/api/vault/pull',{peer:id,folder:f},res,function(j){
      return 'Pulled '+f+': '+(j.created||[]).length+' new, '+(j.updated||[]).length
        +' updated, '+(j.kept||[]).length+' local kept (newer here).';
    }).then(function(x){if(x.code===200)setTimeout(function(){location.reload();},900);});
  };
});
var sa=root.querySelector('.sync-add');
if(sa)sa.addEventListener('submit',function(ev){ev.preventDefault();
  syncApi('/api/peers/add',{name:sa.querySelector('.pa-name').value.trim(),
    url:sa.querySelector('.pa-url').value.trim(),
    token:sa.querySelector('.pa-token').value.trim()},
    sa.querySelector('.pa-res'),function(j){
      return 'Added '+j.name+' - first pull '+j.first_pull+'. Reloading\u2026';})
  .then(function(x){if(x.code===200)setTimeout(function(){location.reload();},1200);});
});
var sm=root.querySelector('.sync-mint');
if(sm)sm.addEventListener('submit',function(ev){ev.preventDefault();
  var out=sm.querySelector('.pm-out');
  fetch('/api/peers/mint',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:sm.querySelector('.pm-name').value.trim()})})
  .then(function(r){return r.json().then(function(j){return{code:r.status,j:j};});})
  .then(function(x){
    if(x.code!==200){out.innerHTML='<div class="a-res error">'+escapeHtml(x.j.error||'failed')+'</div>';return;}
    out.innerHTML='<div class=conn-new><b>Token for '+escapeHtml(x.j.name)
      +' - shown once.</b><div class=cn-field><span>Token</span><code class=cn-tok>'
      +escapeHtml(x.j.token)+'</code></div>'
      +'<p class=meta>On the other console: Knowledge Vault \u2192 Estate sync \u2192 '
      +'Add console, with this console\u2019s URL and this token.</p></div>';
  });
});
function drawSyncMap(){
  var svg=root.querySelector('.sync-map'), el=document.getElementById('sync-data');
  if(!svg||!el)return;
  var d=JSON.parse(el.textContent);
  var cs=getComputedStyle(document.documentElement);
  var green=(cs.getPropertyValue('--ok')||'#113308').trim(),
      gold=(cs.getPropertyValue('--gold')||'#B5B171').trim(),
      warm='#A35C17', mute=(cs.getPropertyValue('--mute')||'#586F7C').trim(),
      ink=getComputedStyle(document.body).color;
  var W=900,H=300,cx=W/2,cy=H/2;
  function node(x,y,label,sub,color,r){
    return '<circle cx="'+x+'" cy="'+y+'" r="'+(r||26)+'" fill="'+color+'" opacity="0.92"/>'
      +'<text x="'+x+'" y="'+(y+(r||26)+16)+'" text-anchor="middle" fill="'+ink+'" '
      +'font-size="13" font-weight="600">'+escapeHtml(label)+'</text>'
      +(sub?'<text x="'+x+'" y="'+(y+(r||26)+31)+'" text-anchor="middle" fill="'+mute+'" font-size="11">'+escapeHtml(sub)+'</text>':'');
  }
  var out='';
  var peers=d.peers||[], n=peers.length;
  peers.forEach(function(p,i){
    var px=W-120, py=n>1?60+i*(H-120)/(n-1):cy;
    p._x=px;p._y=py;
    out+='<line x1="'+(cx+30)+'" y1="'+cy+'" x2="'+(px-30)+'" y2="'+py+'" stroke="'+mute+'" stroke-width="1.2" stroke-dasharray="5 4"/>';
  });
  (d.rules||[]).forEach(function(r){
    var p=peers.filter(function(x){return x.id===r.peer;})[0]; if(!p)return;
    var mx=(cx+p._x)/2, my=(cy+p._y)/2-14;
    out+='<line x1="'+(p._x-30)+'" y1="'+p._y+'" x2="'+(cx+30)+'" y2="'+cy+'" stroke="'+warm+'" stroke-width="2.4" marker-end="url(#syncarrow)"/>'
      +'<text x="'+mx+'" y="'+my+'" text-anchor="middle" fill="'+warm+'" font-size="12" font-weight="600">'+escapeHtml(r.folder)+' \u2192</text>'
      +'<text x="'+mx+'" y="'+(my+14)+'" text-anchor="middle" fill="'+mute+'" font-size="10">'+escapeHtml((r.last_pull||'').slice(5,16))+'</text>';
  });
  out+='<line x1="'+(cx-30)+'" y1="'+cy+'" x2="150" y2="'+cy+'" stroke="'+gold+'" stroke-width="1.6"/>';
  out+=node(cx,cy,d.me+(d.mode==='client'?' (client)':' (admin)'),
    d.local_servers+' server(s) here',green,30);
  peers.forEach(function(p){out+=node(p._x,p._y,p.name,p.servers+' server(s)',gold);});
  out+=node(110,cy,'devices','phones + EUDs via /eud',warm,20);
  svg.innerHTML='<defs><marker id="syncarrow" viewBox="0 0 10 10" refX="9" refY="5" '
    +'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    +'<path d="M0 0L10 5L0 10z" fill="'+warm+'"/></marker></defs>'+out;
}

/* discovery: dropdown of real nodes, free text only behind 'somewhere else' */
var nodeSel=root.querySelector('.pa-node'), urlInp=root.querySelector('.pa-url'),
    urlWrap=root.querySelector('.pa-urlwrap'), probeBtn=root.querySelector('.pa-probe');
function loadNodes(){
  if(!nodeSel)return;
  nodeSel.innerHTML='<option value="">probing known nodes\u2026</option>';
  fetch('/api/peers/discover',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
  .then(function(r){return r.json();}).then(function(j){
    var found=j.consoles||[];
    nodeSel.innerHTML=found.map(function(c){
      return '<option value="'+escapeHtml(c.url)+'"'+(c.already_peered?' disabled':'')+'>'
        +escapeHtml(c.label)+' \u2014 '+escapeHtml(c.url)+(c.already_peered?' (already added)':'')+'</option>';
    }).join('')+'<option value="__other">somewhere else (type a URL)\u2026</option>';
    if(urlWrap)urlWrap.hidden=true;
    var first=found.filter(function(c){return !c.already_peered;})[0];
    if(first&&urlInp)urlInp.value=first.url;
    if(!found.length){nodeSel.selectedIndex=nodeSel.options.length-1;if(urlWrap)urlWrap.hidden=false;}
  }).catch(function(){nodeSel.innerHTML='<option value="__other">type a URL\u2026</option>';if(urlWrap)urlWrap.hidden=false;});
}
if(nodeSel){
  nodeSel.addEventListener('change',function(){
    if(nodeSel.value==='__other'){if(urlWrap)urlWrap.hidden=false;if(urlInp)urlInp.value='';}
    else{if(urlWrap)urlWrap.hidden=true;if(urlInp)urlInp.value=nodeSel.value;}
  });
  loadNodes();
}
if(probeBtn)probeBtn.onclick=loadNodes;
})();
"""



DEVICES_SYNC_JS = r"""
(function(){
  var dv=document.querySelector('#vd-devices .vd-body');
  var mv=document.querySelector('#vd-moves .vd-body');
  if(!dv||!mv)return;
  function el(t,c,txt){var n=document.createElement(t); if(c)n.className=c; if(txt!=null)n.textContent=txt; return n;}
  function post(p,obj,cb){fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj||{})}).then(function(r){return r.json().then(function(j){cb(r.status,j);});}).catch(function(){cb(0,{error:'unreachable'});});}
  var lastSeen='';
  function load(){fetch('/api/deployed/overview').then(function(r){return r.text().then(function(t){
    if(r.status===200&&t===lastSeen)return;      // nothing changed: leave the page alone
    lastSeen=(r.status===200)?t:'';
    var j; try{j=JSON.parse(t);}catch(e){j={error:'bad reply'};}
    render(r.status,j);});}).catch(function(){render(0,{error:'unreachable'});});}
  // The section keeps itself current: a scan on a handset surfaces its code here within
  // seconds, with no reload. Unchanged data never re-renders, so typing is not disturbed.
  setInterval(load, 5000);
  function linkForm(msg){
    dv.textContent=''; mv.textContent='';
    mv.appendChild(el('p','meta','Link the box first, above.'));
    dv.appendChild(el('p','doct','This console is not yet linked to the box\u2019s device-sync surface. '+
      'On the box, run  vd-ops config  and copy the URL and admin token here.'));
    if(msg){dv.appendChild(el('p','vd-err',msg));}
    var u=el('input','vd-in'); u.placeholder='http://box:8095';
    var t=el('input','vd-in'); t.placeholder='admin token';
    var b=el('button','cred-refresh','Link the box'); b.type='button';
    b.onclick=function(){post('/api/deployed/link',{url:u.value.trim(),token:t.value.trim()},function(st,j){
      if(st===200){load();}else{linkForm((j&&j.error)||('refused '+st));}});};
    dv.appendChild(u); dv.appendChild(t); dv.appendChild(b);
  }
  function render(st,j){
    if(st!==200){linkForm(j&&j.error);return;}
    var o=j.overview||{};
    dv.textContent=''; mv.textContent='';

    (o.pending||[]).forEach(function(pd){
      var c=el('div','vd-card');
      c.appendChild(el('b',null,'Awaiting confirmation: '+pd.device+' / '+pd.holder+'  ('+pd.deployment+')'));
      c.appendChild(el('div','vd-code',pd.code.slice(0,3)+' '+pd.code.slice(3)));
      c.appendChild(el('p','meta','Confirm ONLY if the holder reads this code from their handset.'));
      var cb=el('button','cred-refresh','Confirm'); cb.type='button';
      cb.onclick=function(){post('/api/deployed/confirm',{fingerprint:pd.fingerprint},function(){load();});};
      var rb=el('button','cred-del','Reject'); rb.type='button';
      rb.onclick=function(){post('/api/deployed/reject',{fingerprint:pd.fingerprint},function(){load();});};
      c.appendChild(cb); c.appendChild(rb); dv.appendChild(c);});

    if(!(o.devices||[]).length){dv.appendChild(el('p','meta','No devices enrolled yet.'));}
    (o.devices||[]).forEach(function(d){
      var c=el('div','vd-card');
      c.appendChild(el('b',null,d.label+' / '+d.holder));
      c.appendChild(el('span','meta','  '+d.deployment+', '+d.ceiling));
      if(d.revoked){c.appendChild(el('span','vd-rev','  revoked'+(d.revoked_reason?': '+d.revoked_reason:'')));}
      else{var vb=el('button','cred-del','Revoke'); vb.type='button'; vb.style.marginLeft='10px';
        vb.onclick=function(){var why=prompt('Reason for revoking '+d.label+'?','end of deployment');
          if(why!=null){post('/api/deployed/revoke',{fingerprint:d.fingerprint,reason:why},function(){load();});}};
        c.appendChild(vb);}
      dv.appendChild(c);});

    var det=el('details'); det.appendChild(el('summary','onto-sum','Enrol a device'));
    var f={};
    [['device','Device (e.g. S23)'],['holder','Holder (optional: who carries it)']].forEach(function(pr){
      var i=el('input','vd-in'); i.placeholder=pr[1]; f[pr[0]]=i; det.appendChild(i);});
    det.appendChild(el('p','meta','Deployments this device carries (pick one or more):'));
    var checks=[];
    (o.deployments||[]).forEach(function(d2){
      var lab=el('label'); lab.style.display='block';
      var cb2=el('input'); cb2.type='checkbox'; cb2.value=d2;
      lab.appendChild(cb2); lab.appendChild(document.createTextNode(' '+d2));
      checks.push(cb2); det.appendChild(lab);});
    var extra=el('input','vd-in'); extra.placeholder='Or a new deployment name';
    det.appendChild(extra);
    var mb=el('button','cred-refresh','Mint enrolment QR'); mb.type='button';
    var out=el('div');
    mb.onclick=function(){
      var deps=checks.filter(function(c2){return c2.checked;}).map(function(c2){return c2.value;});
      if(extra.value.trim())deps.push(extra.value.trim());
      if(!deps.length){out.textContent='';out.appendChild(el('p','vd-err','Pick at least one deployment.'));return;}
      post('/api/deployed/mint',{device:f.device.value,holder:f.holder.value,
      deployment:deps.join('|'),ceiling:'OFFICIAL'},function(st2,j2){
      out.textContent='';
      if(st2!==200){out.appendChild(el('p','vd-err',(j2&&j2.error)||('refused '+st2)));return;}
      if(j2.qr_png_b64){var im=el('img','cred-qr'); im.alt='Enrolment QR'; im.src='data:image/png;base64,'+j2.qr_png_b64; out.appendChild(im);}
      else{out.appendChild(el('pre','vd-payload',j2.payload||''));}
      out.appendChild(el('p','meta','Single use, expires in minutes. The holder scans it in '+
        'Vantage Deployed (Settings, Join a box); their six-digit code then appears above.'));});};
    det.appendChild(mb); det.appendChild(out); dv.appendChild(det);

    if(!(o.devices||[]).length){mv.appendChild(el('p','meta','Enrol a device to see what it will receive.'));}
    (o.devices||[]).forEach(function(d){
      if(d.revoked)return;
      var labs=(d.deployment||'').split('|').map(function(x){return x.trim();}).filter(Boolean);
      var c=el('div','vd-card');
      c.appendChild(el('b',null,d.label+' ('+labs.join(', ')+') receives:'));
      var ul=el('ul');
      var anyPacks=false;
      labs.forEach(function(lab2){
        ul.appendChild(el('li',null,'Knowledge Vault: the "'+lab2+'" folder'));
        ((o.manifest&&o.manifest[lab2])||[]).forEach(function(pp){
          anyPacks=true;
          ul.appendChild(el('li',null,pp.name+'  ('+pp.kind+' pack, '+Math.round(pp.size/1024)+' KB, '+lab2+')'));});});
      if(!anyPacks){ul.appendChild(el('li','meta','No packs shared to these deployments yet - share one from the File store.'));}
      c.appendChild(ul); mv.appendChild(c);});
    var rl=el('button','cred-refresh','Refresh'); rl.type='button'; rl.onclick=load;
    dv.appendChild(rl);
  }
  load();
})();
"""


def render_sync(state):
    """The Sync tab: the estate's sharing surface. Consoles that read each other, what
    each may pull, and the map of what actually moved. Bigger than the vault on purpose:
    permissions and admin-credential sync belong on this page as they land."""
    age = age_seconds(state.get("generated_at", ""))
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    doc = page_head("Sync — " + load_instance()["product_name"])
    doc.append(header_html(state, ev, age, "sync", crumb="Sync"))
    doc.append("<main id=main class=wrap>")
    # ---- estate sync: which consoles this vault talks to, and what flows
    _pin = _load_json_list(PEERS_IN_FILE)
    _pout = _load_json_list(PEERS_OUT_FILE)
    _rules = _load_json_list(os.path.join(os.path.dirname(PEERS_CACHE), "sync-rules.json"))
    _pc = {c.get("id"): c for c in _load_json_list(PEERS_CACHE)}
    _e2 = html.escape
    doc.append("<section id=syncpage aria-label='Estate sync'><div class=ah>"
               "<h2 class=title>Servers</h2><span class=meta>"
               "Which boxes keep each other up to date, and exactly what moves between "
               "them. Add a box here once; after that the sharing follows the rules you "
               "set.</span></div>")
    _nconsoles = 1 + len(_pout)
    doc.append("<div class='banner'><b>How estate sync works.</b><span> Sync happens "
               "<b>between Vantage consoles</b> - not between plain TAK servers. A box "
               "can be a sync peer only if it runs a Vantage console you can reach. So "
               "a public cloud TAK server with no console cannot be a peer, and a box "
               "that is powered off or off the network will not appear. To sync a "
               "folder from one box to another, both need a console; you add the far "
               "console here (its address and a token it mints), then pull the folders "
               "you want. This is a pull model: <b>this</b> console reaches out and "
               "pulls; nothing is pushed at you.</span></div>")
    if _nconsoles == 1 and not _pin:
        doc.append("<div class='banner drift'><b>Only this console is set up for sync.</b>"
                   "<span> That is expected on an estate with one console. It is NOT a "
                   "version problem. To sync with another box: install a Vantage console "
                   "on it, make sure it is powered on and reachable from here, then add "
                   "it below. If a box is a plain TAK server (no console) it cannot take "
                   "part - manage its files through the console that built it, or over "
                   "the box directly.</span></div>")
    doc.append("<div class=sync-cols><div>")
    doc.append("<h3 class=sync-h>Consoles this one reads</h3>")
    if _pout:
        for p_ in _pout:
            seen = _pc.get(p_.get("id"), {})
            snap = (seen.get("snapshot") or {})
            folders = snap.get("vault_folders", [])
            opts = "".join(f"<option>{_e2(f)}</option>" for f in folders) or                    "<option value=''>pull the estate first</option>"
            doc.append(
                f"<div class=sync-peer data-id='{_e2(p_.get('id', ''))}'>"
                f"<div class=sync-peer-h><b>{_e2(p_.get('name', ''))}</b>"
                f"<span class=meta>{_e2(p_.get('url', ''))}</span></div>"
                f"<div class=sync-peer-acts>"
                f"<button type=button class='sp-pull cred-refresh'>Refresh estate</button>"
                f"<select class=sp-folder>{opts}</select>"
                f"<button type=button class='sp-vpull cred-refresh'>Pull folder</button>"
                f"</div><div class='a-res sp-res' role=status></div></div>")
    else:
        doc.append("<p class=doct>None yet. Ask the other console's operator for a peer "
                   "token (they mint it below on their side), then add it here.</p>")
    doc.append("<form class=sync-add>"
               "<label class=fl>Console"
               "<select class=pa-node></select>"
               "<span class=hint>found by probing the nodes this estate already knows - "
               "enrolled boxes and peers' servers</span></label>"
               "<div class=fedpop-act style='margin:0 0 8px'>"
               "<button type=button class='pa-probe cred-refresh'>Probe again</button></div>"
               "<label class='fl pa-urlwrap' hidden>URL"
               "<input class=pa-url placeholder='http://host:8090'></label>"
               "<label class=fl>Name<input class=pa-name maxlength=40 "
               "placeholder='what you call that console'></label>"
               "<label class=fl>Their token<input class=pa-token autocomplete=off>"
               "<span class=hint>minted on that console's Sync page - the handshake "
               "stays deliberate</span></label>"
               "<div class=fedpop-act><button type=submit class='a-go confirm'>"
               "Add console</button></div><div class='a-res pa-res' role=status></div></form>")
    doc.append("</div><div>")
    doc.append("<h3 class=sync-h>Consoles allowed to read this one</h3>")
    if _pin:
        for p_ in _pin:
            doc.append(f"<div class=sync-peer><div class=sync-peer-h>"
                       f"<b>{_e2(p_.get('name', ''))}</b>"
                       f"<span class=meta>{p_.get('pulls', 0)} reads"
                       + (f" · last {_e2(str(p_.get('last_seen', ''))[:16])}"
                          if p_.get("last_seen") else " · never used")
                       + "</span></div></div>")
    doc.append("<form class=sync-mint>"
               "<label class=fl>Grant a console read access"
               "<input class=pm-name maxlength=40 placeholder='name the console'></label>"
               "<div class=fedpop-act><button type=submit class=cred-refresh>"
               "Mint peer token</button></div><div class=pm-out></div></form>")
    doc.append("</div></div>")
    # the sync map: this console, its peers, its devices - and what flows
    doc.append("<h3 class=sync-h>Sync map</h3>"
               "<svg class=sync-map viewBox='0 0 900 300' role=img "
               "aria-label='What syncs between consoles and devices'></svg>")
    doc.append("<script type=application/json id=sync-data>" + json.dumps({
        "me": load_instance().get("product_name", "Vantage"),
        "mode": load_instance().get("console_mode", "admin"),
        "peers": [{"id": p_.get("id"), "name": p_.get("name"),
                   "servers": len(((_pc.get(p_.get("id")) or {}).get("snapshot") or {})
                                  .get("servers", []))} for p_ in _pout],
        "granted": [p_.get("name") for p_ in _pin],
        "rules": _rules,
        "local_servers": len(state.get("targets", []))}) + "</script>")
    doc.append("</section>")
    doc.append("<section id=vd-devices aria-label='Synchronised devices'><div class=ah>"
               "<h2 class=title>Devices</h2><span class=meta>"
               "The phones and tablets synchronised to this box. Enrol one with a QR; "
               "confirm it against the six-digit code the holder reads out; revoke it "
               "when the deployment ends.</span></div>"
               "<div class=vd-body>Loading...</div></section>")
    doc.append("<section id=vd-moves aria-label='What moves to devices'><div class=ah>"
               "<h2 class=title>What moves</h2><span class=meta>"
               "What each device receives on its next sync: its deployment's Knowledge "
               "Vault folder, plus every pack shared to that deployment from the File "
               "store. Decide sharing where those things live; this section only shows "
               "the consequence.</span></div>"
               "<div class=vd-body>Loading...</div></section>")
    doc.append("<style>.vd-code{font-family:monospace;font-size:40px;font-weight:700;"
               "margin:4px 0}.vd-in{display:block;margin:6px 0;padding:8px;min-width:260px}"
               ".vd-card{margin:10px 0;padding:10px;border:1px solid #D2C78D;"
               "border-radius:8px}.vd-err{color:#B23A48;font-weight:600}"
               ".vd-rev{color:#B23A48}.vd-payload{white-space:pre-wrap;word-break:break-all;"
               "font-size:11px}.cred-qr{width:220px;height:220px;image-rendering:pixelated;"
               "background:#fff;padding:8px;border-radius:8px}</style>")
    doc.append(f"<script>{DEVICES_SYNC_JS}</script>")
    doc.append(f"<script>{SYNC_JS}</script>")
    doc.append(footer_html(state, acts))
    return "".join(doc)


def deployed_api(path, data, client):
    """The POST half of the Deployed page: every route proxies to the linked box's admin
    surface; pack-assign reads the chosen File store file and pushes its bytes across."""
    route = path.split("/api/deployed/", 1)[1]
    if route == "link":
        url = str(data.get("url") or "").rstrip("/")
        tok = str(data.get("token") or "")
        if not url.startswith(("http://", "https://")) or not tok:
            return 400, {"error": "give the box URL and its admin token"}
        import urllib.request
        req = urllib.request.Request(url + "/admin/overview", headers={"X-VD-Admin": tok})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                if r.status != 200:
                    return 502, {"error": f"the box refused ({r.status})"}
        except Exception as e2:
            return 502, {"error": f"could not reach the box: {e2.__class__.__name__}"}
        tmp = DEPLOYED_LINK_FILE + ".tmp"
        os.makedirs(os.path.dirname(DEPLOYED_LINK_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"url": url, "token": tok}, f)
        os.replace(tmp, DEPLOYED_LINK_FILE)
        try:
            os.chmod(DEPLOYED_LINK_FILE, 0o600)
        except OSError:
            pass
        audit({"action": "deployed-link", "client": client, "url": url})
        return 200, {"linked": url}
    if route == "pack-assign":
        rel = str(data.get("store_path") or "")
        full = store_resolve(rel, "store")
        if not full or not os.path.isfile(full):
            return 404, {"error": "no such file on the store shelf"}
        with open(full, "rb") as f:
            blob = f.read()
        from urllib.parse import quote
        st, body = deployed_call(
            "POST",
            "/admin/pack?deployment=" + quote(str(data.get("deployment") or ""))
            + "&kind=" + quote(str(data.get("kind") or ""))
            + "&name=" + quote(os.path.basename(rel)), body=blob, timeout=30)
        audit({"action": "deployed-pack-assign", "client": client, "file": rel,
               "deployment": data.get("deployment")})
        return (st or 502), json.loads(body or b"{}")
    proxy = {"mint": "/admin/mint", "confirm": "/admin/confirm",
             "reject": "/admin/reject", "revoke": "/admin/revoke",
             "pack-remove": "/admin/pack-remove"}.get(route)
    if not proxy:
        return 404, {"error": "not found"}
    st, body = deployed_call("POST", proxy, body=json.dumps(data).encode())
    if route in ("mint", "confirm", "revoke"):
        audit({"action": "deployed-" + route, "client": client,
               "detail": {k: data.get(k)
                          for k in ("device", "holder", "deployment", "fingerprint")
                          if data.get(k)}})
    try:
        return (st or 502), json.loads(body or b"{}")
    except ValueError:
        return 502, {"error": "bad reply from the box"}


def render_vault(state):
    """The vault viewer: tree on the left, rendered note (or editor) on the right, in the image
    of the standalone Vault Viewer. Management ops ride the same vault APIs."""
    age = age_seconds(state.get("generated_at", ""))
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    doc = page_head("Knowledge Vault — " + load_instance()["product_name"])
    doc.append(header_html(state, ev, age, "vault", crumb="Knowledge Vault"))
    doc.append("<main id=main class='wrap vv-wrap'>")
    # is the vault empty? then the templates lead; otherwise they fold away
    _vroot = store_resolve("", "vault")
    _empty = True
    if _vroot and os.path.isdir(_vroot):
        for _dp, _dn, _fn in os.walk(_vroot):
            _dn[:] = [d for d in _dn if not d.startswith(".")]
            if any(not f.startswith(".") for f in _fn):
                _empty = False
                break
    _e = html.escape
    _cards = "".join(
        f"<div class=onto data-k='{_e(k)}'><div class=onto-h><b>{_e(o['name'])}</b></div>"
        f"<div class=onto-d>{_e(o['desc'])}</div>"
        f"<button type=button class='onto-go cred-refresh'>Start from this</button></div>"
        for k, o in VAULT_ONTOLOGIES.items())
    _example = ('{\n  "01 Orders": {\n    "Overview.md": "## Situation\\n"\n  },\n'
                '  "02 Locations": {}\n}')
    doc.append("<section id=vaultpage aria-label='Knowledge vault'>"
               "<details class=onto-wrap" + (" open" if _empty else "") + ">"
               "<summary class=onto-sum>Start from a template <span class=meta>- scaffold "
               "a folder structure with seed notes, ready to fill</span></summary>"
               + ("<p class=doct>An empty vault is a blank page problem. Pick the shape "
                  "that matches your work - folders and starter notes appear in the tree, "
                  "and nothing you later write is ever overwritten.</p>" if _empty else "")
               + "<div class=ontos>" + _cards + "</div>"
               "<details class=onto-custom><summary>Or bring your own structure</summary>"
               "<p class=meta>Paste folders and notes as JSON: folders are objects, notes "
               "are strings of their starting text.</p>"
               "<textarea class=onto-json spellcheck=false placeholder='" + _e(_example)
               + "'></textarea>"
               "<div class=fedpop-act><button type=button class='onto-build cred-refresh'>"
               "Build this</button></div></details>"
               "<div class=onto-res></div></details>")
    doc.append("<details class=kv-graph><summary class=onto-sum>Graph view "
               "<span class='meta kv-scope'>scope: whole vault</span>"
               "<span class=meta> - click a folder in the tree or the graph to drill in; "
               "click the centre node to go up</span></summary>"
               "<div class=fedpop-act style='margin:6px 0 0'>"
               "<button type=button class='kv-graph-all cred-refresh'>Whole vault</button></div>"
               "<canvas class=kv-canvas height=440></canvas>"
               "<div class=meta style='padding:0 2px 10px'>Click a note to open it. "
               "Link notes to each other by writing [[Note name]] inside them.</div>"
               "</details>")
    doc.append("<div class=vv-bar>"
               "<button type=button class='vv-new cred-refresh'>New note</button>"
               "<button type=button class='vv-newf cred-del'>New folder</button>"
               "<label class='cred-dl vv-uplab'>Upload"
               "<input type=file class=vv-up hidden multiple></label>"
               "<span class=vv-locwrap>into <b class=vv-loc>(top level)</b></span>"
               "<span class=spacer></span>"
               "<button type=button class='vv-edit st-edit' disabled>Edit</button>"
               "<button type=button class='vv-ren cred-del' disabled>Rename</button>"
               "<button type=button class='vv-mv cred-del' disabled>Move</button>"
               "<button type=button class='vv-dl cred-del' disabled>Download</button>"
               "<button type=button class='vv-mp cred-refresh' disabled>Make mission pack</button>"
               "<button type=button class='vv-del cred-del' disabled>Delete</button>"
               "</div>"
               "<div class=vv-renwrap hidden><label class=fl>New name "
               "<input class=vv-reninp maxlength=100></label>"
               "<button type=button class='vv-renok cred-refresh'>Rename</button></div>"
               "<div class='a-res vv-res' role=status></div>"
               "<div class=vv-panes>"
               "<nav class='vv-tree tree' aria-label='Vault tree'></nav>"
               "<div class=vv-main><div class=vv-welcome>Select a note from the tree, or start "
               "from a template above. Notes here are the estate's knowledge - a connected "
               "agent reads them to answer questions about your operation.</div></div>"
               "</div></section>")
    doc.append(f"<script>{VAULT_VIEW_JS}</script>")
    doc.append(footer_html(state, acts))
    return "".join(doc)


STORE_SHARE_JS = r"""
(function(){
  var root=document.querySelector('#store-share .vd-body'); if(!root)return;
  function el(t,c,txt){var n=document.createElement(t); if(c)n.className=c; if(txt!=null)n.textContent=txt; return n;}
  function post(p,obj,cb){fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj||{})}).then(function(r){return r.json().then(function(j){cb(r.status,j);});}).catch(function(){cb(0,{error:'unreachable'});});}
  function load(){fetch('/api/deployed/overview').then(function(r){return r.json().then(function(j){render(r.status,j);});}).catch(function(){render(0,{});});}
  function render(st,j){
    root.textContent='';
    if(st!==200){root.appendChild(el('p','meta','Link the box on the Sync page (Devices section) to share packs to deployments.'));return;}
    var o=j.overview||{};
    var man=o.manifest||{};
    var any=false;
    Object.keys(man).sort().forEach(function(dep){
      (man[dep]||[]).forEach(function(pp){
        any=true;
        var row=el('div','vd-card');
        row.appendChild(el('b',null,pp.name));
        row.appendChild(el('span','meta','  '+pp.kind+' pack, '+Math.round(pp.size/1024)+' KB, shared to '+dep));
        var rm=el('button','cred-del','Stop sharing'); rm.type='button'; rm.style.marginLeft='10px';
        rm.onclick=function(){post('/api/deployed/pack-remove',{deployment:dep,name:pp.name},function(){load();});};
        row.appendChild(rm); root.appendChild(row);});});
    if(!any){root.appendChild(el('p','meta','Nothing shared yet.'));}
    var det=el('details'); det.appendChild(el('summary','onto-sum','Share a pack to a deployment'));
    var fs=el('select','vd-in'); (j.store_files||[]).forEach(function(sf){var op=el('option'); op.value=sf; op.textContent=sf; fs.appendChild(op);});
    var ks=el('select','vd-in'); ['mission','map'].forEach(function(k){var op=el('option'); op.value=k; op.textContent=k+' pack'; ks.appendChild(op);});
    var dsi=el('input','vd-in'); dsi.placeholder='Deployment'; dsi.setAttribute('list','ss-deps');
    var dl=el('datalist'); dl.id='ss-deps';
    (o.deployments||[]).forEach(function(d){var op=el('option'); op.value=d; dl.appendChild(op);});
    var ab=el('button','cred-refresh','Share'); ab.type='button';
    ab.onclick=function(){post('/api/deployed/pack-assign',{deployment:dsi.value.trim(),kind:ks.value,store_path:fs.value},
      function(st2,j2){if(st2===200){load();}else{alert((j2&&j2.error)||('refused '+st2));}});};
    det.appendChild(fs); det.appendChild(ks); det.appendChild(dsi); det.appendChild(dl); det.appendChild(ab);
    root.appendChild(det);
  }
  load();
})();
"""


def render_store(state, area="store"):
    """The file manager, over one of two roots. "store": the filestore for everything deployed
    when building and running a TAK server. "vault": the knowledge vault - full add, edit,
    move, download and delete for pre-deployment prep and close-down, with every change carried
    outward by the vault's own sync exactly as if authored on this box by hand."""
    age = age_seconds(state.get("generated_at", ""))
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    vault = area == "vault"
    title = "Knowledge Vault" if vault else "File store"
    doc = page_head(f"{title} — " + load_instance()["product_name"])
    doc.append(header_html(state, ev, age, area, crumb=title))
    doc.append("<main id=main class=wrap>")
    if vault:
        doc.append("<section id=storepage data-area=vault aria-label='Knowledge vault'>"
                   "<div class=ah><h2 class=title>Knowledge vault</h2><span class=meta>The "
                   "Deployed vault on this box, managed here for pre-deployment prep and "
                   "close-down: add, edit, move, download and delete. Every change syncs "
                   "onward to the forward box and the end-user device by the vault's own "
                   "sync, exactly as if you had authored it here by hand. In the field, use "
                   "the Deployed app.</span></div>")
        cards = "".join(
            f"<div class=onto data-k='{e(k)}'><div class=onto-h><b>{e(o['name'])}</b></div>"
            f"<div class=onto-d>{e(o['desc'])}</div>"
            f"<button type=button class='onto-go cred-refresh'>Start from this</button></div>"
            for k, o in VAULT_ONTOLOGIES.items())
        example = ('{\n  "01 Orders": {\n    "Overview.md": "## Situation\\n"\n  },\n'
                   '  "02 Locations": {}\n}')
        doc.append("<details class=onto-wrap><summary class=onto-sum>"
                   "Start from a template <span class=meta>- scaffold a folder structure, "
                   "ready to fill</span></summary>"
                   "<div class=ontos>" + cards + "</div>"
                   "<details class=onto-custom><summary>Or bring your own ontology</summary>"
                   "<p class=meta>Paste a structure as JSON: folders are objects, notes are "
                   "strings of their starting text. Nested folders are objects inside objects. "
                   "Existing files are never overwritten.</p>"
                   "<textarea class=onto-json spellcheck=false placeholder='" + e(example)
                   + "'></textarea>"
                   "<div class=fedpop-act><button type=button class='onto-build cred-refresh'>"
                   "Build this</button></div></details>"
                   "<div class=onto-res></div></details>")
    else:
        doc.append("<section id=storepage data-area=store aria-label='Filestore'><div class=ah>"
                   "<h2 class=title>File store</h2><span class=meta>The console box's filestore for "
                   "everything deployed when building and running a TAK server: install packages, "
                   "mission packs, map packs, software for devices. Browse, upload, download, "
                   "move and delete. Packages in <b>tak-server</b> are what the Deploy wizard "
                   "installs.</span></div>")
        # Disconnected ingest: bring software in from a USB stick (the mirror of the USB
        # export). The console does the fiddly Linux part; files land where they belong.
        doc.append(
            "<details class=usb-import><summary>Import software from a USB stick</summary>"
            "<div class=ui-body><p class=meta>For a <b>disconnected</b> admin box: insert a USB "
            "stick and import software into the estate. TAK Server packages (<code>.deb</code>) "
            "and CloudTAK/MediaMTX image tarballs (<code>.tar</code>) land in the library; device "
            "apps (<code>.apk</code>, <code>.zip</code>) land on the device shelf.</p>"
            "<div class=ui-actions><button type=button class=ui-scan>Scan USB stick</button>"
            "<span class=ui-msg role=status></span></div><div class=ui-list></div></div></details>")
        # Device apps: mark which shelf build is current. /eud badges + lists it first.
        apps = software_apps()
        if apps:
            ca_rows = "".join(
                f"<div class=ca-row><span class=ca-name>{html.escape(a['name'])}</span>"
                f"<span class=ca-sz>{_human_bytes(a['bytes'])}</span>"
                f"<button type=button class='ca-toggle{' on' if a['current'] else ''}' "
                f"data-file=\"{html.escape(a['name'])}\" data-on='{1 if a['current'] else 0}'>"
                + ("Current ✓" if a['current'] else "Make current") + "</button></div>"
                for a in apps)
        else:
            ca_rows = ("<div class=meta>No apps on the software shelf yet. Upload one, or import "
                       "from USB.</div>")
        doc.append(
            "<details class=cur-apps><summary>Device apps - mark the current version</summary>"
            "<div class=ca-body><p class=meta>Mark which build on the software shelf is the "
            "current one for devices. It shows badged <b>current</b> and listed first at "
            "<a href='/eud'>/eud</a> - the new-release journey: upload the <code>.apk</code>, "
            "mark it current, devices pull it over the closed network.</p>"
            f"<div class=ca-list>{ca_rows}</div></div></details>")
        doc.append("<div class=eud-strip><div><b>Device downloads</b><span class=meta> "
                   "phones and EUDs on the estate network browse <a href='/eud' "
                   "id=eudlink>/eud</a> to pull software, mission packs and map packs - "
                   "including the Deployed app itself. Read-only; scan the code to open it "
                   "on a device.</span></div>"
                   "<img class=eud-qr src=/eud/qr.png alt='QR code for the device downloads "
                   "page' onerror=\"this.replaceWith('QR needs qrencode on the console box')\">"
                   "</div>")
    doc.append("<nav class=st-crumb aria-label=Breadcrumb></nav>")
    doc.append("<div class=st-folders></div>")
    doc.append("<div class=st-drop><input type=file class=st-pick hidden id=stpick multiple>"
               "<div class=lib-drop-in><b>Drag files here</b>, or "
               "<label for=stpick class=lib-link>choose files</label>, to add them to this folder. "
               + ("" if vault else
                  "<label class=lib-archwrap>Package arch "
                  "<select class=st-arch><option value=amd64>amd64</option>"
                  "<option value=arm64>arm64 (Raspberry Pi)</option></select></label> ")
               + "<button type=button class='st-newfolder cred-del'>New folder</button>"
               + ("<button type=button class='st-newnote cred-del'>New note</button>" if vault else "")
               + "</div><progress class=st-bar max=100 hidden></progress></div>")
    doc.append("<div class=st-toolbar hidden><span class=st-selname></span>"
               "<span class=spacer></span>"
               + ("<button type=button class='st-tb-edit cred-refresh'>Edit</button>" if vault else "")
               + "<a class='st-tb-dl cred-refresh' download>Download</a>"
               "<select class=st-tb-move><option value=''>Move to\u2026</option></select>"
               "<span style='width:18px'></span>"
               "<button type=button class=st-tb-del>Delete</button></div>")
    doc.append("<table class='lib-table st-finder'><thead><tr>"
               "<th class=st-h data-k=name>Name</th>"
               "<th class=st-h data-k=kind>Kind</th>"
               "<th class='st-h lib-num' data-k=size>Size</th>"
               "<th class=st-h data-k=mtime>Modified</th></tr></thead>"
               "<tbody class=st-tbody><tr><td colspan=4 class=cred-empty>Loading&hellip;</td></tr>"
               "</tbody></table>")
    if vault:
        doc.append("<div class=st-editor hidden><div class=st-ed-head>"
                   "<span class='st-ed-path lib-num'></span>"
                   "<span class=spacer></span>"
                   "<button type=button class='st-ed-save cred-refresh'>Save</button>"
                   "<button type=button class='st-ed-cancel cred-del'>Close</button></div>"
                   "<textarea class=st-ed-text spellcheck=false></textarea></div>")
    if not vault:
        doc.append("<div class=st-fetch-wrap style='display:none'>"
                   "<div class=lib-fetchrow><b>Fetch a package by URL</b> "
                   "<span class=meta>the console downloads it (far faster than your uplink); a "
                   "release behind a login is downloaded by hand and dragged in.</span>"
               "<div class=wz-acc-row style='margin-top:8px'>"
               "<input class='st-url cred-passinp' style='flex:1;min-width:280px' "
               "placeholder='https://\u2026/takserver_5.8-RELEASEnn_all.deb'>"
                   "<button type=button class='st-fetch cred-refresh'>Fetch into tak-server</button>"
                   "</div></div></div>")
    doc.append("<div class='a-res st-res' role=status></div>")
    doc.append("<pre class='deplog st-log'></pre>")
    doc.append("</section>")
    if not vault:
        doc.append("<section id=store-share aria-label='Shared to deployments'><div class=ah>"
                   "<h2 class=title>Shared to deployments</h2><span class=meta>"
                   "Sharing a pack here is the standing decision: every device in that "
                   "deployment receives it on its next sync, and keeps receiving updates "
                   "to it. One-time downloads for device bootstrap stay on the /eud page."
                   "</span></div>"
                   "<style>.vd-card{margin:8px 0;padding:8px;border:1px solid #D2C78D;"
                   "border-radius:8px}.vd-in{display:inline-block;margin:6px 8px 6px 0;"
                   "padding:8px;min-width:200px}</style>"
                   "<div class=vd-body>Loading...</div></section>")
    doc.append(f"<script>{STORE_JS}</script>")
    if not vault:
        doc.append(f"<script>{STORE_SHARE_JS}</script>")
        doc.append(f"<script>{USB_IMPORT_JS}</script>")
    doc.append(footer_html(state, acts))
    return "".join(doc)


def render_deploy(state):
    """Deploy a new TAK server from the console. The form drives vantage-tak-provision.sh on the
    chosen box through the gated provision action: the script is pushed per-run and verified
    twice, every parameter is validated on both sides, and the provisioner refuses to touch an
    existing install. Dry run first is the doctrine, and the default."""
    e = html.escape
    _di = load_instance()
    age = age_seconds(state.get("generated_at", ""))
    ev = state.get("estate_result", "UNKNOWN")
    cfg = load_actions_config()
    acts = enabled_actions(cfg)
    a = ACTIONS["provision-server"]
    doc = page_head("Deploy — " + load_instance()["product_name"])
    doc.append(header_html(state, ev, age, "deploy", crumb="Deploy"))
    doc.append("<main id=main class=wrap>")
    doc.append("<section aria-label='Deploy a TAK server'><div class=ah>"
               "<h2 class=title>Deploy a TAK server</h2><span class=meta>Stands up a complete "
               "MilUX TAK Server on a box the console manages: harden, dependencies, install, "
               "database, PKI, CoreConfig, Let's Encrypt, start. The box must be enrolled to "
               "the console (<code>console-enrol-server.sh</code>) and already hold the TAK "
               "Server <code>.deb</code>. <b>Run the dry run first</b> - it prints every step "
               "and changes nothing. A live TAK server on the box is refused, never "
               "overwritten.</span></div>")
    if "provision-server" not in acts:
        doc.append("<div class=meta style='margin-top:8px'>This console cannot build "
                   "other boxes yet: the provision action is not switched on. On a standard "
                   "install it is on by default; if you removed it, re-run the installer.</div>")
        doc.append(footer_html(state, acts))
        return "".join(doc)

    # the library lives on its own page now; the wizard's step 2 picks from it inline
    doc.append("<p class=doct>Packages come from the <a href='/store'>Store</a> - upload or "
               "fetch releases there. The wizard below picks from what is on the shelf.</p>")

    # top-of-page flow launchers: pick a flow rather than scroll one long page
    dflows = [("new", "Build a new server"), ("provision", "Provision an enrolled box")]
    if "provision-infratak" in acts:
        dflows.append(("infratak", "Deploy infra-TAK"))
    doc.append("<div class=dflow-tabs role=tablist>"
               + "".join(f"<button type=button class=dtab data-flow='{fid}'>{e(lbl)}</button>"
                         for fid, lbl in dflows) + "</div>")
    doc.append("<div class=dflow id=flow-new>")

    # ---- the setup wizard: a NEW box, end to end ----
    doc.append("<h2 class=sec-eye>Set up a new box</h2>"
               "<p class=doct>Everything from the browser: prove access, choose or upload the "
               "TAK package, describe the server, name the first users, run. The bootstrap key "
               "is used for this setup only and destroyed at the end, whatever happens.</p>")
    doc.append("<div id=wz-deps class=wz-deps hidden></div>")
    doc.append("<div id=wizard>")
    doc.append("<fieldset class='wz-step depcard'><legend>1 · The box, and how to reach it</legend>"
               "<div class=wz-selfrow><button type=button id=wz-selfbox class='a-go confirm'>"
               "Deploy on this box</button><span class=hint>the console's own server "
               "becomes the first TAK server - no keys to paste, no addresses. Everything "
               "below fills itself; carry on at step 2.</span></div>"
               "<label class=fl>Estate name<input id=wz-name maxlength=24 placeholder='dev-cloud'>"
               "<span class=hint>short, [a-z0-9-] - how the console will know the box</span></label>"
               "<label class=fl>Label<input id=wz-label maxlength=40 placeholder='Dev TAK'>"
               "<span class=hint>the human name shown on the console</span></label>"
               "<label class=fl>Address<input id=wz-addr placeholder='203.0.113.7 or host.example'>"
               "<span class=hint>IP or hostname the console can reach over SSH</span></label>"
               "<label class=fl>Admin user<input id=wz-user value='root'>"
               "<span class=hint>root, or a passwordless-sudo user</span></label>"
               "<label class=fl>Health profile<select id=wz-profile>"
               "<option value=cloud>Public cloud server - internet-facing, trusted "
               "certificate, ports open</option>"
               "<option value=firmbase>Private network server - VPN or tailnet only, "
               "not internet-facing</option>"
               "<option value=deployed>Deployable kit - field hardware running the "
               "full stack</option></select>"
               "<span class=hint>the ROLE this box plays; sets what healthy means "
               "on the health checks</span></label>"
               "<div class=wz-access><b>Bootstrap access</b> - used once, then destroyed:"
               "<div class=wz-acc-row><button type=button id=wz-genkey class=cred-refresh>"
               "Generate a key</button><span class=meta>the console mints a keypair and shows "
               "you the public half to place on the box</span></div>"
               "<div id=wz-pubwrap hidden><code class=wz-pub id=wz-pub></code>"
               "<div class=meta id=wz-pubhint></div>"
               "<code class=wz-pub id=wz-pubcmd></code>"
               "<div class=wz-acc-row><button type=button id=wz-copycmd class=cred-dl>"
               "Copy the command</button><span class=meta>run it on the box - its own "
               "keyboard, or any session you already have. It is the only terminal step "
               "in this journey; press Test when it is done.</span></div></div>"
               "<div class=wz-acc-row><label class=cred-dl>Upload a private key"
               "<input type=file id=wz-upkey hidden></label>"
               "<button type=button id=wz-pastekey class=cred-dl>Paste a private key</button>"
               "<span class=meta>provide one that already reaches the box - a file, or pasted "
               "from a password manager; either way it is adopted into a root-only store and "
               "the copy you sent is shredded</span></div>"
               "<div id=wz-pastewrap hidden><textarea id=wz-keytext class=wz-keytext "
               "spellcheck=false autocomplete=off "
               "placeholder='-----BEGIN OPENSSH PRIVATE KEY-----'></textarea>"
               "<div class=wz-acc-row><button type=button id=wz-adoptkey class=cred-refresh>"
               "Adopt the pasted key</button><span class=meta>passphrase-protected keys are "
               "refused; the box needs the plain key</span></div></div>"
               "<div id=wz-keyres class=a-res role=status></div></div>"
               "<div class=fedpop-act><button type=button id=wz-test class='a-go confirm'>"
               "Test the connection</button></div>"
               "<div id=wz-testres class=a-res role=status></div></fieldset>")
    doc.append("<fieldset class='wz-step depcard locked'><legend>2 · The TAK package</legend>"
               "<div class=wz-lib id=wz-lib><div class=cred-empty>Prove access first.</div></div>"
               "<div class=wz-acc-row><label class=cred-dl>Upload a .deb to the library"
               "<input type=file id=wz-updeb accept=.deb hidden></label>"
               "<select id=wz-arch><option value=amd64>amd64</option>"
               "<option value=arm64>arm64 (e.g. Raspberry Pi)</option></select>"
               "<progress id=wz-upbar max=100 hidden></progress></div>"
               "<div id=wz-upres class=a-res role=status></div>"
               "<label class=depdry><input type=checkbox id=wz-onbox> The .deb is already on "
               "the box - skip the push and use the path in step 3</label></fieldset>")
    doc.append("<fieldset class='wz-step depcard locked'><legend>3 · The server</legend>"
               "<label class=fl>Server FQDN<input id=wz-fqdn placeholder='tak.example.org' required>"
               "<span class=hint>must already resolve to the box for Let's Encrypt</span></label>"
               "<label class=fl>Let's Encrypt email<input id=wz-email placeholder='ops@example.org' required>"
               "<span class=hint>expiry notices go here</span></label>"
               f"<label class=fl>Organisation<input id=wz-org value='{e(_di['org'])}' placeholder='e.g. Acme Defence' required></label>"
               f"<label class=fl>Org unit<input id=wz-orgunit value='{e(_di['org_unit'])}' placeholder='e.g. Operations' required></label>"
               f"<label class=fl>Country<input id=wz-country value='{e(_di['country'])}' maxlength=2 placeholder='e.g. GB' required></label>"
               f"<label class=fl>State/county<input id=wz-state value='{e(_di['state'])}' placeholder='e.g. Hampshire' required></label>"
               f"<label class=fl>City<input id=wz-city value='{e(_di['city'])}' placeholder='e.g. Andover' required></label>"
               "<label class=fl>Certificate password (optional)<input id=wz-capass "
               "type=password maxlength=64 autocomplete=off "
               "placeholder='blank = strong generated one'>"
               "<span class=hint>typed into devices when importing certificates; simple "
               "is fine for an experimental build</span></label>"
               "<label class=fl>.deb path on the box<input id=wz-deb placeholder='/root/takserver.deb'>"
               "<span class=hint>filled from the library selection; edit only for an "
               "already-on-box package</span></label>"
               "<div class=fl><span class=wz-comp-t>Extra components</span>"
               "<div class=wz-comps>"
               "<label class=wz-comp><input type=checkbox class=wzcomp value=cloudtak>"
               "<span><b>CloudTAK</b><small>web map + missions stack (Docker, pinned "
               "v13.70.0); web port stays loopback until exposed deliberately</small></span></label>"
               "<label class=wz-comp><input type=checkbox class=wzcomp value=mediamtx>"
               "<span><b>MediaMTX</b><small>video relay for drone and camera feeds (pinned "
               "v1.20.0); firewall untouched until a producer needs in</small></span></label>"
               "</div><input type=hidden id=wz-components value=''></div></fieldset>")
    doc.append("<fieldset class='wz-step depcard locked'><legend>4 · First users (optional)</legend>"
               "<p class=meta>Each row mints an enrolment credential once the server is up: a QR "
               "for ATAK and an iTAK line, downloadable at the end and re-downloadable later "
               "from the box's Credentials panel.</p>"
               "<div id=wz-creds></div>"
               "<div class=wz-acc-row><button type=button id=wz-addcred class=cred-dl>+ Add a "
               "user</button><label class=cred-pass id=wz-passwrap hidden>"
               "<span>Operator password <b class=req>required</b></span>"
               "<input type=password id=wz-pass autocomplete=off "
               "placeholder='required to mint users'></label></div></fieldset>")
    doc.append("<fieldset class='wz-step depcard locked'><legend>5 · Run</legend>"
               "<label class=depdry><input type=checkbox id=wz-console checked> "
               "<b>Install a console on this box</b> - a self-manage Vantage console (the "
               "same version this one runs), so whoever logs into the box can manage it "
               "locally: certificates, software, its own health. Recommended for every "
               "box.</label>"
               "<label class=depdry><input type=checkbox id=wz-dry> <b>Dry run</b> - "
               "prints every step, changes nothing on the box beyond enrolment. Tick it to "
               "preview a build before running one for real.</label>"
               "<label class=depdry><input type=checkbox id=wz-enrolonly> <b>Enrol only</b> - "
               "the server already runs (yours, or a build you inherited). Adds it to the "
               "estate for monitoring and gated management; installs no software. Steps 2 "
               "and 3 are ignored beyond the FQDN.</label>"
               "<div class=fedpop-note>Enrols the box to the estate, pushes the package, "
               "provisions TAK Server, mints the first users, then destroys the bootstrap "
               "key.</div>"
               "<div class=fedpop-act><button type=button id=wz-go class='a-go confirm'>"
               "Set up the server</button></div>"
               "<div id=wz-runres class=a-res role=status></div>"
               "<pre id=wz-log class=deplog aria-label='Setup log'></pre>"
               "<div id=wz-credout></div></fieldset>")
    doc.append("</div>")
    doc.append(f"<script>{SETUP_JS}</script>")
    doc.append("</div>")                              # close flow-new
    doc.append("<div class=dflow id=flow-provision>")
    doc.append("<h2 class=sec-eye>Provision an already-enrolled box</h2>")
    doc.append("<form class=depcard id=depform>")
    doc.append("<label class=fl>Target box<select id=deptarget>")
    # actions.json targets map name -> ssh destination (a string); the friendly label lives on
    # the state's target record.
    labels = {t.get("name"): t.get("label") or t.get("name")
              for t in state.get("targets", [])}
    for name in sorted(cfg.get("targets") or {}):
        doc.append(f"<option value='{e(name)}'>{e(labels.get(name) or name)}</option>")
    doc.append("</select><span class=hint>An enrolled box. The provisioner refuses a box "
               "already running TAK Server.</span></label>")
    for f in a["inputs"]:
        if f["name"] == "dry_run" or f.get("hidden"):
            continue
        d = DEPLOY_DEFAULTS.get(f["name"], "")
        req = "" if f["name"] == "components" else " required"
        doc.append(f"<label class=fl>{e(f['label'])}<input data-k='{e(f['name'])}' "
                   f"value='{e(d)}'{req}><span class=hint>{e(f['help'])}</span></label>")
    doc.append("<label class=depdry><input type=checkbox data-k=dry_run checked> "
               "<b>Dry run</b> - print every step, change nothing. Untick only when the dry "
               "run read correctly.</label>")
    doc.append(f"<div class=fedpop-note>{e(a['confirm']).replace('{target}', 'the chosen box').replace('{fqdn}', 'the FQDN above')}</div>")
    doc.append("<div class=fedpop-act><button class='a-go confirm' id=depgo type=submit>"
               "Provision</button></div></form>")
    doc.append("<div id=depstatus class=depstatus role=status></div>")
    doc.append("<pre id=deplog class=deplog aria-label='Provision log'></pre>")
    doc.append("</div>")                              # close flow-provision

    # infra-TAK as a stack choice (card 6175): deploy takwerx's platform on an enrolled box
    # instead of a TAK Server. Vantage stands it up and monitors the box; the stack is run
    # from infra-TAK's own console. Credit + limits stated plainly (Matt).
    if "provision-infratak" in acts:
        doc.append("<div class=dflow id=flow-infratak>")
        doc.append("<h2 class=sec-eye>Deploy infra-TAK instead</h2>"
                   "<p class=doct><b>infra-TAK</b> is a different TAK stack, built by "
                   "<b>takwerx</b> - an open-source project, not MilUX "
                   "(<a href='https://github.com/takwerx/infra-TAK' target=_blank rel=noopener>"
                   "github.com/takwerx/infra-TAK</a>). Its own marketplace deploys the TAK "
                   "ecosystem in Docker, and it brings its own web console on <b>port 5001</b>. "
                   "Vantage stands it up on an enrolled box and keeps health-monitoring that "
                   "box, but the stack itself is run from infra-TAK's own console, not from here "
                   "- <b>Vantage's management of an infra-TAK box is limited</b> to deploying it "
                   "and watching it. Dry run first, as ever.</p>")
        doc.append("<form class=depcard id=itform>")
        it_targets = {t.get("name"): t.get("label") or t.get("name")
                      for t in state.get("targets", [])}
        doc.append("<label class=fl>Target box<select id=ittarget>")
        for tname in sorted(cfg.get("targets") or {}):
            doc.append(f"<option value='{e(tname)}'>{e(it_targets.get(tname) or tname)}</option>")
        doc.append("</select><span class=hint>An enrolled box.</span></label>")
        doc.append("<label class=fl>infra-TAK version<input id=itref value='main'>"
                   "<span class=hint>a tag or branch to pin</span></label>")
        doc.append("<label class=depdry><input type=checkbox id=itdry checked> "
                   "<b>Dry run</b> - print every step, change nothing.</label>")
        doc.append("<div class=fedpop-note>Installs Docker and infra-TAK on the box, and "
                   "serves its console on port 5001. The admin password is generated and "
                   "left root-only on the box.</div>"
                   "<div class=fedpop-act><button class='a-go confirm' id=itgo type=submit>"
                   "Deploy infra-TAK</button></div></form>"
                   "<div id=itstatus class=depstatus role=status></div>"
                   "<pre id=itlog class=deplog aria-label='infra-TAK log'></pre>")
        doc.append(f"<script>{INFRATAK_JS}</script>")
        doc.append("</div>")                          # close flow-infratak

    doc.append("<h2 class=sec-eye>How this stays safe</h2>"
               "<p class=doct>The provisioner is pushed to the box on every run with its "
               "sha256; the box re-hashes it, syntax-checks it, and a root wrapper re-validates "
               "every parameter against its own patterns before running it - the same "
               "two-gate model as every push action. It is staged and re-runnable: a failed "
               "run can be re-run and completed stages skip. It will not reinstall over a "
               "working TAK server. When it finishes it prints ENROL-READY with the facts the "
               "estate needs; enrolment to the console stays a deliberate step.</p>")
    doc.append(f"<script>{DEPLOY_JS}</script>")
    doc.append(f"<script>{DEPLOY_TABS_JS}</script>")
    doc.append(footer_html(state, acts))
    return "".join(doc)


def render(state, err):
    """Kept as the '/' entry point: the estate page, or the error page."""
    if err:
        return render_error(err)
    return render_estate(state)


class Handler(BaseHTTPRequestHandler):
    server_version = "vantage-console"

    def _send(self, code, body, ctype, cache="no-store"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(b)

    def _send_store_file(self, rel, area="store"):
        """Stream any store or vault file back to the browser for download. The path is
        resolved through store_resolve, which guarantees it stays inside that area's root, then
        served chunked so a 600MB package never sits in memory."""
        p = store_resolve(rel, area)
        if p is None or not os.path.isfile(p):
            self._send(404, "no such file\n", "text/plain")
            return
        fname = os.path.basename(p)
        size = os.path.getsize(p)
        ctype = ("text/markdown; charset=utf-8" if fname.lower().endswith(".md")
                 else "application/vnd.debian.binary-package" if fname.lower().endswith(".deb")
                 else "application/vnd.android.package-archive" if fname.lower().endswith(".apk")
                 else "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(p, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_GET(self):
        path = self.path.split("?")[0]
        # The favicon needs no state and is the one thing browsers may cache:
        # it never changes except with the code that serves it. /favicon.ico is
        # answered too, for anything that asks blindly without reading the link tag.
        if path in ("/favicon.svg", "/favicon.ico"):
            self._send(200, favicon_svg(), "image/svg+xml", cache="no-store")
            return
        if path == "/welcome":
            if EDITION == "deployed" or auth_configured():
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._send(200, render_welcome(), "text/html; charset=utf-8")
            return
        if path in ("/api/estate/export", "/api/vault/export"):
            self.do_GET_peer(path)
            return
        if EDITION != "deployed" and not AUTH_OPEN_MODE and not auth_configured() \
                and auth_required(path) and path != "/welcome":
            # a fresh console's FIRST screen is setting the operator password - not an
            # open estate with a polite banner
            if path.startswith("/api/"):
                self._send(401, json.dumps({"error": "set the operator password first"}),
                           "application/json")
            else:
                self.send_response(302)
                self.send_header("Location", "/welcome")
                self.end_headers()
            return
        if path == "/login":
            if not auth_configured() or session_valid(self):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._send(200, render_login(), "text/html; charset=utf-8")
            return
        if path == "/logout":
            cookie = self.headers.get("Cookie", "") or ""
            m = re.search(r"(?:^|;\s*)vsession=([A-Za-z0-9_-]{20,64})", cookie)
            if m:
                with _AUTH_LOCK:
                    _SESSIONS.pop(m.group(1), None)
            self.send_response(302)
            self.send_header("Set-Cookie", "vsession=; Path=/; HttpOnly; Max-Age=0")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if auth_configured() and auth_required(path) and not session_valid(self):
            if path.startswith("/api/"):
                self._send(401, json.dumps({"error": "sign in first"}), "application/json")
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
            return
        state, err = load_state()
        if path == "/api/health.json":
            self._send(503 if err else 200,
                       json.dumps({"error": err}) if err else json.dumps(state, indent=2),
                       "application/json")
        elif path == "/healthz":
            self._send(200 if not err else 503, "ok\n" if not err else err, "text/plain")
        elif EDITION == "deployed" and (path in ("/deploy", "/federation", "/networks",
                "/agent", "/chat", "/customization") or path.startswith("/api/setup/")
                or path in ("/api/sam/chat", "/api/propose", "/api/propose/dismiss",
                            "/api/fedlink/forget", "/api/fedmap/pos",
                            "/api/networks/channel/qr", "/api/networks/map")):
            # the deployed edition is one box's own console: no fleet deploy, no federation,
            # no estate agent, no white-labelling, and none of their APIs. Checked ahead of
            # every page and API route so nothing fleet-shaped answers on a forward box.
            self._send(404, render_error("Not part of this console.", "estate"),
                       "text/html; charset=utf-8")
        elif path in ("/", "/index.html"):
            self._send(200 if not err else 503, render(state, err), "text/html; charset=utf-8")
        elif path == "/operations":
            self._send(503 if err else 200,
                       render_error(err, "operations") if err else render_operations(state),
                       "text/html; charset=utf-8")
        elif path == "/federation":
            self._send(503 if err else 200,
                       render_error(err, "federation") if err else render_federation(state),
                       "text/html; charset=utf-8")
        elif path == "/networks":
            self._send(503 if err else 200,
                       render_error(err, "networks") if err else render_networks(state),
                       "text/html; charset=utf-8")
        elif path == "/api/networks/map":
            import urllib.parse as _up
            _q = _up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            if err:
                self._send(503, json.dumps({"error": err}), "application/json")
                return
            code, res = networks_map((_q.get("box", [""])[0] or "").strip(), state)
            self._send(code, json.dumps(res), "application/json")
        elif path == "/api/networks/channel/qr":
            import urllib.parse as _up
            _q = _up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            _nm = (_q.get("name", [""])[0] or "").strip()
            png = networks_channel_qr(_nm) if re.fullmatch(r"[A-Za-z0-9_-]{1,11}", _nm) else None
            if not png:
                self._send(404, "unknown channel, or qrencode unavailable\n", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)
        elif path == "/agent":
            self._send(503 if err else 200,
                       render_error(err, "agent") if err else render_agent(state),
                       "text/html; charset=utf-8")
        elif path == "/api/deployed/overview":
            url, tok = load_deployed_link()
            if not url:
                self._send(404, json.dumps({"error": "no deployed link configured"}),
                           "application/json")
            else:
                st, body = deployed_call("GET", "/admin/overview")
                if st != 200:
                    try:
                        why = json.loads(body or b"{}")
                    except ValueError:
                        why = {}
                    self._send(502, json.dumps(
                        {"error": why.get("error") or why.get("reason")
                         or f"the box refused ({st})"}), "application/json")
                else:
                    files = []
                    root = store_resolve("", "store")
                    if root and os.path.isdir(root):
                        for dp, dn, fn in os.walk(root):
                            dn[:] = [d2 for d2 in dn if not d2.startswith(".")]
                            for f2 in fn:
                                if f2.startswith("."):
                                    continue
                                files.append(os.path.relpath(os.path.join(dp, f2), root))
                    files.sort()
                    self._send(200, json.dumps(
                        {"url": url, "overview": json.loads(body),
                         "store_files": files[:500]}), "application/json")
        elif path.startswith("/api/deployed/packs"):
            import urllib.parse as _up
            dep = (_up.parse_qs(_up.urlparse(self.path).query).get("deployment") or [""])[0]
            st, body = deployed_call(
                "GET", "/admin/packs?deployment=" + _up.quote(dep, safe=""))
            self._send(st or 502, body or b"{}", "application/json")
        elif path == "/chat":
            # only meaningful when a resident agent is actually paired on this console;
            # otherwise it would leak another estate's plumbing into a product install
            if not sam_ready():
                self._send(404, json.dumps({"error": "no resident agent is paired"}),
                           "application/json")
                return
            self._send(503 if err else 200,
                       render_error(err, "chat") if err else render_chat(state),
                       "text/html; charset=utf-8")
        elif path == "/deploy":
            self._send(503 if err else 200,
                       render_error(err, "deploy") if err else render_deploy(state),
                       "text/html; charset=utf-8")
        elif path == "/api/setup/last-job":
            self._send(200, json.dumps(setup_last_job()), "application/json")
        elif path == "/api/agent/activity":
            self._send(200, json.dumps({"rows": agent_activity()}), "application/json")
        elif path == "/api/setup/preflight":
            import urllib.parse as _up
            _q = _up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            _fq = (_q.get("fqdn", [""])[0] or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", _fq):
                self._send(400, json.dumps({"error": "not a valid DNS name"}),
                           "application/json")
                return
            try:
                import socket as _sk
                _addrs = sorted({ai[4][0] for ai in _sk.getaddrinfo(_fq, None)
                                 if ai[0] in (_sk.AF_INET, _sk.AF_INET6)})
                self._send(200, json.dumps({"fqdn": _fq, "addrs": _addrs}),
                           "application/json")
            except Exception:
                self._send(200, json.dumps({"fqdn": _fq, "addrs": []}),
                           "application/json")
            return
        elif path == "/api/vault/graph":
            import urllib.parse as _up
            _q = _up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            self._send(200, json.dumps(vault_graph((_q.get("folder", [""])[0] or ""))),
                       "application/json")
        elif path == "/api/agent/chat/history":
            self._send(200, json.dumps({"messages": load_chat()[-60:]}), "application/json")
        elif path == "/customization":
            self._send(503 if err else 200,
                       render_error(err, "customization") if err else render_customization(state),
                       "text/html; charset=utf-8")
        elif path == "/eud":
            self._send(200, render_eud(), "text/html; charset=utf-8")
        elif path == "/eud/qr.png":
            host = re.sub(r"[^A-Za-z0-9.:\[\]-]", "", self.headers.get("Host") or "")
            if not host:
                self._send(404, "no host\n", "text/plain")
                return
            try:
                png = subprocess.run(["qrencode", "-t", "PNG", "-s", "6", "-m", "2", "-o", "-",
                                      f"http://{host}/eud"],
                                     capture_output=True, timeout=10).stdout
            except Exception:
                png = b""
            if not png:
                self._send(404, "qrencode unavailable\n", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)
        elif path == "/api/setup/deployments":
            self._send(200, json.dumps({"deployments": load_deployments()}), "application/json")
        elif path.startswith("/api/job/"):
            rec = job_status(path[len("/api/job/"):])
            if rec is None:
                self._send(404, json.dumps({"error": "no such job"}), "application/json")
            else:
                self._send(200, json.dumps(rec), "application/json")
        elif path == "/api/library":
            self._send(200, json.dumps({"packages": library_list(),
                                        "usage": library_usage()}), "application/json")
        elif path == "/api/store":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            rel = (q.get("path", [""]) or [""])[0]
            area = "vault" if (q.get("area", [""]) or [""])[0] == "vault" else "store"
            listing = store_list(rel, area)
            if listing is None:
                self._send(404, json.dumps({"error": "no such folder"}), "application/json")
            else:
                listing["area"] = area
                listing["folders_seed"] = STORE_FOLDERS if area == "store" else []
                self._send(200, json.dumps(listing), "application/json")
        elif path == "/api/vault/read":
            from urllib.parse import parse_qs, urlparse
            rel = (parse_qs(urlparse(self.path).query).get("path", [""]) or [""])[0]
            code, res = vault_read(rel)
            self._send(code, json.dumps(res), "application/json")
        elif path == "/api/vault/tree":
            self._send(200, json.dumps(vault_tree()), "application/json")
        elif path == "/store" or path == "/library":
            self._send(200 if not err else 503,
                       render_error(err, "store") if err else render_store(state, "store"),
                       "text/html; charset=utf-8")
        elif path == "/sync":
            self._send(503 if err else 200,
                       render_error(err, "sync") if err else render_sync(state),
                       "text/html; charset=utf-8")
        elif path == "/vault":
            self._send(200 if not err else 503,
                       render_error(err, "vault") if err else render_vault(state),
                       "text/html; charset=utf-8")
        elif path.startswith("/store/file/"):
            from urllib.parse import unquote
            self._send_store_file(unquote(path[len("/store/file/"):]), "store")
        elif path.startswith("/vault/file/"):
            from urllib.parse import unquote
            self._send_store_file(unquote(path[len("/vault/file/"):]), "vault")
        elif path.startswith("/server/"):
            if err:
                self._send(503, render_error(err), "text/html; charset=utf-8")
                return
            name = path[len("/server/"):].strip("/")
            page = render_server(state, name)
            if page is None:
                self._send(404, render_error(f"no server named '{name}' in the estate"),
                           "text/html; charset=utf-8")
            else:
                self._send(200, page, "text/html; charset=utf-8")
        else:
            self._send(404, "not found\n", "text/plain")

    def do_GET_peer(self, path):
        """Peer-token reads: the estate snapshot and vault folder bundles. Bearer only -
        no session, no cookie; a wrong or missing token is a 401 and an audit line."""
        auth = self.headers.get("Authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        peer = peer_for_token(tok) if tok else None
        if not peer:
            audit({"action": "peer-read", "path": path, "result": "DENIED",
                   "client": self.client_address[0]})
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid or missing peer token"}')
            return
        if path == "/api/estate/export":
            self._send(200, json.dumps(estate_snapshot()), "application/json")
            return
        if path == "/api/vault/export":
            import urllib.parse as _up
            q = _up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code, res = vault_export((q.get("folder", [""])[0] or "").strip())
            self._send(code, json.dumps(res), "application/json")
            return
        self._send(404, json.dumps({"error": "not a peer surface"}), "application/json")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/mcp":
            # the agent socket: bearer-token authed, its own JSON-RPC, no session or CSRF
            auth = self.headers.get("Authorization", "")
            tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            conn = connection_for_token(tok) if tok else None
            if not conn:
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"invalid or missing agent token"}')
                return
            ln = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(ln, 1 << 20)).decode("utf-8", "replace")
            try:
                req = json.loads(raw)
            except Exception:
                self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": "parse error"}}),
                           "application/json")
                return
            client = self.client_address[0]
            if isinstance(req, list):
                out = [r for r in (mcp_dispatch(conn, one, client) for one in req)
                       if r is not None]
                self._send(200, json.dumps(out), "application/json")
            else:
                resp = mcp_dispatch(conn, req, client)
                if resp is None:
                    self.send_response(202)
                    self.end_headers()
                else:
                    self._send(200, json.dumps(resp), "application/json")
            return
        if path == "/login":
            ip = self.client_address[0]
            if login_throttled(ip):
                self._send(429, "Too many attempts. Wait ten minutes.", "text/plain")
                return
            ln = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(ln, 4096)).decode("utf-8", "replace")
            ctype = self.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    pw = str(json.loads(body).get("password", ""))
                except Exception:
                    pw = ""
            else:
                import urllib.parse as _up
                pw = _up.parse_qs(body).get("password", [""])[0]
            if auth_configured() and verify_operator_password(pw):
                tok = session_create()
                self.send_response(302)
                self.send_header("Set-Cookie",
                                 f"vsession={tok}; Path=/; HttpOnly; SameSite=Lax; "
                                 f"Max-Age={SESSION_HOURS * 3600}")
                self.send_header("Location", "/")
                self.end_headers()
                audit({"action": "login", "result": "OK", "client": ip})
            else:
                login_failed(ip)
                audit({"action": "login", "result": "DENIED", "client": ip})
                self._send(200, render_login("Wrong password."), "text/html; charset=utf-8")
            return
        if auth_configured() and auth_required(path) and not session_valid(self):
            self._send(401, json.dumps({"error": "sign in first"}), "application/json")
            return
        if EDITION != "deployed" and not AUTH_OPEN_MODE and not auth_configured() \
                and auth_required(path) and path != "/api/setup/password":
            self._send(401, json.dumps({"error": "set the operator password first"}),
                       "application/json")
            return
        # the deployed edition serves one box and holds no estate machinery: setup, the
        # agent, federation and library-fetch APIs simply are not there. Store and vault
        # writes (local file management) and box-scoped actions stay.
        if EDITION == "deployed" and (path.startswith("/api/setup/")
                or path in ("/api/propose", "/api/propose/dismiss", "/api/sam/chat",
                            "/api/fedlink/forget", "/api/fedmap/pos",
                            "/api/library/upload", "/api/library/upload-image",
                            "/api/library/delete", "/api/library/fetch",
                            "/api/networks/channel", "/api/networks/channel/adopt",
                            "/api/networks/channel/delete", "/api/networks/gwpos")):
            self._send(404, json.dumps({"error": "not part of this console"}), "application/json")
            return
        # streamed uploads first: they carry raw bodies far beyond the JSON cap
        if path in ("/api/library/upload", "/api/library/upload-image",
                    "/api/store/upload", "/api/setup/upload-key"):
            code, res = save_upload(self, path)
            self._send(code, json.dumps(res), "application/json")
            return
        if not (path.startswith("/api/action/") or path.startswith("/api/setup/")
                or path.startswith("/api/agent/") or path.startswith("/api/vault/")
                or path.startswith("/api/peers/") or path.startswith("/api/deployed/")
                or path in ("/api/propose", "/api/propose/dismiss", "/api/fedlink/forget",
                            "/api/sam/chat", "/api/fedmap/pos",
                            "/api/library/delete", "/api/library/fetch",
                            "/api/enrol-batch", "/api/usb-copy", "/api/module/load-offline",
                            "/api/usb-list", "/api/usb-import", "/api/software/current",
                            "/api/destroy", "/api/console/set-mode", "/api/console/kiosk",
                            "/api/store/mkdir", "/api/store/move", "/api/store/delete",
                            "/api/vault/save", "/api/networks/channel",
                            "/api/networks/channel/adopt",
                            "/api/networks/channel/delete", "/api/networks/gwpos")):
            self._send(404, json.dumps({"error": "not found"}), "application/json")
            return
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n) if 0 < n < 65536 else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}), "application/json")
            return
        client = self.address_string()
        if path == "/api/propose":
            # tasking the agent queue is for co-located agents (Sam), never the wider
            # tailnet; the browser only confirms and dismisses
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._send(403, json.dumps({"error": "proposals are accepted from this box only"}),
                           "application/json")
                return
            code, res = ingest_proposal(data, client)
        elif path == "/api/propose/dismiss":
            code, res = dismiss_proposal(data.get("id"), client)
        elif path == "/api/fedlink/forget":
            if forget_fedlink(str(data.get("source", "")), str(data.get("address", "")),
                              str(data.get("port", ""))):
                code, res = 200, {"status": "forgotten"}
            else:
                code, res = 404, {"error": "no such link on the map"}
        elif path == "/api/sam/chat":
            code, res = sam_chat(data.get("text"), client)
        elif path == "/api/fedmap/pos":
            if save_fedpos(data.get("name"), data.get("x"), data.get("y")):
                code, res = 200, {"status": "saved"}
            else:
                code, res = 400, {"error": "bad name or position"}
        elif path == "/api/setup/run":
            code, res = start_setup_job(data, client,
                                        authed=auth_configured() and session_valid(self))
        elif path == "/api/setup/deployment":
            code, res = save_deployment(data)
        elif path == "/api/setup/deployment/delete":
            code, res = delete_deployment(data)
        elif path == "/api/setup/instance":
            code, res = save_instance(data, client)
        elif path == "/api/agent/connect":
            code, res = create_connection(data, client)
        elif path == "/api/agent/autonomy":
            code, res = update_connection(data, client)
        elif path == "/api/agent/revoke":
            code, res = revoke_connection(data, client)
        elif path == "/api/agent/key":
            code, res = save_agent_key(data, client)
        elif path == "/api/agent/key/clear":
            code, res = clear_agent_key(client)
        elif path == "/api/agent/https-enable":
            fqdn = str(data.get("fqdn", "")).strip().lower()
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
                                r"(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", fqdn):
                code, res = 400, {"error": "not a valid DNS name"}
            elif tls_active():
                code, res = 400, {"error": "https is already on"}
            else:
                ok, txt = setup_helper("tlscert", "tls", fqdn)
                if not ok:
                    code, res = 500, {"error": txt[:300] or "certificate copy failed"}
                else:
                    audit({"action": "https-enable", "fqdn": fqdn, "result": "OK",
                           "client": client})
                    code, res = 200, {"restarting": True, "fqdn": fqdn}
                    # answer first, then exit clean; systemd's Restart=always brings
                    # the console back wearing the certificate
                    threading.Timer(1.5, os._exit, args=(0,)).start()
        elif path == "/api/agent/chat":
            code, res = agent_chat(data, client)
        elif path == "/api/peers/mint":
            code, res = peers_mint(data, client)
        elif path == "/api/peers/add":
            code, res = peers_add(data, client)
        elif path == "/api/peers/remove":
            code, res = peers_remove(data, client)
        elif path == "/api/peers/pull":
            code, res = peer_pull(data, client)
        elif path == "/api/peers/discover":
            code, res = sync_discover(client)
        elif path == "/api/vault/pull":
            code, res = vault_pull(data, client)
        elif path == "/api/vault/mission-pack":
            code, res = build_mission_pack(data, client)
        elif path == "/api/agent/chat/clear":
            code, res = clear_chat(client)
        elif path == "/api/agent/context":
            code, res = save_agent_context_doc(data, client)
        elif path == "/api/enrol-batch":
            code, res = enrol_batch_api(data, client)
        elif path == "/api/usb-copy":
            code, res = usb_copy_api(data, client)
        elif path == "/api/module/load-offline":
            code, res = load_offline_api(data, client)
        elif path == "/api/usb-list":
            code, res = usb_import_list(client)
        elif path == "/api/usb-import":
            code, res = usb_import_file(data, client)
        elif path == "/api/software/current":
            code, res = software_current_api(data, client)
        elif path == "/api/destroy":
            code, res = destroy_api(data, client)
        elif path == "/api/console/set-mode":
            code, res = set_console_mode_api(data, client)
        elif path == "/api/console/kiosk":
            code, res = set_kiosk_api(data, client)
        elif path.startswith("/api/setup/"):
            code, res = setup_api(path, data, client)
        elif path == "/api/library/delete":
            code, res = library_delete(data.get("file"), client)
        elif path == "/api/library/fetch":
            code, res = start_library_fetch(data, client)
        elif path == "/api/store/mkdir":
            code, res = store_mkdir(data.get("path", ""), data.get("name"), client,
                                    "vault" if data.get("area") == "vault" else "store")
        elif path == "/api/store/move":
            code, res = store_move(data.get("from"), data.get("to"), client,
                                   "vault" if data.get("area") == "vault" else "store")
        elif path == "/api/store/delete":
            code, res = store_delete(data.get("path"), client,
                                     "vault" if data.get("area") == "vault" else "store")
        elif path == "/api/vault/save":
            code, res = vault_save(data.get("path"), data.get("content"), client)
        elif path == "/api/vault/scaffold":
            code, res = scaffold_ontology(data, client)
        elif path == "/api/vault/rename":
            code, res = vault_rename(data.get("from"), data.get("to"), client)
        elif path == "/api/vault/restore":
            code, res = vault_restore(data, client)
        elif path == "/api/networks/channel":
            code, res = networks_channel_create(data, client)
        elif path == "/api/networks/channel/adopt":
            code, res = networks_channel_adopt(data, client)
        elif path == "/api/networks/gwpos":
            code, res = networks_gwpos(data, client)
        elif path == "/api/networks/channel/delete":
            code, res = networks_channel_delete(data, client)
        elif path.startswith("/api/deployed/"):
            code, res = deployed_api(path, data, client)
        else:
            aid = path.split("/api/action/", 1)[1]
            if aid == "upgrade-server":
                code, res = start_upgrade_job(data.get("target"), data.get("inputs", {}),
                                              bool(data.get("confirm")), client)
            elif aid == "deploy-mesh-gateway":
                code, res = start_mesh_deploy_job(data.get("target"), data.get("inputs", {}),
                                                  bool(data.get("confirm")), client)
            elif ACTIONS.get(aid, {}).get("job"):
                code, res = start_job(aid, data.get("target"), data.get("inputs", {}),
                                      bool(data.get("confirm")), client)
            else:
                code, res = run_action(aid, data.get("target"), data.get("inputs", {}),
                                       data.get("passphrase"), bool(data.get("confirm")), client)
        self._send(code, json.dumps(res), "application/json")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def sweep_seeded_agent_context():
    """One-time tidy: the old build seeded an 'Agent Context' folder into the vault.
    That was clutter (setup is not knowledge). If the folder holds ONLY the three
    generic stubs we shipped - unedited - remove it; if the operator wrote anything
    real, leave it and let them move it by hand."""
    root = store_resolve(AGENT_CONTEXT_DIR if False else "Agent Context", "vault")
    if not root or not os.path.isdir(root):
        return
    stub_marks = ("Edit this note to shape", "Examples to replace with your own",
                  "Brief the assistant like a new colleague")
    files = [f for f in os.listdir(root) if not f.startswith(".")]
    if not files:
        try:
            os.rmdir(root)
        except Exception:
            pass
        return
    all_stub = True
    for f in files:
        if not f.lower().endswith((".md", ".txt")):
            all_stub = False
            break
        try:
            with open(os.path.join(root, f), errors="ignore") as fh:
                body = fh.read()
        except Exception:
            all_stub = False
            break
        if not any(m in body for m in stub_marks):
            all_stub = False
            break
    if not all_stub:
        return
    for f in files:
        try:
            os.remove(os.path.join(root, f))
        except Exception:
            pass
    try:
        os.rmdir(root)
        audit({"action": "agent-context-tidy", "result": "OK",
               "reason": "removed seeded stubs from the vault"})
    except Exception:
        pass


def sweep_orphaned_jobs():
    """Jobs run as threads in this process, so a restart kills them mid-flight - and
    their records would say 'running' forever, leaving the operator staring at a frozen
    log with no verdict (bitten live, 27 Aug). At startup, any record still marked
    running is marked failed with an honest note. The real fix - jobs that survive
    restarts - is card 6172; until then the console at least never lies."""
    try:
        for fn in os.listdir(JOBS_DIR):
            if not fn.endswith(".json"):
                continue
            jid = fn[:-5]
            try:
                with open(os.path.join(JOBS_DIR, fn)) as fh:
                    rec = json.load(fh)
            except Exception:
                continue
            if rec.get("status") == "running":
                rec["status"] = "failed"
                rec["rc"] = -1
                _job_write(jid, rec)
                try:
                    with open(os.path.join(JOBS_DIR, jid + ".log"), "a") as fh:
                        fh.write("\nERR the console restarted while this job ran; the job "
                                 "died with it. Run again - completed stages skip.\n")
                except Exception:
                    pass
    except Exception:
        pass


if __name__ == "__main__":
    sweep_orphaned_jobs()
    sweep_seeded_agent_context()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    scheme = "http"
    tls_dir = os.environ.get("VANTAGE_CONSOLE_TLS_DIR", "/etc/vantage-console/tls")
    cert = os.path.join(tls_dir, "fullchain.pem")
    key = os.path.join(tls_dir, "privkey.pem")
    if os.path.isfile(cert) and os.path.isfile(key):
        # the box already earned a trusted certificate at build time (Let's Encrypt,
        # for TAK); the console wears the same one, so cloud connectors (claude.ai,
        # Cowork) that demand https can reach /mcp with no reverse proxy
        import ssl
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            scheme = "https"
        except Exception as e:
            print(f"TLS configured but unusable ({e}); serving http", flush=True)
    print(f"vantage-console {VERSION} on {scheme}://{BIND}:{PORT}", flush=True)
    srv.serve_forever()
