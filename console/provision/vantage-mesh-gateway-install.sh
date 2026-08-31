#!/usr/bin/env bash
# vantage-mesh-gateway-install.sh - stand up the Meshtastic TAK gateway on THIS box, from the
# console's Networks page (Spec 001). Streamed per-run through tak-mesh-deploy with
# its sha256; every parameter re-validated by the priv wrapper before this runs as root.
#
# Stages:
#   (full)     preflight radio and TAK - unpack the verified bundle - venv from the bundle,
#              never the network (ADR-002) - program the radio onto the channel - TAK input
#              WITH its filter group at creation (LESSONS 16) - systemd unit - start - verify.
#   --stage channel   stop the gateway, rewrite the radio's channel, restart. Nothing else.
#
# The channel URL (it embeds the PSK) arrives in VANTAGE_MESH_CHANNEL_URL_B64, environment
# not argv, so it never shows in the process table. It is written nowhere on disk.
set -euo pipefail

log()  { printf '%s %s\n' "$(date -u '+%H:%M:%S')" "$*"; }
die()  { echo "ERR $*" >&2; exit 2; }

MESH_HOME=/opt/vantage-mesh
CONF=/etc/vantage-mesh.conf
STATE_DIR=/var/lib/vantage-mesh
UNIT=tak-meshtastic-gateway
INBOX=/home/takadmin/package-inbox
MCAST_GROUP=239.2.3.1
MCAST_PORT=6970

SERIAL=""; REGION=""; CHANNEL=""; FILTER_GROUP=""; BUNDLE_SHA=""; STAGE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --serial)        SERIAL="${2:-}"; shift 2 ;;
        --region)        REGION="${2:-}"; shift 2 ;;
        --channel)       CHANNEL="${2:-}"; shift 2 ;;
        --filter-group)  FILTER_GROUP="${2:-}"; shift 2 ;;
        --bundle-sha256) BUNDLE_SHA="${2:-}"; shift 2 ;;
        --stage)         STAGE="${2:-}"; shift 2 ;;
        *) die "unknown option: $1" ;;
    esac
done

