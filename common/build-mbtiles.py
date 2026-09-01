#!/usr/bin/env python3
"""Build an ATAK-compatible MBTiles pack from public-domain USGS imagery.

Zero dependencies beyond the Python standard library, so it runs on a bare NUC,
the deployable kit or a Mac with no GDAL and no QGIS.

Two source families, because they have different depth limits:

  usgs-imagery / usgs-topo   Pre-rendered National Map tile cache. Fast, but the
                             cache stops at z16 (~2.4 m/px) in most of CONUS.
  naip                       USGSNAIPPlus ImageServer, rendered per request. Slower,
                             but serves z17-z19 (down to ~0.3 m/px).
  auto                       usgs-imagery up to z16, naip above it. The default.

Both are US federal public domain. No API key, no licence restriction on caching,
which is the whole reason for preferring them over Google or Esri tiles.

Resumable: re-running against an existing file fills in only what is missing.

    ./build-mbtiles.py --bbox -83.50,45.01,-83.38,45.11 \
                       --minzoom 10 --maxzoom 18 \
                       --name "the field City" --out alpena-city.mbtiles
"""

import argparse
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "vantage-tak-mapbuilder/1.0"
EARTH = 20037508.342789244  # web mercator half-circumference, metres

CACHE_SOURCES = {
    "usgs-imagery": "https://basemap.nationalmap.gov/arcgis/rest/services/"
                    "USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
    "usgs-topo": "https://basemap.nationalmap.gov/arcgis/rest/services/"
                 "USGSTopo/MapServer/tile/{z}/{y}/{x}",
    "usgs-imagery-topo": "https://basemap.nationalmap.gov/arcgis/rest/services/"
                         "USGSImageryTopo/MapServer/tile/{z}/{y}/{x}",
}
NAIP_EXPORT = ("https://imagery.nationalmap.gov/arcgis/rest/services/"
               "USGSNAIPPlus/ImageServer/exportImage")

# Deepest zoom the pre-rendered National Map caches actually hold.
CACHE_MAX_Z = 16


def deg2tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_bbox_3857(x, y, z):
    """Web-mercator bounds of an XYZ tile, as (west, south, east, north)."""
    n = 2 ** z
    span = (2 * EARTH) / n
    west = -EARTH + x * span
    east = -EARTH + (x + 1) * span
    north = EARTH - y * span
    south = EARTH - (y + 1) * span
    return west, south, east, north


def tile_url(source, x, y, z):
    if source in CACHE_SOURCES:
        return CACHE_SOURCES[source].format(z=z, x=x, y=y)
    if source == "naip":
        w, s, e, n = tile_bbox_3857(x, y, z)
        q = urllib.parse.urlencode({
            "bbox": "%f,%f,%f,%f" % (w, s, e, n),
            "bboxSR": "3857", "imageSR": "3857",
            "size": "256,256", "format": "jpg", "f": "image",
        })
        return "%s?%s" % (NAIP_EXPORT, q)
    raise ValueError("unknown source: %s" % source)


def source_for_zoom(mode, z):
    if mode != "auto":
        return mode
    return "usgs-imagery" if z <= CACHE_MAX_Z else "naip"


def fetch(url, retries=4):
    """Return tile bytes, or None if the server has no data for this tile.

    A 404 is a legitimate 'nothing here' (edge of coverage, or past the cache
    depth) and must not abort the build. Anything else is retried.
    """
    delay = 1.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            # An ImageServer miss can come back as a tiny blank or an error page.
            if len(data) < 200:
                return None
            if not (data[:3] == b"\xff\xd8\xff" or data[:8].startswith(b"\x89PNG")):
                return None
            return data
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                return None
            if attempt == retries - 1:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
        time.sleep(delay)
        delay *= 2
    return None


