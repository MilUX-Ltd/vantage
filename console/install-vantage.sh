#!/usr/bin/env bash
#
# install-vantage.sh - stand up Vantage, the estate console, on a fresh Ubuntu server.
#
# THE PRODUCT'S FIRST-RUN PATH. Run as root on the box that will BE the console:
#
#     git clone <the vantage repo> && cd vantage/console
#     sudo ./install-vantage.sh
#
# Ten minutes later there is a running, empty, branded console: sign in, set your
# password, and grow an estate from the browser - enrol existing TAK servers, deploy
# new ones from bare boxes, install components from the Modules panel, manage files,
# credentials and federation. No MilUX assumptions: identity comes from the instance
# file (Customize page), servers from enrolment. This installer touches nothing beyond
# its own directories and firewall rule.
#
#   sudo ./install-vantage.sh [--bind <addr>] [--port <n>]
#
# --bind defaults to 127.0.0.1: choose your exposure deliberately. A tailnet or VPN
# address is the intended posture; 0.0.0.0 is accepted but you are told what it means.
# Idempotent: re-running updates code and leaves state and config alone.
set -euo pipefail
die() { echo "ERROR: $*" >&2; exit 2; }
[[ $EUID -eq 0 ]] || die "run as root (sudo ./install-vantage.sh)"

BIND="127.0.0.1"; PORT="8090"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bind) BIND="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ "$BIND" =~ ^[0-9a-fA-F.:]+$ ]] || die "bad --bind"
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || die "bad --port"

USER_NAME=vantage-console
LIB=/usr/local/lib/vantage-console
ETC=/etc/vantage-console
VAR=/var/lib/vantage-console
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in vantage-console-serve.py vantage-console-collect.py console-enrol-server.sh \
         actions/console-setup-priv provision/vantage-tak-provision.sh; do
    [[ -r "$SRC/$f" ]] || die "missing source file: $SRC/$f - run from the console/ directory"
done
command -v python3 >/dev/null || die "python3 not found (apt-get install -y python3)"
command -v ssh >/dev/null || die "openssh-client not found"

echo "==> [1/8] user"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home-dir "$VAR" --create-home \
    --shell /usr/sbin/nologin "$USER_NAME"

echo "==> [2/8] directories"
install -d -m 0755 "$LIB" "$ETC"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" "$VAR" "$VAR/agent" "$VAR/agent/jobs" \
    "$VAR/agent/store" "$VAR/artifacts"
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$VAR/.ssh"
# the Knowledge Vault: without this, the console's first template click dies on
# a root-owned /srv with a raw permission error
install -d -m 0775 -o "$USER_NAME" -g "$USER_NAME" /srv/vault /srv/vault/Deployed
# a stub state so the very first browser hit renders instead of 503ing while the
# collector's first minute ticks over
[ -f "$VAR/state.json" ] || cat > "$VAR/state.json" <<'SEOF'
{"generated_at": "", "estate_result": "UNKNOWN", "targets": []}
SEOF
chown "$USER_NAME:$USER_NAME" "$VAR/state.json"
for d in tak-server mission-packs map-packs software; do
    install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" "$VAR/agent/store/$d"
done

echo "==> [3/8] code + helpers"
install -m 0755 "$SRC/vantage-console-serve.py"   "$LIB/"
install -m 0755 "$SRC/vantage-console-collect.py" "$LIB/"
install -m 0755 "$SRC/console-enrol-server.sh"  /usr/local/bin/console-enrol-server
install -m 0755 "$SRC/actions/console-setup-priv" /usr/local/bin/console-setup-priv
# the full action catalogue lives on the console as the SOURCE enrolment pushes to
# every box it manages - the whole gated-management surface travels from here
for a in "$SRC"/actions/tak-*; do
    install -m 0755 "$a" "/usr/local/bin/$(basename "$a")"
done
install -m 0755 "$SRC/provision/vantage-tak-provision.sh" "$VAR/artifacts/vantage-tak-provision.sh"
[[ -r "$SRC/provision/vantage-infratak-provision.sh" ]] && \
    install -m 0755 "$SRC/provision/vantage-infratak-provision.sh" "$VAR/artifacts/vantage-infratak-provision.sh"
