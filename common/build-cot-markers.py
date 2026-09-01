#!/usr/bin/env python3
"""Generate ATAK CoT marker files for fixed points of interest.

ATAK imports one CoT event per .cot file, so this writes a file per marker
rather than trying to wrap several events in one document. Point them at
build-datapackage.py to ship them.

    ./build-cot-markers.py --out maps/markers \
        --marker "Days Inn the field|45.061564|-83.491301|Check-in 23 Aug"

Markers carry <archive/> so ATAK keeps them after a restart, and a stale time
far in the future so they do not grey out mid-deployment.
"""

import argparse
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

# b-m-p-s-m is ATAK's plain "spot map" marker: a fixed point of interest,
# not a unit. Using a unit type (a-f-G-...) would draw a friendly force icon
# and imply something that is not true.
COT_TYPE = "b-m-p-s-m"

TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<event version="2.0" uid="{uid}" type="{cot_type}" how="h-g-i-g-o" \
time="{now}" start="{now}" stale="{stale}">
  <point lat="{lat:.6f}" lon="{lon:.6f}" hae="0.0" ce="9999999.0" le="9999999.0"/>
  <detail>
    <contact callsign="{name}"/>
    <remarks>{remarks}</remarks>
    <archive/>
    <color argb="{argb}"/>
    <precisionlocation altsrc="DTED0"/>
  </detail>
</event>
"""


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--marker", action="append", required=True,
                   metavar="NAME|LAT|LON[|REMARKS]",
                   help="repeatable; pipe-separated")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--stale-days", type=int, default=365)
    # -16776961 is 0xFF0000FF, pure blue. Matt's house colour for these markers;
    # white (-1) disappears against pale imagery like concrete and car parks.
    p.add_argument("--argb", default="-16776961",
                   help="marker colour as signed 32-bit ARGB, default blue")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    stale = now + timedelta(days=args.stale_days)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"

    os.makedirs(args.out, exist_ok=True)
    written = []
    for spec in args.marker:
        parts = spec.split("|")
        if len(parts) < 3:
            sys.exit("bad --marker %r; need NAME|LAT|LON[|REMARKS]" % spec)
        name, lat, lon = parts[0].strip(), parts[1].strip(), parts[2].strip()
        remarks = parts[3].strip() if len(parts) > 3 else ""
        try:
            lat, lon = float(lat), float(lon)
        except ValueError:
            sys.exit("bad coordinates in %r" % spec)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            sys.exit("coordinates out of range in %r" % spec)

        path = os.path.join(args.out, "%s.cot" % slug(name))
        with open(path, "w") as f:
            f.write(TEMPLATE.format(
                uid=str(uuid.uuid4()), cot_type=COT_TYPE,
                now=now.strftime(fmt), stale=stale.strftime(fmt),
                lat=lat, lon=lon, name=esc(name), remarks=esc(remarks),
                argb=args.argb))
        written.append((name, lat, lon, path))

    for name, lat, lon, path in written:
        print("  %-38s %10.6f, %11.6f  ->  %s" % (name, lat, lon, path))
    print("\n%d marker(s) in %s" % (len(written), args.out))


if __name__ == "__main__":
    main()
