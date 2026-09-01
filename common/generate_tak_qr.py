#!/usr/bin/env python3
"""Generate TAK enrolment QR codes for a MilUX TAK Server (the TAK Playground).

Companion to the MilUX TAK Server Install Guide and the generate-tak-qr skill.
Turns a server FQDN and a list of users into scannable QR codes:

  - ATAK (Android): one QR per user, encoding
        tak://com.atakmap.app/enroll?host=<FQDN>&username=<U>&token=<P>
    Scanning in ATAK 5.1+ enrols the device and connects in one step.
  - iTAK (iPhone): one server-connect QR for the whole server,
        "<friendly name>,<FQDN>,8089,ssl"
    iTAK prompts for username and password after scanning.
  - qr-sheet.html: a printable contact sheet of every code with labels
    (no passwords shown), for handing out on the day.

Runs entirely offline. No network calls. Passwords are never printed to the
terminal or written into the contact sheet.

SECURITY: the ATAK QR carries a live username and password in plain text.
Treat every generated code as a credential. The output is sensitive and must
never be committed to git or a vault (see the .gitignore and the skill).

Usage:
    pip install "qrcode[pil]" --break-system-packages

    # from a users file (one "username,password" per line; blank / # lines ignored)
    python generate_tak_qr.py --host tak.example.com --users users.txt \
        --friendly-name "MilUX TAK Playground" --out ./tak-qr

    # or inline (repeatable), handy for one or two people
    python generate_tak_qr.py --host tak.example.com \
        --user player01:THEIR_PASSWORD --user player02:THEIR_PASSWORD --out ./tak-qr

    # only the iTAK server-connect QR (no per-user credentials)
    python generate_tak_qr.py --host tak.example.com --itak-only --out ./tak-qr
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path
from urllib.parse import quote

try:
    import qrcode
except ImportError:
    sys.exit(
        "The 'qrcode' library is required.\n"
        '  pip install "qrcode[pil]" --break-system-packages'
    )

STREAMING_PORT = "8089"  # TAK SSL streaming port; must be open on the server.
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def parse_users_file(path: Path) -> list[tuple[str, str]]:
    """Parse a users file: one 'username,password' per line. Blank / # ignored."""
    users: list[tuple[str, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            sys.exit(f"{path}:{lineno}: expected 'username,password', got: {raw!r}")
        username, password = line.split(",", 1)
        username, password = username.strip(), password.strip()
        if not username or not password:
            sys.exit(f"{path}:{lineno}: username and password must both be non-empty")
        users.append((username, password))
    return users


def parse_inline_user(spec: str) -> tuple[str, str]:
    """Parse an inline user spec: 'username:password' or 'username,password'.

    The tak-user.sh 'add' helper emits comma-separated lines, so accept either
    separator (usernames contain neither, and generated passwords contain neither),
    splitting on whichever appears first.
    """
    seps = [i for i in (spec.find(":"), spec.find(",")) if i >= 0]
    if not seps:
        sys.exit(f"--user expects 'username:password' or 'username,password', got: {spec!r}")
    idx = min(seps)
    username, password = spec[:idx].strip(), spec[idx + 1:].strip()
    if not username or not password:
        sys.exit(f"--user '{spec}': username and password must both be non-empty")
    return username, password


def safe_filename(username: str) -> str:
    return _SAFE_FILENAME.sub("_", username).strip("_") or "user"


def write_qr(data: str, out_path: Path) -> None:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(out_path)


def atak_enrol_url(host: str, username: str, password: str) -> str:
    return (
        "tak://com.atakmap.app/enroll"
        f"?host={quote(host, safe='')}"
        f"&username={quote(username, safe='')}"
        f"&token={quote(password, safe='')}"
    )


def itak_connect_string(friendly_name: str, host: str) -> str:
    return f"{friendly_name},{host},{STREAMING_PORT},ssl"


def build_contact_sheet(
    friendly_name: str,
    host: str,
    atak_files: list[tuple[str, str]],  # (username, png filename)
    itak_file: str | None,
) -> str:
    """A printable sheet of every code, labelled by username. No passwords."""
    cards = []
    for username, fname in atak_files:
        cards.append(
            "<figure class='card'>"
            f"<img src='{html.escape(fname)}' alt='ATAK enrolment QR for "
            f"{html.escape(username)}'>"
            f"<figcaption><span class='u'>{html.escape(username)}</span>"
            "<span class='t'>ATAK · Android</span></figcaption></figure>"
        )
    if itak_file:
        cards.append(
            "<figure class='card'>"
            f"<img src='{html.escape(itak_file)}' alt='iTAK server-connect QR'>"
            "<figcaption><span class='u'>Server connect</span>"
            "<span class='t'>iTAK · iPhone (prompts for login)</span>"
            "</figcaption></figure>"
        )
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<title>{html.escape(friendly_name)} — enrolment QR codes</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem;
         color: #113308; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  p.sub {{ color: #586F7C; margin: 0 0 1.5rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
          gap: 1.25rem; }}
  .card {{ margin: 0; padding: 1rem; border: 1px solid #D2C78D; border-radius: 8px;
          text-align: center; break-inside: avoid; }}
  .card img {{ width: 100%; max-width: 200px; height: auto; }}
  figcaption {{ margin-top: .5rem; display: flex; flex-direction: column; }}
  .u {{ font-weight: 600; }}
  .t {{ color: #586F7C; font-size: .8rem; }}
  @media print {{ body {{ margin: 1cm; }} }}
</style>
</head>
<body>
<h1>{html.escape(friendly_name)}</h1>
<p class="sub">Server: {html.escape(host)} · scan to join. Each ATAK code is a
credential — hand out one per person and disable the account afterwards.</p>
<div class="grid">
{chr(10).join(cards)}
</div>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate TAK enrolment QR codes for a MilUX TAK Server."
    )
    ap.add_argument("--host", required=True, help="Server FQDN, e.g. tak.example.com")
    ap.add_argument(
        "--users", type=Path,
        help="Path to a users file ('username,password' per line).",
    )
    ap.add_argument(
        "--user", action="append", default=[], metavar="USER:PASS",
        help="Inline user 'username:password'. Repeatable.",
    )
    ap.add_argument(
        "--friendly-name", default="MilUX TAK Playground",
        help="Friendly server name shown in the iTAK code and on the sheet.",
    )
    ap.add_argument(
        "--out", type=Path, default=Path("./tak-qr"),
        help="Output directory (default ./tak-qr).",
    )
    ap.add_argument(
        "--itak-only", action="store_true",
        help="Only generate the iTAK server-connect QR (no per-user codes).",
    )
    args = ap.parse_args(argv)

    users: list[tuple[str, str]] = []
    if not args.itak_only:
        if args.users:
            users.extend(parse_users_file(args.users))
        users.extend(parse_inline_user(u) for u in args.user)
        if not users:
            ap.error("no users given. Use --users FILE, --user U:P, or --itak-only.")

    # Guard against silent duplicate filenames clobbering each other.
    seen: dict[str, str] = {}
    for username, _ in users:
        fname = f"{safe_filename(username)}.png"
        if fname in seen and seen[fname] != username:
            ap.error(
                f"users {seen[fname]!r} and {username!r} map to the same filename "
                f"{fname!r}; rename one."
            )
        seen[fname] = username

    args.out.mkdir(parents=True, exist_ok=True)

    atak_files: list[tuple[str, str]] = []
    for username, password in users:
        fname = f"{safe_filename(username)}.png"
        write_qr(atak_enrol_url(args.host, username, password), args.out / fname)
        atak_files.append((username, fname))
        print(f"  ATAK  {username:<16} -> {fname}")

    itak_file = "itak-server.png"
    write_qr(itak_connect_string(args.friendly_name, args.host), args.out / itak_file)
    print(f"  iTAK  {'(server connect)':<16} -> {itak_file}")

    sheet = build_contact_sheet(args.friendly_name, args.host, atak_files, itak_file)
    (args.out / "qr-sheet.html").write_text(sheet, encoding="utf-8")
    print(f"\nWrote {len(atak_files)} ATAK code(s) + 1 iTAK code + qr-sheet.html "
          f"to {args.out}/")
    print("These embed live credentials — do not commit them or post them publicly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