def open_mbtiles(path, name, bounds, minzoom, maxzoom, fmt="jpg"):
    fresh = not os.path.exists(path)
    # Writes all happen under a single lock in the worker pool, so sharing one
    # connection across threads is safe. sqlite3 will not allow it otherwise.
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    if fresh:
        con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                    "tile_row INTEGER, tile_data BLOB)")
        con.execute("CREATE UNIQUE INDEX tile_index ON tiles "
                    "(zoom_level, tile_column, tile_row)")
    w, s, e, n = bounds
    meta = {
        "name": name,
        "type": "baselayer",
        "version": "1.1",
        "description": "Public domain USGS imagery. Built by the development repository.",
        "format": fmt,
        "bounds": "%.6f,%.6f,%.6f,%.6f" % (w, s, e, n),
        "center": "%.6f,%.6f,%d" % ((w + e) / 2, (s + n) / 2, maxzoom),
        "minzoom": str(minzoom),
        "maxzoom": str(maxzoom),
    }
    for k, v in meta.items():
        con.execute("DELETE FROM metadata WHERE name=?", (k,))
        con.execute("INSERT INTO metadata (name, value) VALUES (?,?)", (k, v))
    con.commit()
    return con


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bbox", required=True, help="W,S,E,N in decimal degrees")
    p.add_argument("--minzoom", type=int, default=8)
    p.add_argument("--maxzoom", type=int, default=18)
    p.add_argument("--out", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--source", default="auto",
                   choices=["auto", "naip"] + list(CACHE_SOURCES))
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--dry-run", action="store_true",
                   help="Count tiles and estimate size, fetch nothing")
    args = p.parse_args()

    try:
        w, s, e, n = [float(v) for v in args.bbox.split(",")]
    except ValueError:
        sys.exit("--bbox must be W,S,E,N in decimal degrees")
    if w >= e or s >= n:
        sys.exit("--bbox must be W,S,E,N with W<E and S<N")

    name = args.name or os.path.splitext(os.path.basename(args.out))[0]

    # Enumerate the whole job up front so progress and size are knowable.
    jobs = []
    for z in range(args.minzoom, args.maxzoom + 1):
        x0, y0 = deg2tile(n, w, z)   # north-west corner
        x1, y1 = deg2tile(s, e, z)   # south-east corner
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                jobs.append((z, x, y))

    per_zoom = {}
    for z, _, _ in jobs:
        per_zoom[z] = per_zoom.get(z, 0) + 1
    print("Tiles to build, %s:" % name)
    for z in sorted(per_zoom):
        print("  z%-2d  %8d  (%s)" % (z, per_zoom[z], source_for_zoom(args.source, z)))
    print("  %s  %8d total, ~%.0f MB at 15 KB/tile"
          % (" " * 3, len(jobs), len(jobs) * 15 / 1024))
    if args.dry_run:
        return

    con = open_mbtiles(args.out, name, (w, s, e, n), args.minzoom, args.maxzoom)
    have = set(con.execute("SELECT zoom_level, tile_column, tile_row FROM tiles"))
    todo = [(z, x, y) for (z, x, y) in jobs
            if (z, x, (2 ** z - 1) - y) not in have]
    print("%d already present, %d to fetch\n" % (len(jobs) - len(todo), len(todo)))
    if not todo:
        con.close()
        return

    lock = threading.Lock()
    state = {"done": 0, "bytes": 0, "missing": 0, "failed": 0}
    started = time.time()

    def work(job):
        z, x, y = job
        try:
            data = fetch(tile_url(source_for_zoom(args.source, z), x, y, z))
        except Exception:
            with lock:
                state["failed"] += 1
                state["done"] += 1
            return
        with lock:
            state["done"] += 1
            if data is None:
                state["missing"] += 1
            else:
                state["bytes"] += len(data)
                # MBTiles rows are TMS: y counts up from the south.
                con.execute(
                    "INSERT OR REPLACE INTO tiles "
                    "(zoom_level, tile_column, tile_row, tile_data) VALUES (?,?,?,?)",
                    (z, x, (2 ** z - 1) - y, sqlite3.Binary(data)))
                if state["done"] % 200 == 0:
                    con.commit()
            if state["done"] % 100 == 0 or state["done"] == len(todo):
                el = time.time() - started
                rate = state["done"] / el if el else 0
                eta = (len(todo) - state["done"]) / rate if rate else 0
                sys.stdout.write(
                    "\r  %d/%d  %.1f tiles/s  %.0f MB  %d empty  %d failed  ETA %dm%02ds  "
                    % (state["done"], len(todo), rate, state["bytes"] / 1048576,
                       state["missing"], state["failed"], eta // 60, eta % 60))
                sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        list(pool.map(work, todo))

    con.commit()
    stored = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    con.execute("VACUUM")
    con.close()
    size = os.path.getsize(args.out) / 1048576
    print("\n\n%s: %d tiles stored, %.1f MB" % (args.out, stored, size))
    if state["failed"]:
        print("WARNING: %d tiles failed after retries. Re-run to fill the gaps."
              % state["failed"])


if __name__ == "__main__":
    main()
