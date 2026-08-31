#!/usr/bin/env python3
# Vantage - fleet management for TAK.  Copyright (c) 2026 MilUX Ltd.
# Source-available under the Vantage Community Licence (see LICENSE): free for
# non-commercial use; commercial, government, and MOD use require a licence -
# matt@milux.co.uk
"""
vantage-console-collect.py - gather health from every target, write one state file.

Runs on a systemd timer. Deliberately separate from the server: if collection
wedges, the server keeps serving and the page goes visibly stale; if the server
dies, systemd restarts it and collection is unaffected. infra-TAK needed a daily
console restart timer to recover wedged workers; splitting the two avoids the
class of problem rather than scheduling a workaround for it.

STATE IS DERIVED, NEVER HELD. This file is a cache. Delete it and the next run
rebuilds it. Nothing here is a source of truth.

Standard library only. The NUC must not need pip to monitor itself, and the
deployable kit has no internet at all.

Argos, 2026-08-23. Businessmap card 6165.
"""
import json, os, subprocess, sys, tempfile, time
from datetime import datetime, timezone

VERSION = "1.2.1"

CONFIG = os.environ.get("VANTAGE_CONSOLE_CONFIG", "/etc/vantage-console/targets.json")
STATE  = os.environ.get("VANTAGE_CONSOLE_STATE",  "/var/lib/vantage-console/state.json")
HISTORY = os.environ.get("VANTAGE_CONSOLE_HISTORY", "/var/lib/vantage-console/history.ndjson")
RETAIN_DAYS = int(os.environ.get("VANTAGE_CONSOLE_RETAIN_DAYS", "30"))
TIMEOUT = int(os.environ.get("VANTAGE_CONSOLE_TIMEOUT", "45"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, timeout):
    """Return (rc, stdout, stderr). Never raises on a failing target."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)
    except Exception as e:                       # noqa: BLE001 - a target must never kill the run
        return 1, "", f"{type(e).__name__}: {e}"


def collect_one(t):
    """
    One target -> one record. A target that is unreachable is reported as
    unreachable, never as healthy and never as an exception.
    """
    started = time.time()
    kind = t.get("kind", "ssh")

    if kind == "local":
        cmd = [t["command"], "--profile", t["profile"], "--json"]
    elif kind == "ssh":
        cmd = ["ssh", "-o", "BatchMode=yes",
               "-o", f"ConnectTimeout={t.get('connect_timeout', 10)}",
               "-o", "StrictHostKeyChecking=accept-new"]
        if t.get("identity"):
            cmd += ["-i", t["identity"]]
        cmd += [t["ssh"]]
        # The remote authorized_keys entry carries a forced command, so nothing
        # sent here is executed. This argument exists only to be ignored, and to
        # document intent at the calling end.
        hc = f"tak-health --profile {t['profile']} --json"
        # Per-box recorded facts beat profile defaults (the checker's override flags); the
        # product's profiles carry no estate values, so an estate records its own here.
        for key, flag in (("fqdn", "--fqdn"), ("hostmatch", "--hostmatch"),
                          ("probe", "--probe"), ("tscert", "--tscert")):
            v = t.get(key)
            if v:
                hc += f" {flag} '{v}'"
        cmd += [hc]
    else:
        return {"name": t["name"], "reachable": False, "result": "UNKNOWN",
                "error": f"unknown target kind '{kind}'", "checked_at": now_iso()}

    rc, out, err = run(cmd, t.get("timeout", TIMEOUT))
    elapsed = round(time.time() - started, 2)

    rec = {
        "name": t["name"],
        "label": t.get("label", t["name"]),
        "profile": t.get("profile"),
        "kind": kind,
        "checked_at": now_iso(),
        "elapsed_s": elapsed,
        "expected_offline": bool(t.get("expected_offline", False)),
        # A browser-reachable host for service links: an explicit override, else the
        # ssh host the console already uses to reach the box (a tailnet name or the
        # public FQDN, both of which resolve from the operator's browser). Local
        # targets fall back to the reported fqdn below.
        "link_host": t.get("link_host")
        or (t["ssh"].split("@")[-1] if kind == "ssh" and t.get("ssh") else None),
    }

    # rc 0/1/2 are tak-health verdicts. Anything else means we never got a verdict.
    if rc in (0, 1, 2) and out.strip():
        try:
            payload = json.loads(out)
            rec.update({
                "reachable": True,
                "result": payload.get("result", "UNKNOWN"),
                "host": payload.get("host"),
                "fqdn": payload.get("fqdn"),
                "counts": payload.get("counts", {}),
                "checks": payload.get("checks", []),
                # 1.1.0: the software inventory rides along when the box's
                # checker is new enough to report one. Absent is absent, not [].
                "software": payload.get("software", []),
                # Spec 002: the declared loadout rides along (checker 1.6.0+);
                # absent means the box's checker predates declarations.
                "loadout": payload.get("loadout"),
                "schema": payload.get("schema"),
                "checker_version": payload.get("version"),
            })
            # the mesh heartbeat (Spec 001) rides along when the box runs a gateway
            # and its checker is new enough to report one. Absent is absent.
            if payload.get("mesh"):
                rec["mesh"] = payload["mesh"]
            if not rec.get("link_host"):
                rec["link_host"] = payload.get("fqdn")
            return rec
        except json.JSONDecodeError as e:
            rec.update({"reachable": True, "result": "UNKNOWN",
                        "error": f"target returned rc={rc} but unparseable JSON: {e}",
                        "raw": out[:500]})
            return rec

    rec.update({
        "reachable": False,
        "result": "UNREACHABLE",
        "error": (err.strip() or out.strip() or f"rc={rc}")[:500],
        "rc": rc,
    })
    return rec


def main():
    try:
        with open(CONFIG) as fh:
            cfg = json.load(fh)
    except Exception as e:                       # noqa: BLE001
        print(f"cannot read config {CONFIG}: {e}", file=sys.stderr)
        return 2

    targets = cfg.get("targets", [])
    records = [collect_one(t) for t in targets]

    # Previous poll's RAW per-target result, for the flap debounce below.
    prev_raw = {}
    try:
        with open(HISTORY) as fh:
            last = fh.read().splitlines()[-1]
        for t in json.loads(last).get("targets", []):
            prev_raw[t.get("name")] = t.get("result_raw", t.get("result"))
    except Exception:
        pass

    # Estate verdict. Two things keep it from crying wolf:
    #
    # 1. An expected-offline target that is unreachable is NOT a fault - the
    #    deployable kit going dark is its designed behaviour.
    # 2. FLAP DEBOUNCE. A target that failed only THIS poll, having been healthy
    #    on the previous one, is UNCONFIRMED: shown as WARN, not FAIL, and not
    #    counted as a hard failure against the estate. A public TAK server's 8089
    #    accept queue briefly overflows under scanner bursts and drops a SYN, and
    #    the one-shot health probe must not turn the whole estate red for it (cloud
    #    flapped FAIL->OK seven times in four hours, 25 Aug 2026, "100 SYNs to
    #    LISTEN dropped", while every real client stayed connected). A genuine
    #    outage fails a second consecutive poll and escalates within one cycle; a
    #    blip never does. result_raw keeps the checker's true verdict for the record.
    worst = "OK"
    rank = {"OK": 0, "WARN": 1, "FAIL": 2, "UNKNOWN": 1, "UNREACHABLE": 2, "OFFLINE": 0}
    for r in records:
        r["result_raw"] = r.get("result")
        v = r.get("result", "UNKNOWN")
        if v == "UNREACHABLE" and r.get("expected_offline"):
            r["result"] = "OFFLINE"
            r["note"] = "offline by design, not counted against the estate"
        elif v in ("FAIL", "UNREACHABLE") and prev_raw.get(r["name"]) not in ("FAIL", "UNREACHABLE"):
            r["unconfirmed"] = True
            r["result"] = "WARN"
            r["note"] = f"failed one poll ({v.lower()}); holding until the next poll confirms"
        vr = r["result"]
        if rank.get(vr, 1) > rank.get(worst, 0):
            worst = vr

    # A box running an old checker reports old truths. That is invisible unless
    # something compares the versions, and the MINIX is away with the first
    # build on it right now.
    seen = sorted({r.get("checker_version") for r in records
                   if r.get("checker_version")})
    drift = seen if len(seen) > 1 else []

    state = {
        "schema": "vantage.console/1",
        "console_version": VERSION,
        "generated_at": now_iso(),
        "console_host": os.uname().nodename,
        "estate_result": worst,
        "checker_versions": seen,
        "checker_drift": drift,
        "blind_spot": ("This console cannot report its own box being down. If that "
                       "matters, watch this box from somewhere else too."),
        "targets": records,
    }

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    # Atomic write: a half-written state file must never be served.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE), prefix=".state-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATE)
    except Exception:
        os.path.exists(tmp) and os.unlink(tmp)
        raise

    append_history(state)

    print(f"{state['generated_at']}  estate={worst}  targets={len(records)}")
    return 0


def append_history(state):
    """One compact line per poll, retained ~RETAIN_DAYS. This is the ONE place state
    is deliberately kept rather than derived: a transient FAIL was unrecoverable and
    a week of handshake bursts went unnoticed because nothing remembered anything
    (handover, item 1). Kept small on purpose: per-target verdict, counts, checker
    version, and the numeric values the schema/2 checks now carry. ~288 lines/day,
    a few hundred bytes each.
    """
    line = {
        "ts": state["generated_at"],
        "estate": state["estate_result"],
        "targets": [],
    }
    for r in state.get("targets", []):
        t = {
            "name": r.get("name"),
            "result": r.get("result"),
            "result_raw": r.get("result_raw", r.get("result")),  # for the flap debounce
            "reachable": bool(r.get("reachable")),
            "counts": r.get("counts") or {},
            "v": r.get("checker_version"),
        }
        values = {}
        for c in (r.get("checks") or []):
            if isinstance(c.get("value"), (int, float)):
                values[f"{c.get('category','')}/{c.get('name','')}"] = c["value"]
        if values:
            t["values"] = values
        line["targets"].append(t)

    cutoff = time.time() - RETAIN_DAYS * 86400
    kept = []
    try:
        with open(HISTORY) as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                # json.loads, not string surgery: the first cut split on '"ts": "'
                # while the writer uses compact separators, so every prior line was
                # silently dropped and the file never grew past one entry. The
                # gather step is where the bugs live (handover rule 4).
                try:
                    t = datetime.strptime(json.loads(raw).get("ts", ""),
                                          "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    if t.timestamp() >= cutoff:
                        kept.append(raw)
                except (json.JSONDecodeError, ValueError, AttributeError):
                    continue  # a malformed line is dropped, never fatal
    except FileNotFoundError:
        pass
    except Exception as e:                       # noqa: BLE001 - history must never kill the poll
        print(f"history read failed, starting fresh: {e}", file=sys.stderr)

    kept.append(json.dumps(line, separators=(",", ":")))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(HISTORY), prefix=".hist-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(kept) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, HISTORY)
    except Exception as e:                       # noqa: BLE001
        os.path.exists(tmp) and os.unlink(tmp)
        print(f"history write failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
