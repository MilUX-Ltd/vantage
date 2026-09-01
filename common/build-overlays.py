#!/usr/bin/env python3
"""build-overlays.py - turn declared shapes into ATAK overlays, and package them.

THE GAP THIS FILLS
------------------
build-markers.py puts POINTS on the map. Almost nothing a planner draws is a point.
An area of operations, a boundary, a route, a phase line, an exclusion zone and an
ERV radius are lines and areas, and until this existed there was no way to get any
of them out of a plan and onto a device except by drawing them again by hand on the
day, which is where they diverge from the plan nobody has time to re-check.

Input is the same shape as build-markers.py's: a JSON list, one object per overlay,
with the provenance kept alongside the geometry. Output is one .cot per overlay and,
optionally, a data package that publish-to-mission.sh can put in front of devices.

    ./build-overlays.py --in vault/AO.json --outdir out/overlays \
        --package out/ao-overlays.zip --package-name "AO ALPHA"

Input format
------------
    [
      {"name": "AO ALPHA", "shape": "area", "colour": "amber",
       "points": [[51.20, -1.40], [51.22, -1.40], [51.22, -1.36]],
       "remarks": "...", "source": "OpO Annex B fig 3"},

      {"name": "ROUTE BLUE", "shape": "route", "colour": "blue",
       "points": [[51.19, -1.41], [51.21, -1.38]]},

      {"name": "ERV 1", "shape": "circle", "colour": "green",
       "centre": [51.205, -1.395], "radius_m": 250}
    ]

`source` matters as much here as it does for markers. A boundary lifted off a
photographed briefing slide and a boundary from a grid in the written order are
worth different amounts to whoever is standing on it, and the only place that
difference survives is in the data.

VERIFICATION STATUS
-------------------
The CoT written here follows ATAK's documented drawing-shape conventions and is
checked structurally by console/test-overlays.py. It has NOT yet been imported on a
device. Until someone does that and says so, treat "ATAK renders this" as unproven:
the whole point of the mission-pack skill is that a package which imports cleanly and
then behaves wrongly is the expensive failure, and saying otherwise here would be the
same mistake in the documentation.

MilUX, 2026-09-01.
"""

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _sibling(name, path):
    """Load a sibling builder as a module, so its tables are used and never restated.

    The colour palette and the data-package manifest already exist in build-markers.py.
    Copying them here would give the estate two of each, free to disagree; an overlay
    drawn 'milux-gold' and a marker drawn 'milux-gold' must be the same gold.
    """
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_markers = _sibling("_markers", "build-markers.py")
COLOURS = _markers.COLOURS
argb = _markers.argb
esc = _markers.esc
manifest = _markers.manifest

SHAPES = ("area", "route", "line", "circle")
# ATAK's drawing types: a closed or open polyline is u-d-f, a circle is u-d-c-c.
TYPE_FOR = {"area": "u-d-f", "route": "u-d-f", "line": "u-d-f", "circle": "u-d-c-c"}


def validate(ov, i):
    """Everything wrong with one overlay, as a list. Empty means it is usable.

    Collected rather than raised on the first fault: a planner fixing a file wants to
    see all of it, not to run the tool six times.
    """
    bad = []
    where = f"[{i}] {ov.get('name', '<unnamed>')!r}"
    if not str(ov.get("name", "")).strip():
        bad.append(f"{where}: needs a name, it becomes the callsign on the map")
    shape = ov.get("shape")
    if shape not in SHAPES:
        bad.append(f"{where}: shape must be one of {', '.join(SHAPES)}, got {shape!r}")
        return bad
    if ov.get("colour") and ov["colour"] not in COLOURS:
        bad.append(f"{where}: colour {ov['colour']!r} is not one of "
                   f"{', '.join(sorted(COLOURS))}")

    def _pt(p, label):
        try:
            lat, lon = float(p[0]), float(p[1])
        except Exception:
            bad.append(f"{where}: {label} is not a [lat, lon] pair: {p!r}")
            return
        if not -90 <= lat <= 90:
            bad.append(f"{where}: {label} latitude {lat} is outside -90..90")
        if not -180 <= lon <= 180:
            bad.append(f"{where}: {label} longitude {lon} is outside -180..180")
        # A plan drafted in the northern hemisphere with a sign slip lands in the ocean
        # off Africa. Not an error, but worth saying out loud.
        if lat == 0 and lon == 0:
            bad.append(f"{where}: {label} is 0,0 - almost always a missing coordinate")

    if shape == "circle":
        c = ov.get("centre") or ov.get("center")
        if not c:
            bad.append(f"{where}: a circle needs a centre [lat, lon]")
        else:
            _pt(c, "centre")
        try:
            r = float(ov.get("radius_m", 0))
            if r <= 0:
                bad.append(f"{where}: radius_m must be greater than 0")
        except Exception:
            bad.append(f"{where}: radius_m is not a number: {ov.get('radius_m')!r}")
    else:
        pts = ov.get("points") or []
        least = 3 if shape == "area" else 2
        if len(pts) < least:
            bad.append(f"{where}: a {shape} needs at least {least} points, got {len(pts)}")
        for n, p in enumerate(pts):
            _pt(p, f"point {n}")
    return bad


