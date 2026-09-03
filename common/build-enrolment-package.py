#!/usr/bin/env python3
"""build-enrolment-package.py - the data package a device needs before it can enrol.

A box that issues its own certificates presents a TLS certificate signed by a CA no
device has ever seen. ATAK then refuses with "the TAK server's identity could not be
verified", which is correct behaviour: we asked it to trust something we never gave it.
The QR alone carries host, username and password and no certificate authority at all, so
on a private build it can never complete.

This builds the thing that closes that gap: a TAK data package holding the box's own
truststore and a preference file pointing at this server. The device imports it once,
then the QR works.

    build-enrolment-package.py --host H --user U --pass P \\
        --truststore /opt/tak/certs/files/truststore-root.p12 --ca-pass X [--name N]

Writes the zip to stdout as base64, so it can travel back over the action channel.

It contains the truststore password and the user's password, because a TAK data package
has to. Treat it exactly like the credential it is.
"""
import argparse
import base64
import io
import os
import sys
import uuid
import zipfile
from xml.sax.saxutils import quoteattr

CERT_DIR = "certs"


def pref_xml(host, user, password, ca_entry, ca_pass, label):
    """ATAK reads cot_streams for the connection and the CA to trust it with.

    enrollForCertificateWithTrust is the flag that makes the device fetch its client
    certificate from the server after trusting the CA, which is what the QR then does.
    """
    def s(key, val):
        return (f'    <entry key={quoteattr(key)} class="class java.lang.String">'
                f'{quoteattr(str(val))[1:-1]}</entry>')

    def b(key, val):
        return (f'    <entry key={quoteattr(key)} class="class java.lang.Boolean">'
                f'{"true" if val else "false"}</entry>')

    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
        "<preferences>\n"
        '  <preference version="1" name="cot_streams">\n'
        '    <entry key="count" class="class java.lang.Integer">1</entry>\n'
        + s("description0", label) + "\n"
        + b("enabled0", True) + "\n"
        + s("connectString0", f"{host}:8089:ssl") + "\n"
        + s("caLocation0", f"cert/{ca_entry}") + "\n"
        + s("caPassword0", ca_pass) + "\n"
        + b("enrollForCertificateWithTrust0", True) + "\n"
        + b("useAuth0", True) + "\n"
        + s("username0", user) + "\n"
        + s("password0", password) + "\n"
        + s("cacheCreds0", "Cache credentials") + "\n"
        "  </preference>\n"
        "</preferences>\n")


def manifest_xml(uid, name, entries):
    body = "\n".join(f'    <Content ignore="false" zipEntry={quoteattr(e)}/>' for e in entries)
    return (
        '<MissionPackageManifest version="2">\n'
        "  <Configuration>\n"
        f'    <Parameter name="uid" value={quoteattr(uid)}/>\n'
        f'    <Parameter name="name" value={quoteattr(name)}/>\n'
        '    <Parameter name="onReceiveDelete" value="true"/>\n'
        "  </Configuration>\n"
        "  <Contents>\n" + body + "\n  </Contents>\n"
        "</MissionPackageManifest>\n")


def build(host, user, password, truststore, ca_pass, name=None):
    if not os.path.isfile(truststore):
        raise SystemExit(f"no truststore at {truststore}")
    with open(truststore, "rb") as fh:
        ts = fh.read()
    if not ts:
        raise SystemExit(f"{truststore} is empty")
    ca_entry = os.path.basename(truststore)
    label = name or host
    pkg_name = f"{label}-enrolment"
    uid = str(uuid.uuid4())
    pref = pref_xml(host, user, password, ca_entry, ca_pass, label)
    entries = [f"{CERT_DIR}/{ca_entry}", f"{CERT_DIR}/config.pref"]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("MANIFEST/manifest.xml", manifest_xml(uid, pkg_name, entries))
        z.writestr(entries[0], ts)
        z.writestr(entries[1], pref)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--pass", dest="password", required=True)
    ap.add_argument("--truststore", required=True)
    ap.add_argument("--ca-pass", required=True)
    ap.add_argument("--name")
    ap.add_argument("--out", help="write the zip here instead of base64 on stdout")
    a = ap.parse_args()
    blob = build(a.host, a.user, a.password, a.truststore, a.ca_pass, a.name)
    if a.out:
        with open(a.out, "wb") as fh:
            fh.write(blob)
    else:
        sys.stdout.write(base64.b64encode(blob).decode())
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