[[ -r "$SRC/../common/tak-health.sh" ]] && \
    install -m 0755 "$SRC/../common/tak-health.sh" "$VAR/artifacts/tak-health.sh"

# This box is now A CONSOLE. The marker is what console-side tooling (enrolment above
# all) checks before acting - the product's replacement for a hard-coded hostname guard.
date -u +"installed=%Y-%m-%dT%H:%M:%SZ" > "$ETC/console-host"
chmod 0644 "$ETC/console-host"

echo "==> [4/8] sudoers for the setup helper"
cat > /etc/sudoers.d/vantage-console-setup <<EOF
$USER_NAME ALL=(root) NOPASSWD: /usr/local/bin/console-setup-priv
EOF
chmod 440 /etc/sudoers.d/vantage-console-setup
visudo -c >/dev/null || die "sudoers validation failed"

echo "==> [5/8] empty estate config (kept if present)"
[[ -f "$ETC/targets.json" ]] || printf '{ "targets": [] }\n' > "$ETC/targets.json"
[[ -f "$ETC/actions.json" ]] || cat > "$ETC/actions.json" <<'EOF'
{
  "enabled": ["provision-server", "provision-infratak", "upgrade-server",
              "enrol-device", "issue-cert", "list-credentials", "fetch-credential",
              "harden-plan", "harden-apply", "harden-revert", "set-loadout",
              "firewall-plan", "firewall-apply"],
  "targets": {}
}
EOF

echo "==> [6/8] services"
cat > /etc/systemd/system/vantage-console.service <<EOF
[Unit]
Description=Vantage estate console
After=network-online.target
[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
ExecStart=/usr/bin/python3 $LIB/vantage-console-serve.py
Environment=VANTAGE_CONSOLE_BIND=$BIND
Environment=VANTAGE_CONSOLE_PORT=$PORT
Restart=always
RestartSec=5
NoNewPrivileges=no
PrivateTmp=yes
ProtectHome=yes
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/vantage-console-collect.service <<EOF
[Unit]
Description=Vantage estate console - health poll
[Service]
Type=oneshot
User=$USER_NAME
Group=$USER_NAME
ExecStart=/usr/bin/python3 $LIB/vantage-console-collect.py
EOF
cat > /etc/systemd/system/vantage-console-collect.timer <<'EOF'
[Unit]
Description=Vantage estate console - poll every minute
[Timer]
OnBootSec=20
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now vantage-console-collect.timer
systemctl enable --now vantage-console.service

echo "==> [7/8] firewall"
# The rule is added even while ufw is INACTIVE: ufw stores it, and whatever later
# enables the firewall (the TAK provisioner's hardening stage, above all) then cannot
# silently wall off the console - the exact way the first-run page went deaf mid-build.
if [[ "$BIND" != "127.0.0.1" && "$BIND" != "::1" ]] && command -v ufw >/dev/null; then
    ufw allow "$PORT/tcp" >/dev/null 2>&1 || true
    echo "    allowed $PORT/tcp (stored; applies whenever ufw is enabled)"
fi

echo "==> [8/8] done"
sleep 2
if systemctl is-active --quiet vantage-console; then
    echo
    echo "Vantage is running on http://$BIND:$PORT"
    echo
    echo "Open that address in a browser. The console walks you through the rest:"
    echo "  1. Set the operator password (the first screen - nothing else opens until"
    echo "     it is done)."
    echo "  2. The empty Overview IS the build screen: upload the TAK Server .deb from"
    echo "     your own tak.gov account, fill in the server's public name, press Install."
    echo "  3. When it finishes you get a QR code - scan it with ATAK and the device"
    echo "     joins the map."
    [[ "$BIND" == "127.0.0.1" ]] && {
        echo
        echo "Bound to loopback. To serve your VPN or tailnet address, re-run:"
        echo "  sudo ./install-vantage.sh --bind <that-address>"
    }
else
    die "the console service did not start - journalctl -u vantage-console"
fi
