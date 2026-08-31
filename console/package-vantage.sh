#!/usr/bin/env bash
# package-vantage.sh - build the Vantage product tarball from the EXPLICIT ship-list.
#
# The repo's working tree carries far more than the product: our estate's agent (Sam),
# go-live runbooks, kit-build install files, vendored checkouts, caches - and, on disk
# though never in git, TAK Server releases. None of that ships. TAK software NEVER
# ships: releases enter an estate only as a deliberate operator upload into the
# console's Store. This manifest is also the seed list for the eventual product repo
# split (card 6174).
#
#   ./package-vantage.sh [out-dir]      -> vantage-<version>.tgz
set -euo pipefail
die() { echo "ERROR: $*" >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/..}"

VER=$(grep -m1 '^VERSION = ' "$HERE/vantage-console-serve.py" | sed 's/[^0-9.]*//g')
[[ -n "$VER" ]] || die "could not read VERSION"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/vantage/console/actions" "$STAGE/vantage/console/provision" \
         "$STAGE/vantage/console/systemd" "$STAGE/vantage/common"

# ---- the ship-list --------------------------------------------------------------------
CONSOLE_FILES=(
    vantage-console-serve.py
    vantage-console-collect.py
    console-enrol-server.sh
    install-vantage.sh
    install-deployed-console.sh
    package-vantage.sh
    targets.example.json
    desired.example.json
)
SYSTEMD_FILES=(
    vantage-console-deployed.service
)
PROVISION_FILES=(
    vantage-tak-provision.sh
    vantage-infratak-provision.sh
    vantage-mesh-gateway-install.sh
)
# every tak-* action pair, and the setup helper - but NOTHING else from actions/
# (go-live runbooks and READMEs are ours, not the product's)

for f in "${CONSOLE_FILES[@]}"; do
    [[ -r "$HERE/$f" ]] || die "ship-list file missing: console/$f"
    install -m 0755 "$HERE/$f" "$STAGE/vantage/console/$f"
done
for f in "${SYSTEMD_FILES[@]}"; do
    [[ -r "$HERE/systemd/$f" ]] || die "ship-list file missing: console/systemd/$f"
    install -m 0644 "$HERE/systemd/$f" "$STAGE/vantage/console/systemd/$f"
done
for f in "${PROVISION_FILES[@]}"; do
    [[ -r "$HERE/provision/$f" ]] || die "ship-list file missing: console/provision/$f"
    install -m 0755 "$HERE/provision/$f" "$STAGE/vantage/console/provision/$f"
done
shopt -s nullglob
for f in "$HERE"/actions/tak-* "$HERE"/actions/console-setup-priv; do
    install -m 0755 "$f" "$STAGE/vantage/console/actions/$(basename "$f")"
done
install -m 0755 "$HERE/../common/tak-health.sh" "$STAGE/vantage/common/tak-health.sh"
[[ -r "$HERE/../docs/VANTAGE-README.md" ]] && \
    install -m 0644 "$HERE/../docs/VANTAGE-README.md" "$STAGE/vantage/README.md"
# the user guide is product-facing and belongs in the beta package; the INTERNAL
# design docs (VANTAGE-JOURNEYS, VANTAGE-VPN-DESIGN, VANTAGE-ARCHITECTURE) never ship -
# they stay in this dev repo. Ship only README + the user guide.
[[ -r "$HERE/../docs/USER-GUIDE.md" ]] && \
    install -m 0644 "$HERE/../docs/USER-GUIDE.md" "$STAGE/vantage/USER-GUIDE.md"
# the licence and its notice travel with every copy (the Community Licence requires it)
[[ -r "$HERE/../LICENSE" ]] && install -m 0644 "$HERE/../LICENSE" "$STAGE/vantage/LICENSE"
[[ -r "$HERE/../NOTICE" ]]  && install -m 0644 "$HERE/../NOTICE"  "$STAGE/vantage/NOTICE"
# brand assets the README and user guide reference (logo banner, marks, diagrams)
if [[ -d "$HERE/../docs/assets" ]]; then
    install -d -m 0755 "$STAGE/vantage/assets"
    install -m 0644 "$HERE/../docs/assets/"*.svg "$STAGE/vantage/assets/" 2>/dev/null || true
fi
# the user guide's live captures (scrubbed: no live QR, audit addresses redacted)
if [[ -d "$HERE/../docs/assets/screens" ]]; then
    install -d -m 0755 "$STAGE/vantage/assets/screens"
    install -m 0644 "$HERE/../docs/assets/screens/"*.png "$STAGE/vantage/assets/screens/" 2>/dev/null || true
