#!/usr/bin/env bash
# pins.sh - third-party versions the console itself pins, in the one shape every reader
# understands: the baseline (docs/THIRD-PARTY.md), the console at start, the fetch script and
# the pairing payload all read these lines. Moving a pin is a deliberate edit with a changelog
# note; the hash beside it is what the fetched package and the package on a box are checked
# against, so the two move together.
SYNCTHING_VER="v2.1.3"
SYNCTHING_FILE="syncthing-linux-amd64-v2.1.3.tar.gz"
SYNCTHING_SHA256="f929eb8e5b72a85543eeeefb2c38f34a68e0c530e70758a2905b78840c76602c"
