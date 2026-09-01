#!/usr/bin/env bash
#
# build-uk-mbtiles.sh — build an offline GB basemap pack from OS VectorMap District.
#
# WHY THIS AND NOT THE OS MAPS API
# --------------------------------
# The OS Maps API (Outdoor/Road/Light, and the Explorer/Landranger raster) is
# PREMIUM data. Its terms cap caching at 24 hours and explicitly forbid End
# Users downloading, storing or extracting it. An .mbtiles file on a deployable
# kit is permanent stored extraction, so the API is not a lawful source for an
# offline pack, however convenient it would be.
#
# OS VectorMap District is OS OpenData under the Open Government Licence, which
# permits copying, adapting and redistribution. It ships as pre-rendered GeoTIFF,
# so there is no tile-scraping involved at all. That makes it the correct source
# for anything that leaves the network.
#
# Premium still has a place: it is right for ONLINE map sources on the cloud and
# NUC servers, where tiles are fetched live and cached transiently.
#
# RESOLUTION - READ THIS BEFORE ASKING FOR z18
# --------------------------------------------
# VectorMap District is 1:25 000-class cartography, natively about 2.5 m/px.
# At this latitude z16 is ~1.5 m/px, so z16 is already at or past native
# resolution. Building z17+ produces bigger files containing upscaled pixels and
# no more information. If you genuinely need sub-metre ground detail, that is
# imagery, not mapping, and it is a different (licensed) source.
#
# Usage:
#   bash common/build-uk-mbtiles.sh --src maps/src/vmdras_su.zip \
#        --bbox -1.65,51.10,-1.40,51.28 --maxzoom 16 --out maps/andover.mbtiles \
#        --name "Andover (OS VectorMap District)"
#
# MilUX, 2026-08-09.
set -euo pipefail

SRC=""; BBOX=""; OUT=""; NAME=""; MINZOOM=8; MAXZOOM=16
while [[ $# -gt 0 ]]; do
  case "$1" in
    --src) SRC="$2"; shift 2 ;;
    --bbox) BBOX="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --minzoom) MINZOOM="$2"; shift 2 ;;
    --maxzoom) MAXZOOM="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$SRC" && -n "$BBOX" && -n "$OUT" ]] || {
  echo "usage: $0 --src <zip> --bbox W,S,E,N --out <file.mbtiles> [--name N] [--maxzoom Z]" >&2
  exit 2; }
NAME="${NAME:-$(basename "$OUT" .mbtiles)}"

command -v gdalwarp >/dev/null || { echo "FAIL: GDAL not on PATH (brew install gdal)" >&2; exit 1; }

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

echo "  unpacking $(basename "$SRC") ..."
unzip -q -o "$SRC" -d "$WORK/src"
# NOT `mapfile` — that is a bash 4 builtin and macOS ships bash 3.2, where it
# fails with "command not found" (exit 127). Write the list to a file instead,
# which is what gdalbuildvrt wants anyway. Note the parentheses around the -o:
# without them the -iname branches do not both bind to the search path.
find "$WORK/src" \( -iname '*.tif' -o -iname '*.tiff' \) | sort > "$WORK/list.txt"
COUNT=$(wc -l < "$WORK/list.txt" | tr -d ' ')
[[ "$COUNT" -gt 0 ]] || { echo "FAIL: no GeoTIFFs inside $SRC" >&2; exit 1; }
FIRST=$(head -1 "$WORK/list.txt")
echo "  found $COUNT GeoTIFF tiles"

# VMD GeoTIFFs are British National Grid. Some OS raster products ship without
# the CRS embedded, relying on a .tfw world file, so assert it rather than hope.
if ! gdalinfo "$FIRST" 2>/dev/null | grep -qiE 'OSGB|27700'; then
    echo "  source has no embedded CRS - asserting EPSG:27700"
    ASSERT=(-a_srs EPSG:27700)
else
    ASSERT=()
fi

