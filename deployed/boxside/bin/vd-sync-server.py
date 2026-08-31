#!/usr/bin/env python3
"""Run the box's enrolment and sync surface (Spec 002).

Foreground, one process, stdlib only. Bind to the address the phones will reach (the tailnet
address, or the kit LAN on a deployed box); never bind wider than needed. State lives under
--state-dir, never inside a vault.

    python3 vd-sync-server.py --state-dir ~/vd-state --bind 100.x.y.z --port 8095 \
        --box "NUC" --base-url http://100.x.y.z:8095
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from vaultsync.syncsurface import BoxState, SyncServer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--bind", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--box", default=None, help="box name shown in QR payloads (kept once set)")
    ap.add_argument("--base-url", default=None, help="sync base url carried in QR payloads")
    ap.add_argument("--vault", default=None,
                    help="the vault this box serves over the transport (kept once set)")
    ap.add_argument("--read-only", action="store_true",
                    help="serve pulls only; pushes are politely refused")
    args = ap.parse_args()

    state = BoxState(os.path.expanduser(args.state_dir), box=args.box, base_url=args.base_url,
                     vault=os.path.expanduser(args.vault) if args.vault else None,
                     read_only=args.read_only)
    server = SyncServer(state, bind=args.bind, port=args.port)
    server.start()
    print(f"vd-sync-server: {state.box} on {args.bind}:{server.port}, "
          f"state {state.state_dir}, vault {state.vault or '(none)'}, "
          f"staleness {state.staleness_bound_s()}s"
          f"{', READ-ONLY' if state.read_only else ''}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
