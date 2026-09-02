#!/usr/bin/env python3
"""
build-mapsource-dp.py — make an ATAK data package that points devices at a
tile server.

THE GAP THIS FILLS
------------------
Putting .mbtiles on mbtileserver makes tiles available over HTTP. It does NOT
make them appear in ATAK. The client has no idea the server exists until it is
given a <customMapSource> definition. That definition is what this builds, one
per tileset, wrapped in a data package so it can be delivered through the same
mission route as everything else.

Symptom when it is missing: the mission contains a small markers package, the
markers appear, and the map stays blank. Which is exactly where we were.

Zoom bounds are read from each tileset's TileJSON rather than assumed, so a
source never claims detail it does not hold.

Usage:
    python3 common/build-mapsource-dp.py \\
        --server http://192.168.50.10:8080 \\
        --out maps/milux-mapsources-deployed-dp.zip \\
        --package-name "MilUX Map Sources (kit)"

MilUX, 2026-08-09.
"""

import argparse
import json
import urllib.request
import uuid
import zipfile


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def fetch(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def map_source_xml(name, url_tpl, minz, maxz):
    # ATAK placeholders are {$z}/{$x}/{$y}. mbtileserver serves XYZ (it applies
    # the TMS row flip itself), so no <tileUpdate>TMS</tileUpdate> is wanted -
    # setting that would invert the map.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<customMapSource>\n'
        f'  <name>{esc(name)}</name>\n'
        f'  <minZoom>{minz}</minZoom>\n'
        f'  <maxZoom>{maxz}</maxZoom>\n'
        '  <tileType>png</tileType>\n'
        '  <tileUpdate>None</tileUpdate>\n'
        f'  <url>{esc(url_tpl)}</url>\n'
        '  <backgroundColor>#000000</backgroundColor>\n'
        '</customMapSource>\n'
    )


def manifest(pkg_uid, name, files):
    entries = "\n".join(
        f'    <Content ignore="false" zipEntry="{f}"/>' for f in files)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<MissionPackageManifest version="2">\n'
        '  <Configuration>\n'
        f'    <Parameter name="uid" value="{pkg_uid}"/>\n'
        f'    <Parameter name="name" value="{esc(name)}"/>\n'
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
    ap.add_argument("--server", required=True,
                    help="base URL clients will use, e.g. http://192.168.50.10:8080")
    ap.add_argument("--query-via", default=None,
                    help="URL to query if different from --server (e.g. via ssh tunnel)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--package-name", default="MilUX Map Sources")
    ap.add_argument("--only", default=None,
                    help="comma-separated service ids to include (default: all)")
    args = ap.parse_args()

    base = args.server.rstrip("/")
    qbase = (args.query_via or args.server).rstrip("/")
    only = {s.strip() for s in args.only.split(",")} if args.only else None

    services = fetch(f"{qbase}/services")
    files = {}
    for s in services:
        sid = s["url"].rstrip("/").rsplit("/", 1)[-1]
        if only and sid not in only:
            continue
        # TileJSON carries the real zoom range for this tileset.
        try:
            tj = fetch(f"{qbase}/services/{sid}")
            minz, maxz = int(tj.get("minzoom", 0)), int(tj.get("maxzoom", 16))
        except Exception:
            minz, maxz = 0, 16
        name = s.get("name") or sid
        url = f"{base}/services/{sid}/tiles/{{$z}}/{{$x}}/{{$y}}.png"
        files[f"{sid}.xml"] = map_source_xml(name, url, minz, maxz)
        print(f"  {name:<46} z{minz}-{maxz}")

    if not files:
        raise SystemExit("no tilesets matched")

    pkg_uid = str(uuid.uuid4())
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as z:
        # MANIFEST/manifest.xml, in a subdirectory — NOT manifest.xml at the
        # root, which is the WinTAK enrolment-package rule and is wrong here.
        # Get it wrong and the package imports silently and does nothing.
        z.writestr("MANIFEST/manifest.xml",
                   manifest(pkg_uid, args.package_name, list(files)))
        for fn, body in files.items():
            z.writestr(fn, body)
    print(f"\n  wrote {args.out} ({len(files)} map sources)")


if __name__ == "__main__":
    main()