fi

# ---- Vantage Deployed: the EUD app and its box-side sync surface ----------------------
# Source ships; APKs never do (the BAD check below enforces it) - devices get the APK from
# an estate's own Store shelf. The internal build-log stays in the dev repo; the ADRs,
# specs and compliance watch travel, because a source-available product should show its
# reasoning. eud-shared is vendored in beside android/ and the composite-build path is
# patched to match the shipped layout.
VD="$HERE/../vantage-deployed"
if [[ -d "$VD" ]]; then
    install -d -m 0755 "$STAGE/vantage/deployed"
    install -m 0644 "$VD/README.md" "$STAGE/vantage/deployed/README.md"
    install -m 0644 "$VD/CONTEXT.md" "$STAGE/vantage/deployed/CONTEXT.md"
    for d in boxside docs android; do
        [[ -d "$VD/$d" ]] || die "ship-list dir missing: vantage-deployed/$d"
        tar -C "$VD" -cf -             --exclude='build' --exclude='.gradle' --exclude='.kotlin' --exclude='local.properties'             --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' "$d"             | tar -xf - -C "$STAGE/vantage/deployed"
    done
    rm -f "$STAGE/vantage/deployed/docs/../build-log.md" 2>/dev/null || true
    # canonical layout: code/milux-tak beside code/milux-eud-shared; a worktree passes
    # EUD_SHARED explicitly (the same convention as the app build's -PeudShared).
    EUDSHARED="${EUD_SHARED:-$HERE/../../milux-eud-shared}"
    [[ -d "$EUDSHARED" ]] || die "eud-shared checkout missing (set EUD_SHARED=/path)"
    install -d -m 0755 "$STAGE/vantage/deployed/eud-shared"
    tar -C "$EUDSHARED" -cf -         --exclude='.git' --exclude='build' --exclude='.gradle' --exclude='.DS_Store'         settings.gradle.kts build.gradle.kts gradle gradlew gradlew.bat estate mesh identity 2>/dev/null         | tar -xf - -C "$STAGE/vantage/deployed/eud-shared" || true
    # the shipped layout puts eud-shared beside android/: point the composite build there
    sed -i.bak 's#getOrElse("../../../milux-eud-shared")#getOrElse("../eud-shared")#'         "$STAGE/vantage/deployed/android/settings.gradle.kts" &&         rm -f "$STAGE/vantage/deployed/android/settings.gradle.kts.bak"
fi

# ---- what must NOT be in the tarball, verified, not assumed ---------------------------
BAD=$(find "$STAGE" \( -name "*.deb" -o -name "*.zip" -o -name "*.apk" -o -name "*.pyc" \
      -o -name ".DS_Store" -o -name "sam*" -o -name "*.pack" -o -name "GO-LIVE*" \
      -o -name "VANTAGE-JOURNEYS*" -o -name "VANTAGE-VPN-DESIGN*" \
      -o -name "VANTAGE-ARCHITECTURE*" -o -name "build-log.md" \) | head)
[[ -z "$BAD" ]] || die "forbidden content reached the stage: $BAD"
# estate values never ship: tailnet addresses have no place in a product tarball.
# 100.64.x is exempt: it is the CGNAT documentation address the tests and docs use.
grep -rlE "100\.(6[5-9]|[7-9][0-9]|1[0-2][0-9])\.[0-9]+\.[0-9]+" "$STAGE" \
    | head | grep . && die "an estate address reached the stage" || true
# real key bodies start with the base64 of "openssh-key-v1" - the BEGIN marker alone
# appears legitimately (the wizard's paste-key placeholder, and this check itself)
grep -rl "^b3BlbnNzaC1rZXktdjE" "$STAGE" && die "a private key reached the stage" || true

TARBALL="$OUT/vantage-$VER.tgz"
# COPYFILE_DISABLE stops macOS bsdtar embedding AppleDouble xattr headers that make
# GNU tar on the target box print a wall of harmless-but-alarming warnings
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$TARBALL" -C "$STAGE" vantage 2>/dev/null \
    || COPYFILE_DISABLE=1 tar -czf "$TARBALL" -C "$STAGE" vantage
SIZE=$(du -h "$TARBALL" | cut -f1)
N=$(tar -tzf "$TARBALL" | grep -vc '/$')
echo "built $TARBALL ($SIZE, $N files)"
echo "install: scp it to the box, then:"
echo "  tar -xzf vantage-$VER.tgz && cd vantage/console && sudo ./install-vantage.sh --bind <addr>"
