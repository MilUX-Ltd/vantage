#!/usr/bin/env bash
# install-deployed-console.sh - stand up Vantage Deployed, the local console, on THIS box.
#
# Run as root on a deployed box (a MINIX-class forward kit). Installs the same single-file
# console the estate runs, but in the "deployed" edition: it manages only this box, from
# Overview/Operations/Store/Vault, with no fleet surfaces. Health comes from the box's own
# tak-health, polled by a local collector - there is no reach back to the estate console,
# by design, so the kit's own console works with the cable out.
#
# What it does NOT do: touch the estate. This box holds no estate keys and no path to any
# other box. That boundary is the product's whole point on a forward node.
#
#   sudo ./install-deployed-console.sh [--bind <addr>] [--port <n>] [--profile <p>]
#
# Idempotent: re-running updates the code and leaves the state.
set -euo pipefail
die() { echo "ERROR: $*" >&2; exit 2; }
[[ "$(id -u)" == 0 ]] || die "run as root"

BIND="127.0.0.1"; PORT="8092"; PROFILE="deployed"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bind) BIND="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done
HERE="$(cd "$(dirname "$0")" && pwd)"
[[ -f "$HERE/vantage-console-serve.py" ]] || die "run this from the console/ directory"
[[ -x /usr/local/bin/tak-health ]] || die "tak-health is not installed on this box"

# --- Vantage 2.0 namespace migration --------------------------------------
# Convert a pre-2.0 deployed box (milux-console user/dirs/units) to the
# vantage-console namespace in place before laying down 2.0. Idempotent no-op
# on a fresh or already-migrated box. See docs/VANTAGE-ARCHITECTURE.md.
migrate_from_milux_console() {
    local O="milux-console" N="vantage-console" d u
    id "$O" >/dev/null 2>&1 || [[ -e /etc/$O || -e /var/lib/$O || -e /usr/local/lib/$O ]] || return 0
    echo "==> migrating pre-2.0 install: $O -> $N"
    for u in "$O.service" "$O-deployed.service" "$O-collect.timer" "$O-collect.service"; do
        systemctl stop "$u" 2>/dev/null || true
        systemctl disable "$u" 2>/dev/null || true
    done
    if id "$O" >/dev/null 2>&1 && ! id "$N" >/dev/null 2>&1; then
        usermod -l "$N" "$O"                       # UID kept, so on-disk ownership follows
        usermod -d "/var/lib/$N" "$N" 2>/dev/null || true
    fi
    if getent group "$O" >/dev/null 2>&1 && ! getent group "$N" >/dev/null 2>&1; then
        groupmod -n "$N" "$O"
    fi
    for d in /etc /var/lib /usr/local/lib; do
        [[ -e "$d/$O" && ! -e "$d/$N" ]] && mv "$d/$O" "$d/$N"
        [[ -d "$d/$N" && ! -e "$d/$O" ]] && ln -s "$N" "$d/$O"
    done
    # the directory moved but the provisioner files kept their milux- names; the 2.0 action
    # registry looks for vantage- names, so rename them (idempotent). Without this the
    # module Install and Deploy actions fail with "provisioner not on this console".
    if [[ -d "/var/lib/$N/artifacts" ]]; then
        for f in "/var/lib/$N/artifacts/"milux-*; do
            [[ -e "$f" ]] || continue
            b=$(basename "$f"); mv -n "$f" "/var/lib/$N/artifacts/vantage-${b#milux-}"
        done
    fi
    # the console's one root grant: rewrite it for the new user and drop old-named
    # variants, or the migrated console loses every root op (setup wizard, vault).
    rm -f "/etc/sudoers.d/$O-setup" /etc/sudoers.d/console-setup
    printf '%s ALL=(root) NOPASSWD: /usr/local/bin/console-setup-priv\n' "$N" > "/etc/sudoers.d/$N-setup"
    chmod 440 "/etc/sudoers.d/$N-setup"
    visudo -cf "/etc/sudoers.d/$N-setup" >/dev/null || rm -f "/etc/sudoers.d/$N-setup"
    rm -f "/etc/systemd/system/$O.service" "/etc/systemd/system/$O-deployed.service" \
          "/etc/systemd/system/$O-collect.service" "/etc/systemd/system/$O-collect.timer"
    rm -rf "/etc/systemd/system/$O.service.d"
    systemctl daemon-reload
    echo "    migrated; laying down 2.0 below"
}
migrate_from_milux_console

echo "[1/6] user + dirs"
id vantage-console >/dev/null 2>&1 || useradd --system --home /var/lib/vantage-console \
    --shell /usr/sbin/nologin vantage-console
install -d -m 755 /usr/local/lib/vantage-console
install -d -o vantage-console -g vantage-console -m 750 /var/lib/vantage-console
install -d -o vantage-console -g vantage-console -m 750 /var/lib/vantage-console/agent
install -d -o vantage-console -g vantage-console -m 750 /var/lib/vantage-console/agent/store
install -d -m 755 /etc/vantage-console

echo "[2/6] code"
install -m 644 "$HERE/vantage-console-serve.py" /usr/local/lib/vantage-console/vantage-console-serve.py
install -m 644 "$HERE/vantage-console-collect.py" /usr/local/lib/vantage-console/vantage-console-collect.py

echo "[3/6] this box as the one local target"
LABEL=$(hostname -s)
cat > /etc/vantage-console/targets.json <<JSON
{
  "targets": [
    { "name": "self", "label": "$LABEL", "kind": "local",
      "command": "/usr/local/bin/tak-health", "profile": "$PROFILE" }
  ]
}
JSON
# No actions.json => the console is read-only. A deployed box that wants local gated
# actions installs its own action pairs later; the estate never reaches in.

echo "[4/6] seed the store shelves"
for d in tak-server mission-packs map-packs software; do
  install -d -o vantage-console -g vantage-console -m 750 "/var/lib/vantage-console/agent/store/$d"
done

echo "[5/6] services"
install -m 644 "$HERE/systemd/vantage-console-deployed.service" /etc/systemd/system/vantage-console-deployed.service
# bind/port drop-in
install -d -m 755 /etc/systemd/system/vantage-console-deployed.service.d
cat > /etc/systemd/system/vantage-console-deployed.service.d/bind.conf <<CONF
[Service]
Environment=VANTAGE_CONSOLE_BIND=$BIND
Environment=VANTAGE_CONSOLE_PORT=$PORT
CONF
# a local collector timer, reusing the estate collector against the one local target
cat > /etc/systemd/system/vantage-console-deployed-collect.service <<'SVC'
[Unit]
Description=Vantage Deployed - local health poll
[Service]
Type=oneshot
User=vantage-console
Group=vantage-console
Environment=VANTAGE_CONSOLE_STATE=/var/lib/vantage-console/agent/state.json
ExecStart=/usr/bin/python3 /usr/local/lib/vantage-console/vantage-console-collect.py
ProtectSystem=strict
ReadWritePaths=/var/lib/vantage-console/agent
SVC
cat > /etc/systemd/system/vantage-console-deployed-collect.timer <<'TMR'
[Unit]
Description=Vantage Deployed - poll every minute
[Timer]
OnBootSec=20
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
TMR
systemctl daemon-reload
systemctl enable --now vantage-console-deployed-collect.timer
systemctl start vantage-console-deployed-collect.service || true
systemctl enable --now vantage-console-deployed.service

echo "[6/6] done"
echo "Vantage Deployed is on http://$BIND:$PORT (edition=deployed, profile=$PROFILE)."
echo "It manages THIS box only. No estate keys, no path off this box."
