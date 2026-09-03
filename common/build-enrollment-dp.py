#!/usr/bin/env python3
"""Build a per-user TAK connection data package (ATAK / iTAK / WinTAK).

The import-once alternative to a QR code, and the only route that works on WinTAK:
a QR carries a `tak://` URL that only Android registers a handler for, so a Windows
client has nothing to hand it to, camera or no camera.

    manifest.xml      lowercase, at the root
    server.pref       connection and certificate settings
    caCert.p12        truststore
    clientCert.p12    the issued client certificate (--client-cert only)

THE FORMAT MATTERS AND IS NOT THE ATAK ENROLMENT FORMAT.
--------------------------------------------------------
Certificate settings go in the `com.atakmap.app_preferences` block with NO index
suffix: caLocation, caPassword, clientPassword, certificateLocation. Only the
connection (count, description0, enabled0, connectString0) belongs in cot_streams.

Writing them as caLocation0 / certificateLocation0 inside cot_streams — which is how
an ATAK *enrolment* package looks — means WinTAK never finds them. It connects with
no client certificate and the server logs PEER_DID_NOT_RETURN_A_CERTIFICATE, while
the client appears connected and can still send. That cost a full day on 2026-08-08.

This layout is copied from a MilUX package of March 2024 that was in production use.
See TROUBLESHOOTING-wintak.md before changing anything here.

    # WinTAK: issue the cert on the server first, then ship it
    ssh root@<server> 'cd /opt/tak/certs && ./makeCert.sh client Surface-Pro'
    ./build-enrollment-dp.py \
        --host tak.example.com --description "MilUX TAK (Deployed)" \
        --username Surface-Pro \
        --ca caCert.p12 --ca-password 'CERT_PASSWORD' \
        --client-cert Surface-Pro.p12 \
        --out ../secrets/deployed/Surface-Pro.zip

Channels: a hard-certificate client never shows channels, by design. That is normal
and is NOT required for messaging. Channels need certificate auto-enrolment.

SECURITY: the output embeds live credentials in plain text — a client certificate and
the password that opens it. Treat every package as a credential. Write it only into
the gitignored secrets/ tree, and when a device is retired disable the account *and*
revoke its certificate.
"""
from __future__ import annotations

import argparse
import sys
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

# The client copies bundled certificates into a `cert/` folder beside its config,
# so the path recorded in the preference is not the path inside the zip.
CERT_ENTRY = "caCert.p12"
CERT_RUNTIME_PATH = "cert/caCert.p12"
CLIENT_ENTRY = "clientCert.p12"
CLIENT_RUNTIME_PATH = "cert/clientCert.p12"

# The manifest sits at the TOP LEVEL as MANIFEST.xml, not at MANIFEST/manifest.xml.
#
# MANIFEST/manifest.xml is the general mission-package convention and ATAK accepts
# it, but TAK Server's own enrolment packages use the top-level form and WinTAK
# only reliably finds it there. A WinTAK import of the nested form fails silently:
# no error, no server in the list, and nothing at all in the server logs because
# the client never dials out. Do not "tidy" this into a folder.
MANIFEST_ENTRY = "manifest.xml"

MANIFEST = """<MissionPackageManifest version="2">
<Configuration>
    <Parameter name="uid" value={uid}/>
    <Parameter name="name" value={name}/>
    <Parameter name="onReceiveDelete" value="true"/>
</Configuration>
<Contents>
{contents}</Contents>
</MissionPackageManifest>
"""


