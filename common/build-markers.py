#!/usr/bin/env python3
"""
build-markers.py — turn a list of places into TAK CoT markers and a data package.

Part of the pre-deployment mission pack: maps give you the ground, markers give
you the things that matter on it.

Reads a simple JSON list, writes one .cot per location plus a data package ready
to publish to a TAK Server mission.

WHY ce MATTERS HERE
-------------------
`ce` is the circular error in metres, and ATAK draws it as an accuracy ring.
Most tooling dumps 9999999.0 (unknown) into it and moves on. We do not, because
these markers come from sources of genuinely different precision:

    a surveyed grid                     ce ~10
    a geocoded street address           ce ~50
    a UK postcode centroid              ce ~100   (the centroid, not the building)
    a site or camp centroid             ce ~250
    a point inferred off a briefing map ce ~500

Encoding that honestly means an operator can SEE which points are precise. The
Ex-SENS pack made the same distinction in prose ("ERVs exact; tac pts inferred");
this puts it in the data where it cannot be lost.

Usage:
    python3 common/build-markers.py --in maps/markers/andover.json \\
        --outdir maps/markers --package maps/milux-andover-markers-dp.zip

Input format:
    [{"callsign": "...", "lat": 51.2, "lon": -1.4, "ce": 100,
      "remarks": "...", "colour": "red", "source": "how we got this"}]

MilUX, 2026-08-09.
"""

import argparse
import datetime as dt
import json
import os
import uuid
import zipfile

# Signed 32-bit ARGB, which is what ATAK expects.
COLOURS = {
    "red":       0xFFFF0000,
    "amber":     0xFFFFBF00,
    "yellow":    0xFFFFFF00,
    "green":     0xFF00FF00,
    "blue":      0xFF0000FF,
    "cyan":      0xFF00FFFF,
    "white":     0xFFFFFFFF,
    # MilUX brand palette, from Context/Brand.md
    "milux-green": 0xFF113308,
    "milux-gold":  0xFFB5B171,
}


def argb(name):
    v = COLOURS.get(name, COLOURS["blue"])
    return v - (1 << 32) if v >= (1 << 31) else v


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def cot(loc, stale_days=365):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    stale = now + dt.timedelta(days=stale_days)
    f = lambda t: t.isoformat().replace("+00:00", "") + "Z"

    remarks = loc.get("remarks", "")
    # Keep provenance with the point. A marker whose origin is unknown is a
    # marker nobody can decide whether to trust.
    if loc.get("source"):
        remarks = (remarks + "  " if remarks else "") + f"[source: {loc['source']}]"

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<event version="2.0" uid="{loc.get("uid") or uuid.uuid4()}" '
        f'type="{loc.get("type", "b-m-p-s-m")}" how="h-g-i-g-o" '
        f'time="{f(now)}" start="{f(now)}" stale="{f(stale)}">\n'
        f'  <point lat="{loc["lat"]}" lon="{loc["lon"]}" hae="0.0" '
        f'ce="{float(loc.get("ce", 9999999.0))}" le="9999999.0"/>\n'
        '  <detail>\n'
        f'    <contact callsign="{esc(loc["callsign"])}"/>\n'
        f'    <remarks>{esc(remarks)}</remarks>\n'
        '    <archive/>\n'
        f'    <color argb="{argb(loc.get("colour", "blue"))}"/>\n'
        '    <precisionlocation altsrc="DTED0"/>\n'
        '  </detail>\n'
        '</event>\n'
    )


def manifest(pkg_uid, name, files):
    entries = "\n".join(
        f'    <Content ignore="false" zipEntry="{f}"/>' for f in files)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<MissionPackageManifest version="2">\n'
        '  <Configuration>\n'
        f'    <Parameter name="uid" value="{pkg_uid}"/>\n'
        f'    <Parameter name="name" value="{name}"/>\n'
        '    <Parameter name="onReceiveImport" value="true"/>\n'
        '    <Parameter name="onReceiveDelete" value="false"/>\n'
        '  </Configuration>\n'
        '  <Contents>\n'
        f'{entries}\n'
        '  </Contents>\n'
        '</MissionPackageManifest>\n'
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--package", help="also build a data package zip")
    ap.add_argument("--package-name", default="MilUX Markers")
    args = ap.parse_args()

    locs = json.load(open(args.infile))
    os.makedirs(args.outdir, exist_ok=True)

    written = []
    for loc in locs:
        slug = loc.get("slug") or "".join(
            c if c.isalnum() else "-" for c in loc["callsign"].lower()).strip("-")
        path = os.path.join(args.outdir, f"{slug}.cot")
        with open(path, "w") as fh:
            fh.write(cot(loc))
        written.append(path)
        print(f"  {loc['callsign']:<34} {loc['lat']:>10.6f},{loc['lon']:<11.6f} "
              f"ce={loc.get('ce', '?')}m")

    if args.package:
        pkg_uid = str(uuid.uuid4())
        names = [os.path.basename(p) for p in written]
        with zipfile.ZipFile(args.package, "w", zipfile.ZIP_DEFLATED) as z:
            # MANIFEST/manifest.xml — in a SUBDIRECTORY, capitalised.
            #
            # This is the OPPOSITE of the WinTAK enrolment package, which needs
            # manifest.xml lowercase at the ROOT. Two package types, two rules,
            # and applying the enrolment rule here produces a package that
            # imports without error and puts nothing on the map.
            # Verified against a known-working package (Ex-SENS Control
            # Measures, cloud server) on 2026-08-09.
            z.writestr("MANIFEST/manifest.xml",
                       manifest(pkg_uid, args.package_name, names))
            for p, n in zip(written, names):
                z.write(p, n)
        size = os.path.getsize(args.package)
        print(f"\n  package: {args.package} ({size} bytes, {len(names)} markers)")


if __name__ == "__main__":
    main()