# `${ASSERT[@]+"${ASSERT[@]}"}` rather than plain `"${ASSERT[@]}"`: under `set -u`,
# bash 3.2 (which is what macOS ships) treats an EMPTY array expansion as an
# unbound variable and aborts. bash 4+ does not. The += guard makes it portable.
gdalbuildvrt -input_file_list "$WORK/list.txt" ${ASSERT[@]+"${ASSERT[@]}"} "$WORK/src.vrt" >/dev/null
echo "  built mosaic"

# Convert the WGS84 bbox to Web Mercator for the warp target extent.
read -r XMIN YMIN XMAX YMAX < <(python3 -c "
import math,sys
w,s,e,n = [float(v) for v in '$BBOX'.split(',')]
def x(lon): return lon*20037508.342789244/180.0
def y(lat): return math.log(math.tan((90.0+lat)*math.pi/360.0))/(math.pi/180.0)*20037508.342789244/180.0
print(x(w), y(s), x(e), y(n))
")

echo "  reprojecting 27700 -> 3857 and cropping ..."
gdalwarp -q -t_srs EPSG:3857 -te "$XMIN" "$YMIN" "$XMAX" "$YMAX" \
    -r bilinear -multi -co COMPRESS=DEFLATE \
    "$WORK/src.vrt" "$WORK/warped.tif"

echo "  writing mbtiles (z$MINZOOM-$MAXZOOM) ..."
rm -f "$OUT"
gdal_translate -q -of MBTILES "$WORK/warped.tif" "$OUT" \
    -co "TILE_FORMAT=PNG" -co "NAME=$NAME" \
    -co "ZOOM_LEVEL_STRATEGY=UPPER"

# Overviews are what give the lower zoom levels. Without them the pack only
# renders at its native zoom and looks blank when the operator zooms out.
LEVELS=""; f=2
for ((z=MAXZOOM-1; z>=MINZOOM; z--)); do LEVELS="$LEVELS $f"; f=$((f*2)); done
# shellcheck disable=SC2086
gdaladdo -q -r average "$OUT" $LEVELS 2>/dev/null || true

# OS licence clause 6.2 requires the copyright acknowledgement to travel with
# the data in a conspicuous position. The mbtiles metadata table is the right
# home: it survives copying, and clients can surface it. Doing it here means the
# obligation is met by the tool rather than by someone remembering.
YEAR=$(date +%Y)
python3 - "$OUT" "$YEAR" "$NAME" <<'PY'
import sqlite3, sys
out, year, name = sys.argv[1], sys.argv[2], sys.argv[3]
db = sqlite3.connect(out)
attrib = f"Contains OS data © Crown copyright and database right {year}. Licensed under the Open Government Licence v3.0."
vals = {
    "attribution": attrib,
    "description": f"{name}. Source: OS VectorMap District (OS OpenData). {attrib}",
    "licence": "Open Government Licence v3.0",
    "source": "OS VectorMap District, Backdrop Raster",
    # GDAL writes type=overlay, which tells a client to draw this transparently
    # over something else. It is the ground layer, so say so.
    "type": "baselayer",
}
# DELETE then INSERT, NOT `INSERT OR REPLACE`: the mbtiles metadata table has no
# unique index on `name`, so REPLACE has nothing to match against and silently
# appends a second row for the same key.
for k, v in vals.items():
    db.execute("DELETE FROM metadata WHERE name=?", (k,))
    db.execute("INSERT INTO metadata (name,value) VALUES (?,?)", (k, v))
db.commit(); db.close()
print("  attribution + baselayer type written to mbtiles metadata")
PY

echo
echo "=== result ==="
ls -lh "$OUT" | awk '{print "  size: "$5}'
python3 - "$OUT" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
try:
    rows = db.execute("select zoom_level, count(*) from tiles group by zoom_level order by zoom_level").fetchall()
    print(f"  {sum(c for _, c in rows)} tiles across {len(rows)} zoom levels")
    for z, c in rows:
        print(f"    z{z:<3} {c:>7} tiles")
except Exception as e:
    print("  could not read tiles table:", e)
PY