def centroid(points):
    """The mean of the vertices, for the event's own <point>.

    ATAK puts the marker for a shape here, so it decides where the map jumps to when
    someone taps the overlay in a list. The mean is good enough for that and is stable;
    a true polygon centroid is not worth the arithmetic and goes wrong on self-crossing
    shapes, which hand-entered plans do contain.
    """
    lat = sum(float(p[0]) for p in points) / len(points)
    lon = sum(float(p[1]) for p in points) / len(points)
    return lat, lon


def cot(ov, stale_days=365):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    stale = now + dt.timedelta(days=stale_days)
    f = lambda t: t.isoformat().replace("+00:00", "") + "Z"

    shape = ov["shape"]
    colour = argb(ov.get("colour", "blue"))
    remarks = ov.get("remarks", "")
    if ov.get("source"):
        remarks = (remarks + "  " if remarks else "") + f"[source: {ov['source']}]"

    if shape == "circle":
        c = ov.get("centre") or ov.get("center")
        lat, lon = float(c[0]), float(c[1])
        r = float(ov["radius_m"])
        geometry = (f'      <ellipse major="{r}" minor="{r}" angle="360"/>\n')
    else:
        pts = [(float(p[0]), float(p[1])) for p in ov["points"]]
        lat, lon = centroid(pts)
        closed = "true" if shape == "area" else "false"
        verts = "".join(f'        <vertex lat="{a}" lon="{b}"/>\n' for a, b in pts)
        geometry = (f'      <polyline closed="{closed}">\n{verts}      </polyline>\n')

    # Areas get a fill; a route or a line with a fill is unreadable on a moving map.
    fill = (f'    <fillColor value="{(colour & 0x00FFFFFF) - (1 << 32) + (0x4C << 24)}"/>\n'
            if shape == "area" else "")

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<event version="2.0" uid="{ov.get("uid") or uuid.uuid4()}" '
        f'type="{TYPE_FOR[shape]}" how="h-e" '
        f'time="{f(now)}" start="{f(now)}" stale="{f(stale)}">\n'
        f'  <point lat="{lat}" lon="{lon}" hae="0.0" '
        'ce="9999999.0" le="9999999.0"/>\n'
        '  <detail>\n'
        '    <shape>\n'
        f'{geometry}'
        '    </shape>\n'
        f'    <strokeColor value="{colour}"/>\n'
        '    <strokeWeight value="4.0"/>\n'
        f'{fill}'
        f'    <contact callsign="{esc(ov["name"])}"/>\n'
        f'    <remarks>{esc(remarks)}</remarks>\n'
        '    <archive/>\n'
        '    <labels_on value="true"/>\n'
        '  </detail>\n'
        '</event>\n'
    )


def slug_for(ov):
    return (ov.get("slug") or "".join(
        c if c.isalnum() else "-" for c in ov["name"].lower()).strip("-")) or "overlay"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--outdir", help="where the .cot files go (omit with --check)")
    ap.add_argument("--package", help="also build a data package zip")
    ap.add_argument("--package-name", default="MilUX Overlays")
    ap.add_argument("--check", action="store_true",
                    help="validate the input and write nothing")
    args = ap.parse_args()

    overlays = json.load(open(args.infile))
    if not isinstance(overlays, list):
        raise SystemExit("ERROR: the input must be a JSON list of overlays")

    problems = []
    for i, ov in enumerate(overlays):
        problems += validate(ov, i)
    if problems:
        for p in problems:
            print("ERROR " + p)
        raise SystemExit(2)
    if args.check:
        print(f"ok: {len(overlays)} overlays, nothing written")
        return
    if not args.outdir:
        raise SystemExit("ERROR: --outdir is required unless you pass --check")

    os.makedirs(args.outdir, exist_ok=True)
    written = []
    for ov in overlays:
        path = os.path.join(args.outdir, slug_for(ov) + ".cot")
        with open(path, "w") as fh:
            fh.write(cot(ov))
        written.append(path)
        n = (1 if ov["shape"] == "circle" else len(ov["points"]))
        print(f"  {ov['name']:<28} {ov['shape']:<7} {n} point(s)"
              f"{'  source: ' + ov['source'] if ov.get('source') else ''}")

    if args.package:
        names = [os.path.basename(p) for p in written]
        with zipfile.ZipFile(args.package, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("MANIFEST/manifest.xml",
                       manifest(str(uuid.uuid4()), args.package_name, names))
            for p in written:
                z.write(p, os.path.basename(p))
        print(f"\npackage: {args.package} ({len(written)} overlays)")
        # Deliberately does not name publish-to-mission.sh: that script is estate-only and
        # is not in a release, and telling someone to run a file they do not have is the
        # exact fault the shipped skills exist to avoid.
        print("put it on the console's mission-packs shelf, or attach it to a TAK mission "
              "so subscribed devices pull it on their own")


if __name__ == "__main__":
    main()
