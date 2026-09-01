#!/usr/bin/env python3
"""Wrap files into a TAK data package (mission package) zip.

A data package is just a zip with a MANIFEST/manifest.xml describing its
contents. ATAK and iTAK both import it, which makes it the only distribution
route that works for iOS as well as Android.

    ./build-datapackage.py --name "the field City Maps" \
                           --out maps/alpena-city-dp.zip \
                           maps/alpena-city.mbtiles

Size matters: TAK Server's uploadSizeLimit in CoreConfig.xml defaults to 400 MB.
Anything larger has to be split, or sideloaded over USB instead.
"""

import argparse
import os
import sys
import uuid
import zipfile

MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="{uid}"/>
    <Parameter name="name" value="{name}"/>
    <Parameter name="onReceiveDelete" value="false"/>
  </Configuration>
  <Contents>
{contents}  </Contents>
</MissionPackageManifest>
"""


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help="files to include")
    p.add_argument("--name", required=True, help="package name shown in ATAK")
    p.add_argument("--out", required=True)
    p.add_argument("--uid", default=None, help="stable UID (default: random)")
    args = p.parse_args()

    for f in args.files:
        if not os.path.isfile(f):
            sys.exit("not a file: %s" % f)

    uid = args.uid or str(uuid.uuid4())
    entries = [os.path.basename(f) for f in args.files]
    contents = "".join(
        '    <Content ignore="false" zipEntry="%s"/>\n' % e for e in entries)
    manifest = MANIFEST.format(uid=uid, name=args.name, contents=contents)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    # MBTiles are already-compressed JPEG; storing rather than deflating saves
    # minutes on a multi-hundred-MB pack for no meaningful size gain.
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        z.writestr("MANIFEST/manifest.xml", manifest)
        for f, e in zip(args.files, entries):
            z.write(f, e)

    size = os.path.getsize(args.out) / 1048576
    print("%s  %.1f MB  uid=%s" % (args.out, size, uid))
    for e in entries:
        print("  %s" % e)
    if size > 400:
        print("\nWARNING: larger than TAK Server's default 400 MB uploadSizeLimit.\n"
              "Either raise that limit or sideload this over USB instead.")


if __name__ == "__main__":
    main()
