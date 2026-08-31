#!/usr/bin/env bash
# vantage-infratak-provision.sh - stand up infra-TAK on a bare Ubuntu/Rocky box, non-
# interactively, from the console. infra-TAK (github.com/takwerx/infra-TAK) is a browser
# platform that deploys the whole TAK ecosystem from its own marketplace; MilUX offers it as
# a deployment CHOICE alongside the MilUX TAK Server, so a customer who wants its breadth
# gets it stood up and then estate-managed by Vantage.
#
# This is the deterministic wrapper around infra-TAK's own `sudo ./start.sh`, which prompts
# once for an admin password. We generate that password, feed it in, and leave it root-only
# on the box. The clone is pinned to a tag so a customer's install is reproducible and its
# drift is trackable, exactly like every other pinned component.
#
#   vantage-infratak-provision.sh [--ref <tag-or-branch>] [--dry-run]
#
# With no --ref it clones the pinned tag below, so two customers provisioned a month apart
# get the same software. Pass --ref to move a box deliberately.
#
# On success it prints INFRATAK-READY with the URL and where the password lives.
set -uo pipefail

# The pinned baseline. infra-TAK tags every release -alpha and has no stable line, so this
# is the tag we have actually stood up, not a claim about its maturity. Defaulting to a
# branch made two installs a week apart different software with nothing recording why, which
# is the one thing the header above promises not to do. Move this deliberately, with a note
# in the changelog, exactly like MEDIAMTX_VER and the rest.
REF="v10.1.55-alpha" DRY=0
die() { echo "PROVISION-ERROR: $*" >&2; exit 2; }
run() { if (( DRY )); then echo "DRY: $*"; else eval "$*"; fi; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref) REF="${2:-}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done
[[ "$REF" =~ ^[A-Za-z0-9._/-]{1,60}$ ]] || die "bad --ref"
(( DRY )) || [[ "$(id -u)" == 0 ]] || die "run as root"

log() { echo "== $* =="; }

log "STAGE 1/4 dependencies"
export DEBIAN_FRONTEND=noninteractive
run "apt-get update -qq"
run "apt-get install -y -qq git curl ca-certificates"

log "STAGE 2/4 fetch infra-TAK ($REF)"
if [[ -d /opt/infra-TAK/.git ]]; then
    echo "infra-TAK already cloned at /opt/infra-TAK; keeping it"
else
    run "git clone --depth 1 --branch $REF https://github.com/takwerx/infra-TAK.git /opt/infra-TAK"
fi
[[ -f /opt/infra-TAK/start.sh || $DRY == 1 ]] || die "infra-TAK start.sh not found after clone"

log "STAGE 3/4 admin password"
if (( DRY )); then
    echo "DRY: generate a 20-char admin password -> /root/infra-tak-admin-pass (root only)"
    PW="DRY-PLACEHOLDER"
else
    if [[ -f /root/infra-tak-admin-pass ]]; then
        PW=$(cat /root/infra-tak-admin-pass)
        echo "reusing the existing /root/infra-tak-admin-pass"
    else
        PW=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)
        install -m 600 /dev/null /root/infra-tak-admin-pass
        printf '%s\n' "$PW" > /root/infra-tak-admin-pass
    fi
fi

log "STAGE 4/4 run infra-TAK start.sh"
# start.sh prompts twice for the admin password on a first-time setup, then serves the
# console on :5001. Feed the password on stdin; stream its output.
if (( DRY )); then
    echo "DRY: (cd /opt/infra-TAK && printf '%s\\n%s\\n' <pass> <pass> | ./start.sh)"
    echo "STAGE-OK infratak (dry)"
    echo "DRY RUN COMPLETE - nothing was changed on this box. infra-TAK is NOT installed."
    exit 0
fi
ip=$(hostname -I 2>/dev/null | awk '{print $1}')
( cd /opt/infra-TAK && printf '%s\n%s\n' "$PW" "$PW" | ./start.sh ) || die "infra-TAK start.sh failed"

echo
echo "INFRATAK-READY url=https://${ip:-this-box}:5001 admin_pass=/root/infra-tak-admin-pass ref=$REF"
echo "infra-TAK is running. Log in at the URL and deploy modules from its marketplace."
echo "Enrol this box to Vantage to health-monitor it alongside the estate."