CH_URL=""
if [[ -n "${VANTAGE_MESH_CHANNEL_URL_B64:-}" ]]; then
    CH_URL=$(printf '%s' "$VANTAGE_MESH_CHANNEL_URL_B64" | base64 -d 2>/dev/null || true)
    [[ "$CH_URL" == https://meshtastic.org/e/#* ]] || die "channel URL does not decode"
fi

meshtastic_cli() {
    "$MESH_HOME/venv/bin/meshtastic" "$@"
}

write_channel() { # the radio must be free: the one-serial-one-process rule
    local serial="$1"
    [[ -n "$CH_URL" ]] || die "no channel URL supplied"
    [[ -e "$serial" ]] || die "radio not present at $serial - check the cable, then ls -l /dev/serial/by-id/"
    log "writing channel to the radio (this takes a few seconds)"
    meshtastic_cli --port "$serial" --seturl "$CH_URL" >/dev/null \
        || die "the radio refused the channel write - is another process holding $serial?"
}

# ---------------------------------------------------------------- stage: channel only
if [[ "$STAGE" == "channel" ]]; then
    [[ -f "$CONF" ]] || die "no $CONF - deploy the gateway first"
    # shellcheck source=/dev/null
    source "$CONF"
    [[ -n "${SERIAL:-}" ]] || die "$CONF carries no SERIAL"
    log "== apply channel ${CHANNEL:-?} =="
    systemctl stop "$UNIT" || true
    trap 'systemctl start "$UNIT" || true' EXIT
    write_channel "$SERIAL"
    sed -i "s/^CHANNEL=.*/CHANNEL=$CHANNEL/" "$CONF" 2>/dev/null || true
    systemctl start "$UNIT"
    trap - EXIT
    systemctl is-active --quiet "$UNIT" || die "gateway did not come back after the channel write"
    log "channel applied; gateway running. Devices rejoin by scanning the new QR."
    echo "STAGE-OK channel"
    exit 0
fi

# ---------------------------------------------------------------- full install
[[ -n "$SERIAL" && -n "$REGION" && -n "$CHANNEL" && -n "$FILTER_GROUP" && -n "$BUNDLE_SHA" ]] \
    || die "missing parameters for a full install"

log "== deploy Meshtastic TAK gateway =="
log "-- 1/6 preflight --"
[[ -e "$SERIAL" ]] || die "radio not present at $SERIAL - plug it in, then ls -l /dev/serial/by-id/"
[[ -d /opt/tak ]]  || die "TAK Server is not installed on this box - build it first from Deploy"
command -v python3 >/dev/null || die "python3 is missing"
python3 -m venv --help >/dev/null 2>&1 || die "python3-venv is missing (apt-get install python3-venv)"

log "-- 2/6 the gateway bundle --"
bundle="$INBOX/$BUNDLE_SHA.tgz"
[[ -f "$bundle" ]] || die "bundle not staged: the console pushes it first (Store shelf, mesh-gateway)"
got=$(sha256sum "$bundle" | awk '{print $1}')
[[ "$got" == "$BUNDLE_SHA" ]] || die "staged bundle hash mismatch: got $got"
rm -rf "$MESH_HOME/bundle"
mkdir -p "$MESH_HOME/bundle"
tar -xzf "$bundle" -C "$MESH_HOME/bundle" --strip-components=1 \
    || die "bundle does not unpack"
[[ -d "$MESH_HOME/bundle/wheels" && -d "$MESH_HOME/bundle/gateway" ]] \
    || die "bundle layout wrong: expected wheels/ and gateway/ (cut it with cut-bundle.sh)"

log "-- 3/6 venv from the bundle (no network) --"
rm -rf "$MESH_HOME/venv"
python3 -m venv "$MESH_HOME/venv"
# wheels only, nothing builds on the box: the patched gateway wheel was built at cut
# time (its poetry build backend needs the internet this install must never touch)
"$MESH_HOME/venv/bin/pip" install --quiet --no-index \
    --find-links "$MESH_HOME/bundle/wheels" TAK-Meshtastic-Gateway \
    || die "venv build failed - the bundle must carry every wheel for this box's architecture (re-cut with the current cut-bundle.sh)"
[[ -x "$MESH_HOME/venv/bin/tak-meshtastic-gateway" ]] || die "gateway entrypoint missing after install"
# The site-package patches (the meshtastic library ships as wheels; a wheel cannot carry
# a source patch, so the bundle ships them and they land here, verified by exit code).
sp=$("$MESH_HOME/venv/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
for p in "$MESH_HOME"/bundle/patches/sitepkg-*.patch; do
    [[ -e "$p" ]] || continue
    log "  applying $(basename "$p")"
    (cd "$sp" && patch -p1 --forward --fuzz=2) < "$p" \
        || die "site-package patch did not apply: $(basename "$p")"
done
"$MESH_HOME/venv/bin/python3" -c "import tak_meshtastic_gateway; from meshtastic.protobuf import atak_pb2" \
    || die "patched install does not import"

log "-- 4/6 program the radio --"
systemctl stop "$UNIT" 2>/dev/null || true
write_channel "$SERIAL"

log "-- 5/6 TAK input '$FILTER_GROUP'-scoped, created with its filter group --"
CC=/opt/tak/CoreConfig.xml
[[ -f "$CC" ]] || die "no CoreConfig.xml"
cp -n "$CC" "$CC.orig-mesh" || true
MESH_GROUP="$FILTER_GROUP" MESH_MCAST="$MCAST_GROUP" MESH_PORT="$MCAST_PORT" \
python3 - "$CC" <<'PYCC'
import os, re, sys
p = sys.argv[1]
s = open(p).read()
s_nc = re.sub(r"<!--.*?-->", "", s, flags=re.S)
grp, mc, port = os.environ["MESH_GROUP"], os.environ["MESH_MCAST"], os.environ["MESH_PORT"]
if re.search(r'<input[^>]*_name="meshtastic"', s_nc):
    # an input's port and protocol cannot be modified in place (LESSONS 16); if one is
    # already here, leave it alone and say so - delete-and-recreate is an operator call
    print("input 'meshtastic' already present; leaving it untouched")
    raise SystemExit(0)
blk = (f'        <input auth="anonymous" _name="meshtastic" protocol="mcast" '
       f'port="{port}" group="{mc}">\n'
       f'            <filtergroup>{grp}</filtergroup>\n'
       f'        </input>\n')
m = re.search(r"^[ \t]*</network>", s, re.M)
if not m:
    raise SystemExit("no </network> element to anchor on")
s = s[:m.start()] + blk + s[m.start():]
open(p, "w").write(s)
# verify our own write before claiming success - the silent-edit disease (LESSONS, provision
# stage 6). The needle is our concrete filtergroup line, which no example config carries.
if f"<filtergroup>{grp}</filtergroup>" not in open(p).read():
    raise SystemExit("mesh input insertion did not verify")
print("input 'meshtastic' created with filter group " + grp)
PYCC
chown tak:tak "$CC" 2>/dev/null || true

log "-- 6/6 unit, config, start --"
# a box migrating from a hand-built gateway keeps its old unit as the rollback:
# restore the .orig-mesh copy, daemon-reload, start, and the estate build is back
[[ -f "/etc/systemd/system/$UNIT.service" ]] \
    && cp -n "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.service.orig-mesh" || true
mkdir -p "$STATE_DIR"
{
    echo "# written by vantage-mesh-gateway-install $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "SERIAL=$SERIAL"
    echo "REGION=$REGION"
    echo "CHANNEL=$CHANNEL"
    echo "FILTER_GROUP=$FILTER_GROUP"
    echo "EXTRA_ARGS="
} > "$CONF"
chmod 0644 "$CONF"
cat > "/etc/systemd/system/$UNIT.service" <<UNITEOF
[Unit]
Description=Meshtastic TAK gateway (Vantage Networks)
After=network.target

[Service]
Type=simple
EnvironmentFile=$CONF
ExecStart=$MESH_HOME/venv/bin/tak-meshtastic-gateway -s \${SERIAL} \$EXTRA_ARGS
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable --now "$UNIT"
log "restarting TAK Server to pick up the input (brief outage)"
systemctl restart takserver || die "takserver did not restart - read journalctl -u takserver"
sleep 3
systemctl is-active --quiet "$UNIT" || die "gateway unit is not active - read journalctl -u $UNIT"
log "gateway running on $SERIAL, channel $CHANNEL, region $REGION"
log "the proof is a marker on a client that signed in normally - an input counter is not it"
echo "STAGE-OK deploy"
