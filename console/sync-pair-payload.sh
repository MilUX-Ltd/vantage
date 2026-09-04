#!/usr/bin/env bash
# VANTAGE-CONSOLE-INSTALLER - pairs this box's sync engine with the estate console (Spec 010, slice 5).
# Sent by the estate console over the install-console key, which already authenticates this
# box; the id this script reports is the id the console pins. It uses the engine the box has,
# installs the pinned engine from the package the console sent if there is none, hardens the
# configuration (no discovery, relays, NAT traversal or reporting; web page on loopback), gives
# the engine the vault, and introduces the console as a known device at a fixed address.
set -euo pipefail
MASTER_ID='__MASTER_ID__'
MASTER_ADDR='__MASTER_ADDR__'
PIN='__PIN__'
UNIT='syncthing@vaultsync'
HOME_DIR='/var/lib/vaultsync/syncthing'
err() { echo "SYNC-PAIR-ERROR $*"; exit 3; }

root="${VANTAGE_CONSOLE_VAULT:-}"
if [[ -z "$root" ]]; then
  for u in /etc/systemd/system/vantage-console-deployed.service /etc/systemd/system/vantage-console.service; do
    [[ -f "$u" ]] && root=$(grep -m1 '^Environment=VANTAGE_CONSOLE_VAULT=' "$u" | cut -d= -f3-) && [[ -n "$root" ]] && break
  done
fi
root="${root:-/srv/vault/Deployed}"

want=$(mktemp) && base64 -d > "$want" <<'B64SYNC'
__HELPER_B64__
B64SYNC
if [[ ! -x /usr/local/bin/console-sync-priv ]] || ! cmp -s "$want" /usr/local/bin/console-sync-priv; then
  install -m 0755 "$want" /usr/local/bin/console-sync-priv
fi
rm -f "$want"
install -d -m 0750 /var/lib/vantage-sync /etc/vantage
[[ -f /var/lib/vantage-sync/sync-paths.json ]] || printf '{"vault": "%s"}\n' "$root" > /var/lib/vantage-sync/sync-paths.json

engine=existing
if ! systemctl list-unit-files 'syncthing@*' 2>/dev/null | grep -q '^syncthing@'; then
  bin=$(command -v syncthing || true)
  if [[ -z "$bin" ]]; then
    tgz=""
    for c in "/root/syncthing-linux-amd64-$PIN.tar.gz" "/var/lib/vantage-console/agent/store/software/syncthing-linux-amd64-$PIN.tar.gz" \
             /home/USER/package-inbox/__PKG_SHA__.tgz; do
      [[ -f "$c" ]] && tgz="$c" && break
    done
    if [[ -n "$tgz" && "$tgz" == *package-inbox* ]]; then
      echo "__PKG_SHA__  $tgz" | sha256sum -c --status - || err "the package in the inbox does not match the pinned hash"
    fi
    [[ -n "$tgz" ]] || err "needs-package syncthing-linux-amd64-$PIN.tar.gz"
    tmp=$(mktemp -d)
    tar -xzf "$tgz" -C "$tmp"
    install -m 0755 "$tmp/syncthing-linux-amd64-$PIN/syncthing" /usr/local/bin/syncthing
    rm -rf "$tmp"
    bin=/usr/local/bin/syncthing
    engine=installed
  fi
  id vaultsync >/dev/null 2>&1 || useradd --system --home-dir /var/lib/vaultsync --create-home --shell /usr/sbin/nologin vaultsync
  install -d -m 0750 -o vaultsync -g vaultsync /var/lib/vaultsync "$HOME_DIR"
  cat > /etc/systemd/system/syncthing@.service <<UNIT
[Unit]
Description=Vantage sync engine (Syncthing) for %i
After=network.target

[Service]
User=%i
Environment=STNOUPGRADE=1
Environment=STNODEFAULTFOLDER=1
ExecStart=$bin serve --no-browser --no-restart --logflags=0 --home=/var/lib/%i/syncthing
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/%i /srv/vault

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
fi
bin="${bin:-$(command -v syncthing || echo /usr/bin/syncthing)}"

cfg="$HOME_DIR/config.xml"
if [[ ! -f "$cfg" ]]; then
  id vaultsync >/dev/null 2>&1 || useradd --system --home-dir /var/lib/vaultsync --create-home --shell /usr/sbin/nologin vaultsync
  install -d -m 0750 -o vaultsync -g vaultsync /var/lib/vaultsync "$HOME_DIR"
  pw=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
  sudo -u vaultsync "$bin" generate --home="$HOME_DIR" --no-default-folder --gui-user=vantage --gui-password="$pw" >/dev/null 2>&1
fi
[[ -f "$cfg" ]] || err "the engine produced no configuration at $cfg"
# hardening, in the file the engine reads at start: no discovery, relays, NAT or reporting;
# the engine's own web page stays on loopback
sed -i 's#<globalAnnounceEnabled>true#<globalAnnounceEnabled>false#; s#<localAnnounceEnabled>true#<localAnnounceEnabled>false#; s#<relaysEnabled>true#<relaysEnabled>false#; s#<natEnabled>true#<natEnabled>false#; s#<urAccepted>0<#<urAccepted>-1<#; s#<address>0.0.0.0:8384</address>#<address>127.0.0.1:8384</address>#' "$cfg"
mkdir -p "$root"
chgrp -R vaultsync "$root" 2>/dev/null || true
chmod -R g+rwX "$root" 2>/dev/null || true
find "$root" -type d -exec chmod g+s {} + 2>/dev/null || true
systemctl enable "$UNIT" >/dev/null 2>&1 || true
systemctl restart "$UNIT"

if [[ ! -s /etc/vantage/sync-api.key ]]; then
  key=$(sed -n 's/.*<apikey>\([^<]*\)<\/apikey>.*/\1/p' "$cfg" | head -1)
  [[ -n "$key" ]] && printf '%s\n' "$key" | install -m 0600 -o root -g root /dev/stdin /etc/vantage/sync-api.key
fi
for i in $(seq 1 60); do /usr/local/bin/console-sync-priv status >/dev/null 2>&1 && break; sleep 0.5; done
/usr/local/bin/console-sync-priv status >/dev/null 2>&1 || err "the engine did not answer on this box after starting"
if [[ "$MASTER_ADDR" != "-" ]]; then
  /usr/local/bin/console-sync-priv device-add "$MASTER_ID" office "$MASTER_ADDR" >/dev/null
elif ! /usr/local/bin/console-sync-priv device-list | grep -q "$MASTER_ID"; then
  err "this box does not know the console and the console gave no address to dial"
fi
myid=$(/usr/local/bin/console-sync-priv status | python3 -c 'import json,sys; print(json.load(sys.stdin)["myID"])')
echo "SYNC-PAIR-JSON {\"deviceID\": \"$myid\", \"engine\": \"$engine\", \"unit\": \"$UNIT\", \"vault\": \"$root\"}"
