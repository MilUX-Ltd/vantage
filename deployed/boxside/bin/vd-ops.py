#!/usr/bin/env python3
"""Operator CLI for the enrolment surface (Spec 002): mint, pending, confirm, reject, revoke,
config. The console pages land later; this stays for headless boxes.

    vd-ops.py --state-dir ~/vd-state mint --device "S23" --holder "Cpl Bloggs" \
        --deployment "OP TELIC" --ceiling OFFICIAL
    vd-ops.py --state-dir ~/vd-state pending
    vd-ops.py --state-dir ~/vd-state confirm --fingerprint <fp>
    vd-ops.py --state-dir ~/vd-state revoke --fingerprint <fp> --reason "end of deployment"
    vd-ops.py --state-dir ~/vd-state config --staleness 604800
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from vaultsync.enrolment import pairing_code  # noqa: E402
from vaultsync.syncsurface import BoxState, STALENESS_MENU_S  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mint", help="mint a single-use enrolment token, print the QR payload")
    m.add_argument("--device", required=True)
    m.add_argument("--holder", required=True)
    m.add_argument("--deployment", required=True)
    m.add_argument("--ceiling", default="OFFICIAL")
    m.add_argument("--ttl", type=int, default=600)

    sub.add_parser("pending", help="list pending enrolments awaiting the fingerprint check")

    c = sub.add_parser("confirm", help="operator has checked the code; write to the roll")
    c.add_argument("--fingerprint", default=None)
    c.add_argument("--code", default=None, help="the 6-digit pairing code the holder reads")
    r = sub.add_parser("reject", help="discard a pending enrolment")
    r.add_argument("--fingerprint", required=True)
    v = sub.add_parser("revoke", help="tombstone an enrolled device")
    v.add_argument("--fingerprint", required=True)
    v.add_argument("--reason", required=True)

    pa = sub.add_parser("pack", help="the deployment manifest: assign, list, unassign packs (ADR-003)")
    pasub = pa.add_subparsers(dest="packcmd", required=True)
    padd = pasub.add_parser("add", help="assign a pack file to a deployment (the sharing decision)")
    padd.add_argument("--deployment", required=True)
    padd.add_argument("--file", required=True)
    padd.add_argument("--kind", required=True, choices=["mission", "map"])
    plist = pasub.add_parser("list", help="what a deployment carries")
    plist.add_argument("--deployment", required=True)
    prem = pasub.add_parser("remove", help="unassign a pack from a deployment")
    prem.add_argument("--deployment", required=True)
    prem.add_argument("--name", required=True)

    g = sub.add_parser("config", help="show, or set the staleness bound (ADR-002 menu)")
    g.add_argument("--staleness", type=int, default=None,
                   help=f"seconds, one of {STALENESS_MENU_S}")

    args = ap.parse_args()
    state = BoxState(os.path.expanduser(args.state_dir))
    now = int(time.time())

    def resolve_fp(fingerprint, code):
        if fingerprint:
            return fingerprint
        matches = [p.fingerprint for p in state.enrolment.pending()
                   if pairing_code(p.fingerprint) == (code or "").replace(" ", "")]
        if len(matches) != 1:
            sys.exit(f"{'no' if not matches else 'more than one'} pending enrolment for that code")
        return matches[0]

    if args.cmd == "mint":
        payload = state.mint_qr(device_label=args.device, holder=args.holder,
                                deployment_scope=args.deployment,
                                clearance_ceiling=args.ceiling, now=now, ttl_s=args.ttl)
        print(payload)
        print(f"# single use, expires in {args.ttl}s; render as a QR or hand over as text",
              file=sys.stderr)
    elif args.cmd == "pending":
        for p in state.enrolment.pending():
            code = pairing_code(p.fingerprint)
            print(f"code {code[:3]} {code[3:]}  {p.device_label} / {p.holder}  "
                  f"{p.deployment_scope}  ({p.fingerprint[:12]}...)")
        if not state.enrolment.pending():
            print("(none)")
    elif args.cmd == "confirm":
        roll = state.confirm(resolve_fp(args.fingerprint, args.code), now=now)
        print(f"enrolled; roll now v{roll.version}")
    elif args.cmd == "reject":
        print("rejected" if state.enrolment.reject(args.fingerprint) else "no such pending")
    elif args.cmd == "revoke":
        ok = state.revoke(args.fingerprint, args.reason, now=now)
        print("revoked (tombstone)" if ok else "no such device")
    elif args.cmd == "pack":
        if args.packcmd == "add":
            e = state.packs.add(args.deployment, os.path.expanduser(args.file),
                                kind=args.kind, now=now)
            print(f"assigned {e['name']} ({e['kind']}, {e['size']} bytes) to {args.deployment}")
        elif args.packcmd == "list":
            rows = state.packs.list_for(args.deployment)
            for e in rows:
                print(f"{e['kind']:8} {e['size']:>10}  {e['name']}  {e['sha256'][:12]}...")
            if not rows:
                print("(none)")
        elif args.packcmd == "remove":
            print("unassigned" if state.packs.remove(args.deployment, args.name)
                  else "no such pack")
    elif args.cmd == "config":
        if args.staleness is not None:
            state.set_staleness(args.staleness)
        print(f"box={state.box} staleness_bound_s={state.staleness_bound_s()} "
              f"roll_v{state.roll().version}")
        print(f"admin_token={state.admin_token}  (the console's Deployed page presents this)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