def build_pref(host: str, port: str, description: str, username: str,
               password: str, ca_password: str, team: str, role: str,
               client_password: str | None) -> str:
    """The cot_streams preference block the client reads on import.

    Two modes, and the choice matters:

    Enrolment (no client certificate given). The package carries trust only, and
    the client authenticates with the username and password to be issued its own
    certificate. This is what ATAK does after a QR scan. **WinTAK does not
    reliably do this from a data package** — it imports the server entry, then
    goes straight at the mutual-TLS streaming port with no client certificate and
    the server rejects it with "peer not verified", never touching the enrolment
    port at all.

    Client certificate (the --client-cert path). The package carries the issued
    certificate, so there is no enrolment step and nothing for the client to get
    wrong. Use this for WinTAK, and for any client where enrolment misbehaves.

    Entry keys are suffixed with the stream index (0 here, one server per
    package). Values are XML text, so anything user-supplied is escaped.
    """
    e = escape
    if client_password is None:
        # Enrolment: trust only, credentials, no client certificate yet.
        auth = f"""    <entry key="caLocation" class="class java.lang.String">{CERT_RUNTIME_PATH}</entry>
    <entry key="caPassword" class="class java.lang.String">{e(ca_password)}</entry>
    <entry key="enrollForCertificateWithTrust" class="class java.lang.Boolean">true</entry>
    <entry key="useAuth" class="class java.lang.Boolean">true</entry>
    <entry key="username" class="class java.lang.String">{e(username)}</entry>
    <entry key="password" class="class java.lang.String">{e(password)}</entry>"""
    else:
        # Client certificate. Key names and placement copied verbatim from the
        # 2024 ULOTC package that demonstrably worked on this estate.
        auth = f"""    <entry key="caLocation" class="class java.lang.String">{CERT_RUNTIME_PATH}</entry>
    <entry key="caPassword" class="class java.lang.String">{e(ca_password)}</entry>
    <entry key="clientPassword" class="class java.lang.String">{e(client_password)}</entry>
    <entry key="certificateLocation" class="class java.lang.String">{CLIENT_RUNTIME_PATH}</entry>"""
    return f"""<?xml version='1.0' encoding='ASCII' standalone='yes'?>
<preferences>
<preference version="1" name="cot_streams">
    <entry key="count" class="class java.lang.Integer">1</entry>
    <entry key="description0" class="class java.lang.String">{e(description)}</entry>
    <entry key="enabled0" class="class java.lang.Boolean">true</entry>
    <entry key="connectString0" class="class java.lang.String">{e(host)}:{e(port)}:ssl</entry>
</preference>
<preference version="1" name="com.atakmap.app_preferences">
    <entry key="displayServerConnectionWidget" class="class java.lang.Boolean">true</entry>
{auth}
</preference>
</preferences>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--host", required=True, help="Server FQDN")
    ap.add_argument("--port", default="8089", help="Streaming port (default 8089)")
    ap.add_argument("--description", required=True,
                    help="Server name shown in the client's server list")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", default="",
                    help="Account password. Required unless --client-cert is given.")
    ap.add_argument("--ca", type=Path, required=True,
                    help="Truststore .p12 holding the server's CA chain")
    ap.add_argument("--ca-password", required=True,
                    help="Password for the truststore (the server's CERT_PASSWORD)")
    ap.add_argument("--client-cert", type=Path, default=None,
                    help="Issued client .p12 (from 'makeCert.sh client <user>'). "
                         "Ships the certificate instead of enrolling for one. "
                         "Required for WinTAK, which does not enrol from a package.")
    ap.add_argument("--client-password", default=None,
                    help="Password for --client-cert (defaults to --ca-password, "
                         "which is what makeCert.sh uses).")
    ap.add_argument("--team", default="Blue")
    ap.add_argument("--role", default="Team Member")
    ap.add_argument("--name", default=None,
                    help="Package name shown on import (default: the zip filename)")
    ap.add_argument("--uid", default=None, help="Stable UID (default: random)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    if not args.ca.is_file():
        sys.exit(f"truststore not found: {args.ca}")

    client_password = None
    if args.client_cert:
        if not args.client_cert.is_file():
            sys.exit(f"client certificate not found: {args.client_cert}")
        # makeCert.sh protects the client .p12 with the server's CERT_PASSWORD,
        # the same value that opens the truststore, so default to it.
        client_password = args.client_password or args.ca_password
    elif not args.password:
        sys.exit("--password is required unless --client-cert is given")

    pref = build_pref(args.host, args.port, args.description, args.username,
                      args.password, args.ca_password, args.team, args.role,
                      client_password)

    entries = [CERT_ENTRY] + ([CLIENT_ENTRY] if args.client_cert else [])
    contents = '    <Content ignore="false" zipEntry="server.pref"/>\n' + "".join(
        f'    <Content ignore="false" zipEntry="{e}"/>\n' for e in entries)
    manifest = MANIFEST.format(
        uid=quoteattr(args.uid or str(uuid.uuid4())),
        name=quoteattr(args.name or args.out.name),
        contents=contents,
    )

    # Entry order mirrors a server-generated package: certificates, prefs, manifest.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(args.ca, CERT_ENTRY)
        if args.client_cert:
            z.write(args.client_cert, CLIENT_ENTRY)
        z.writestr("server.pref", pref)
        z.writestr(MANIFEST_ENTRY, manifest)

    # The package is a credential; keep it off other accounts on this machine.
    args.out.chmod(0o600)
    mode = "client certificate" if args.client_cert else "enrolment"
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes, {mode}) "
          f"for {args.username}@{args.host}:{args.port}")
    print("This embeds a live credential — keep it in secrets/, never commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
