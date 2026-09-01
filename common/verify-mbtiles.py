#!/usr/bin/env python3
"""Check an MBTiles pack is complete and ATAK-readable before it ships.

The failure that costs you a deployment is a pack that looks fine on the bench
and turns out to have holes in the middle of the AO. This compares what is
actually stored against what the declared bounds imply, per zoom level, and
sanity-checks the image data and the TMS row convention.

    ./verify-mbtiles.py maps/alpena-city.mbtiles
"""

import math
import os
import sqlite3
import sys


def deg2tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: verify-mbtiles.py FILE.mbtiles")
    path = sys.argv[1]
    if not os.path.isfile(path):
        sys.exit("no such file: %s" % path)

    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    meta = dict(con.execute("SELECT name, value FROM metadata"))

    print("%s  (%.1f MB)" % (path, os.path.getsize(path) / 1048576))
    print()
    problems = []

    for key in ("name", "format", "bounds", "minzoom", "maxzoom"):
        val = meta.get(key)
        print("  %-10s %s" % (key, val if val is not None else "MISSING"))
        if val is None:
            problems.append("metadata '%s' missing; ATAK may refuse the layer" % key)
    print()

    if meta.get("format") not in ("jpg", "jpeg", "png"):
        problems.append("format is %r; ATAK expects jpg or png" % meta.get("format"))

    try:
        w, s, e, n = [float(v) for v in meta["bounds"].split(",")]
        minz, maxz = int(meta["minzoom"]), int(meta["maxzoom"])
    except Exception:
        print("Cannot parse bounds/zooms; skipping coverage check.")
        con.close()
        sys.exit(1)

    print("  %-5s %9s %9s %7s" % ("zoom", "stored", "expected", "cover"))
    total_missing = 0
    for z in range(minz, maxz + 1):
        x0, y0 = deg2tile(n, w, z)
        x1, y1 = deg2tile(s, e, z)
        expected = (x1 - x0 + 1) * (y1 - y0 + 1)
        stored = con.execute(
            "SELECT COUNT(*) FROM tiles WHERE zoom_level=?", (z,)).fetchone()[0]
        pct = (stored / expected * 100) if expected else 0
        flag = "" if pct >= 99.5 else "  <-- holes"
        print("  z%-4d %9d %9d %6.1f%%%s" % (z, stored, expected, pct, flag))
        if pct < 99.5:
            total_missing += expected - stored

    # The classic silent killer: rows written in XYZ order rather than TMS, which
    # puts the imagery in the wrong hemisphere of the tile grid.
    row = con.execute("SELECT zoom_level, tile_column, tile_row FROM tiles "
                      "WHERE zoom_level=? LIMIT 1", (maxz,)).fetchone()
    if row:
        z, x, tms_y = row
        xyz_y = (2 ** z - 1) - tms_y
        lat_top = math.degrees(math.atan(math.sinh(
            math.pi * (1 - 2 * xyz_y / (2 ** z)))))
        lon_left = x / (2 ** z) * 360.0 - 180.0
        inside = (w - 0.05 <= lon_left <= e + 0.05) and (s - 0.05 <= lat_top <= n + 0.05)
        print("\n  row convention: sample z%d tile maps to %.4f, %.4f  -> %s"
              % (z, lat_top, lon_left, "inside bounds" if inside else "OUTSIDE BOUNDS"))
        if not inside:
            problems.append("tile_row looks like XYZ, not TMS; imagery will be flipped")

    bad = 0
    for (blob,) in con.execute("SELECT tile_data FROM tiles ORDER BY RANDOM() LIMIT 50"):
        if not (bytes(blob[:3]) == b"\xff\xd8\xff" or bytes(blob[:4]) == b"\x89PNG"):
            bad += 1
    print("  image sanity: %d of 50 sampled tiles are not valid JPEG/PNG" % bad)
    if bad:
        problems.append("%d/50 sampled tiles are not valid images" % bad)

    con.close()
    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  - %s" % p)
        sys.exit(1)
    if total_missing:
        print("Complete enough to ship, but %d tiles are missing. "
              "Re-run build-mbtiles.py to fill gaps." % total_missing)
        sys.exit(0)
    print("OK. Complete, correctly oriented, and ATAK-readable.")


if __name__ == "__main__":
    main()
