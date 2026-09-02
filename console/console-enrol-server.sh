#!/usr/bin/env bash
# console-enrol-server - add a TAK server to the estate in one deliberate act.
#
# Runs ON the console box (the NUC), as root, by a human. Joining the estate is a trust
# decision, exactly like adding a key, so this never runs from the console UI and never
# will. What it removes is the failure mode, proven twice on 25 Aug 2026, of hand-carried
# paste blocks: a placeholder pasted literally into authorized_keys, and an afternoon lost.
#
# The console box is the SOURCE: the tak-health artefact it pushes, the action scripts in
# its own /usr/local/bin, and the public halves of its scoped keys are what a new box
# needs. Nothing is fetched from the internet and no repo checkout is required; the estate
# propagates itself.
#
#     console-enrol-server <name> <admin-ssh> --profile <cloud|nuc|deployed> \
#         [--label "Human name"] [--enrol-host <fqdn>] [--expected-offline] [--dry-run]
#
#     <name>       short estate name, e.g. training-1 ([a-z0-9-], max 24)
#     <admin-ssh>  YOUR admin route to the new box: root@host, or user@host with
#                  passwordless sudo. Used once, for this enrolment; the console itself
#                  never holds it.
#     --profile    which tak-health profile the box runs (see tak-health --list-profiles)
#     --enrol-host the FQDN phones enrol against, when it differs from hostname -f
#
# After enrolment the box appears on the next poll: watched via its takwatch forced
# command, actionable via its takadmin forced commands, judged against the baseline.
#
# MilUX, 25 August 2026.
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 2; }

KEYDIR=/var/lib/vantage-console/.ssh
ARTEFACT=/var/lib/vantage-console/artifacts/tak-health.sh
TARGETS=/etc/vantage-console/targets.json
ACTIONS=/etc/vantage-console/actions.json

# key -> entry script; the estate's whole action catalogue, kept in step with the
# console registry. A new action means a new pair here and a re-enrol (idempotent).
declare -A ACTION_SCRIPTS=(
  [id_action_logs]=tak-tail-logs
  [id_action_certs]=tak-list-certs
  [id_action_lerenew]=tak-le-renew
  [id_action_svc]=tak-restart-service
  [id_action_checker]=tak-push-checker
  [id_action_cloudtak]=tak-update-cloudtak
  [id_action_cert]=tak-issue-cert
  [id_action_enrol]=tak-enrol-device
  [id_action_group]=tak-set-group
  [id_action_revoke]=tak-revoke-cert
  [id_action_cfgview]=tak-view-coreconfig
  [id_action_cfgbak]=tak-backup-coreconfig
  [id_action_fedca]=tak-federation-ca
  [id_action_feden]=tak-federation-enable
  [id_action_fedconn]=tak-federation-connect
  [id_action_fedgroups]=tak-federation-groups
  [id_action_provision]=tak-provision
  [id_action_credlist]=tak-list-credentials
  [id_action_credfetch]=tak-fetch-credential
  [id_action_pkgpush]=tak-push-package
  [id_action_upgrade]=tak-upgrade-server
  [id_action_capass]=tak-capass
  [id_action_vpn]=tak-vpn
  [id_action_install_console]=tak-install-console
  [id_action_loadimg]=tak-load-images
  [id_action_destroy]=tak-destroy
  [id_action_harden]=tak-harden
  [id_action_firewall]=tak-firewall
  [id_action_conmode]=tak-console-mode
  [id_action_authcon]=tak-authorize-console
  [id_action_conadmin]=tak-console-admin
  [id_action_loadout]=tak-set-loadout
  [id_action_kiosk]=tak-kiosk
  [id_action_meshdeploy]=tak-mesh-deploy
  [id_action_meshchan]=tak-mesh-channel
)

# Scripts that deliberately have NO -priv half: they touch nothing beyond their own
# user's staging area, so a root wrapper (and a sudoers line) would widen, not narrow.
declare -A NO_PRIV=([tak-push-package]=1)

# Root-only helpers with no console key: installed so the box can take part in the trust
# step, but reachable only by a human with root (no forced command, no sudoers).
EXTRA_SCRIPTS=(tak-federation-trust tak-federation-trust-priv)

# ---------------------------------------------------------------- arguments
NAME="${1:-}"; ADMIN="${2:-}"; shift 2 2>/dev/null || die "usage: console-enrol-server <name> <admin-ssh> --profile <p> [options]"
PROFILE=""; LABEL=""; ENROL_HOST=""; EXPECTED_OFFLINE=false; DRY=false; IDENTITY=""; FQDN=""; COMPONENTS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --label) LABEL="${2:-}"; shift 2 ;;
    --fqdn) FQDN="${2:-}"; shift 2 ;;           # this box's TAK FQDN, recorded for the checker
    --enrol-host) ENROL_HOST="${2:-}"; shift 2 ;;
    --identity) IDENTITY="${2:-}"; shift 2 ;;   # bootstrap key for the admin route (setup wizard)
    --components) COMPONENTS="${2:-}"; shift 2 ;;  # seed the declared loadout (Spec 002)
    --expected-offline) EXPECTED_OFFLINE=true; shift ;;
    --dry-run) DRY=true; shift ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ "$NAME" =~ ^[a-z0-9-]{1,24}$ ]] || die "name must match [a-z0-9-], max 24 chars"
[[ "$ADMIN" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$ ]] || die "admin-ssh must look like user@host"
case "$PROFILE" in cloud|nuc|firmbase|deployed) ;; *) die "--profile must be one of: cloud firmbase deployed (nuc = old name for firmbase)" ;; esac
[[ "$PROFILE" == "nuc" ]] && PROFILE="firmbase"
[[ -z "$FQDN" || "$FQDN" =~ ^[A-Za-z0-9.-]{1,253}$ ]] || die "--fqdn must be a hostname"
[[ -z "$COMPONENTS" || "$COMPONENTS" =~ ^[a-z]+(,[a-z]+)*$ ]] || die "--components must be a comma list of [a-z]+ tokens"
# Phones enrol against the public name, and it is baked into every QR. Without this
# default a wizard-built box fell back to its bare hostname and every QR was dead
# on arrival (dev-cloud, 26 Aug 2026: host 'milux-dev' in the QR, unresolvable).
[[ -z "$ENROL_HOST" && -n "$FQDN" ]] && ENROL_HOST="$FQDN"
[[ -n "$LABEL" ]] || LABEL="$NAME"
HOST="${ADMIN#*@}"

# ---------------------------------------------------------------- preflight
# The guard is a marker, not a hostname: install-vantage.sh (or install-console.sh on the
# MilUX estate) stamps /etc/vantage-console/console-host on the box that IS the console.
# Any box can be a console; no box is one by accident.
# The hostname fallback is an internal convenience, and the publish scrub rewrites the name
# it looks for. Held in a QUOTED variable so a rewritten value survives as one token: as a
# bare glob this became `== *the admin box*`, a syntax error, and shipped that way in every
# release from 25 Aug 2026. The scrub is right to rewrite it; the code has to survive it.
CONSOLE_HOST_HINT="the admin box"
[[ -f /etc/vantage-console/console-host || "$(hostname -s)" == *"$CONSOLE_HOST_HINT"* ]] \
    || die "run this on the console box (no /etc/vantage-console/console-host marker)"
[[ "$(id -u)" == 0 ]] || die "run as root (sudo console-enrol-server ...)"
[[ -f "$ARTEFACT" ]] || die "no checker artefact at $ARTEFACT"
# The console's own keys self-mint on first use: a fresh product install has none, and
# there is nothing an operator could choose about them. Missing SCRIPTS still die - they
# mean a broken install, not a first run.
mint_key() {
  local kf="$1" comment="$2"
  [[ -f "$kf.pub" ]] && return 0
  install -d -m 700 "$KEYDIR"
  ssh-keygen -t ed25519 -N "" -C "$comment" -f "$kf" -q || die "could not generate $kf"
  chown vantage-console:vantage-console "$kf" "$kf.pub" 2>/dev/null || true
  chmod 600 "$kf"
  echo "  [key] minted $(basename "$kf")"
}
mint_key "$KEYDIR/id_ed25519" "vantage-console-health"
for k in "${!ACTION_SCRIPTS[@]}"; do
  mint_key "$KEYDIR/$k" "vantage-console-$k"
  s="${ACTION_SCRIPTS[$k]}"
  [[ -x "/usr/local/bin/$s" ]] || die "missing /usr/local/bin/$s on the console box"
  [[ -n "${NO_PRIV[$s]:-}" || -x "/usr/local/bin/$s-priv" ]] || die "missing /usr/local/bin/$s-priv on the console box"
done

IDOPT=""
if [[ -n "$IDENTITY" ]]; then
  [[ -f "$IDENTITY" ]] || die "identity file $IDENTITY does not exist"
  IDOPT="-i $IDENTITY -o IdentitiesOnly=yes"
fi
# Known hosts live in a path the console's sandbox can write; /root/.ssh is read-only
# there, and every ssh otherwise warns (harmless) or, worse, cannot pin the host.
KNOWN="-o UserKnownHostsFile=/var/lib/console-setup/known_hosts"
install -d -m 700 /var/lib/console-setup 2>/dev/null || true
SSH="ssh $IDOPT -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $KNOWN $ADMIN"
$SSH true 2>/dev/null || die "cannot reach $ADMIN non-interactively (key auth required)"
if [[ "$($SSH 'id -u' 2>/dev/null)" == "0" ]]; then RSUDO=""
elif $SSH 'sudo -n true' 2>/dev/null; then RSUDO="sudo"
else die "$ADMIN is neither root nor passwordless-sudo; enrol needs an admin route"; fi
RHOST="$($SSH 'hostname -s' 2>/dev/null || echo '?')"
echo "enrolling '$NAME' ($LABEL): $ADMIN -> host '$RHOST', profile $PROFILE, sudo='${RSUDO:-root}'"

# ---------------------------------------------------------------- the plan
HEALTH_LINE="command=\"/usr/local/bin/tak-health --profile $PROFILE --json\",restrict $(cat "$KEYDIR/id_ed25519.pub")"
if $DRY; then
  echo "-- DRY RUN; would do the following on $ADMIN --"
  echo "install tak-health ($(grep -m1 '^VERSION=' "$ARTEFACT")) + self-test gate"
  echo "/etc/tak-health.conf: FQDN=${FQDN:-<none>} HOSTMATCH=$RHOST"
  echo "create users takwatch, takadmin; install ${#ACTION_SCRIPTS[@]} action script pairs"
  echo "sudoers: ${#ACTION_SCRIPTS[@]} priv wrappers; qrencode via apt (best effort)"
  echo "takwatch authorized_keys: 1 health line; takadmin: ${#ACTION_SCRIPTS[@]} action lines"
  [[ -n "$ENROL_HOST" ]] && echo "/etc/tak-enrol-host = $ENROL_HOST"
  echo "-- and locally --"
  echo "targets.json += {name: $NAME, ssh: takwatch@$HOST, profile: $PROFILE, expected_offline: $EXPECTED_OFFLINE}"
  echo "actions.json targets += {$NAME: takadmin@$HOST}; fresh poll"
  exit 0
fi

# ---------------------------------------------------------------- remote: checker
echo "[1/6] tak-health (self-test gated)"
$SSH "$RSUDO bash -s" <<EOF
set -euo pipefail
tmp=\$(mktemp); cat > "\$tmp" <<'ARTEFACT_EOF'
$(cat "$ARTEFACT")
ARTEFACT_EOF
bash -n "\$tmp"
bash "\$tmp" --self-test >/dev/null || { echo "candidate failed self-test" >&2; exit 2; }
install -m 0755 "\$tmp" /usr/local/bin/tak-health; rm -f "\$tmp"
/usr/local/bin/tak-health --version
EOF

# The box's own facts, recorded once at enrolment so the profile's defaults never
# have to fit every box of that role (1.5.0): the checker reads this before running.
if [[ "$RHOST" =~ ^[A-Za-z0-9-]{1,63}$ || -n "$FQDN" ]]; then
  echo "[1b/6] /etc/tak-health.conf (fqdn='${FQDN:-}' hostmatch='${RHOST}' profile='${PROFILE}')"
  $SSH "$RSUDO bash -s" <<EOF
set -euo pipefail
{
  echo "# written by console-enrol-server $(date -u +%Y-%m-%dT%H:%M:%SZ); the checker reads FQDN= and HOSTMATCH="
  [[ -n "$FQDN" ]] && echo "FQDN=$FQDN"
  [[ "$RHOST" =~ ^[A-Za-z0-9-]+\$ ]] && echo "HOSTMATCH=$RHOST"
  echo "PROFILE=$PROFILE"
  true
} > /etc/tak-health.conf
chmod 0644 /etc/tak-health.conf
EOF
fi

# The declared loadout (Spec 002, ADR 004) lives in ITS OWN file so this script's
# wholesale rewrite of tak-health.conf above can never clobber it. Seeding rules:
# an explicit --components always writes (the build knows what it installed); with
# no flag an existing declaration is left exactly as the operator set it.
if [[ -n "$COMPONENTS" ]]; then
  echo "[1c/6] /etc/tak-health.d/loadout.conf (components='${COMPONENTS}')"
  $SSH "$RSUDO bash -s" <<EOF
set -euo pipefail
install -d -m 0755 /etc/tak-health.d
{
  echo "# written by console-enrol-server $(date -u +%Y-%m-%dT%H:%M:%SZ); edited by the gated loadout action"
  echo "COMPONENTS=$COMPONENTS"
} > /etc/tak-health.d/loadout.conf
chmod 0644 /etc/tak-health.d/loadout.conf
EOF
fi

# ---------------------------------------------------------------- remote: users + scripts
echo "[2/6] users and action scripts"
tar -C /usr/local/bin -cf - $(for k in "${!ACTION_SCRIPTS[@]}"; do s="${ACTION_SCRIPTS[$k]}"; printf '%s ' "$s"; [[ -z "${NO_PRIV[$s]:-}" ]] && printf '%s-priv ' "$s"; done) "${EXTRA_SCRIPTS[@]}" \
  | $SSH "$RSUDO tar -C /usr/local/bin -xf -"
$SSH "$RSUDO bash -s" <<'EOF'
set -euo pipefail
useradd --system --create-home --shell /bin/bash takwatch 2>/dev/null || true
useradd --system --create-home --shell /bin/bash takadmin 2>/dev/null || true
install -d -m 700 -o takwatch -g takwatch /home/USER/.ssh
install -d -m 700 -o takadmin -g takadmin /home/USER/.ssh
touch /home/USER/.ssh/authorized_keys /home/USER/.ssh/authorized_keys
chown takwatch:takwatch /home/USER/.ssh/authorized_keys; chmod 600 /home/USER/.ssh/authorized_keys
chown takadmin:takadmin /home/USER/.ssh/authorized_keys; chmod 600 /home/USER/.ssh/authorized_keys
EOF

# ---------------------------------------------------------------- remote: sudoers + extras
echo "[3/6] sudoers, qrencode, enrol host"
{
  echo "# console actions for the Vantage estate console - written by console-enrol-server"
  for k in "${!ACTION_SCRIPTS[@]}"; do
    s="${ACTION_SCRIPTS[$k]}"
    [[ -n "${NO_PRIV[$s]:-}" ]] || echo "takadmin ALL=(root) NOPASSWD: /usr/local/bin/$s-priv"
  done
} | $SSH "$RSUDO tee /etc/sudoers.d/milux-actions >/dev/null && $RSUDO chmod 440 /etc/sudoers.d/milux-actions && $RSUDO visudo -c >/dev/null && echo 'sudoers OK'"
$SSH "$RSUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -q qrencode >/dev/null 2>&1 && echo 'qrencode OK' || echo 'qrencode NOT installed (offline box?) - enrol-device will refuse politely'"
[[ -n "$ENROL_HOST" ]] && printf '%s\n' "$ENROL_HOST" | $SSH "$RSUDO tee /etc/tak-enrol-host >/dev/null && echo 'enrol host OK'"

# ---------------------------------------------------------------- remote: keys
echo "[4/6] authorized_keys (idempotent)"
# Each side creates the directory it writes into before writing. Step 2 makes these too,
# but a step that appends into a path it has not ensured is a step that fails silently:
# on 2 Sep 2026 a box came out of enrolment with no /home/USER/.ssh at all, every
# append discarded, and this script printed "keys OK" underneath it because the echo was
# unconditional. The build then died three phases later on "Permission denied", pointing
# at sshd instead of here.
{
  echo "$HEALTH_LINE"
} | $SSH "$RSUDO bash -c 'install -d -m 700 -o takwatch -g takwatch /home/USER/.ssh; touch /home/USER/.ssh/authorized_keys; while IFS= read -r line; do grep -qF \"\$line\" /home/USER/.ssh/authorized_keys || echo \"\$line\" >> /home/USER/.ssh/authorized_keys; done; chown takwatch:takwatch /home/USER/.ssh/authorized_keys; chmod 600 /home/USER/.ssh/authorized_keys'" \
  || die "could not write takwatch's authorized_keys"
for k in "${!ACTION_SCRIPTS[@]}"; do
  # An unreadable or empty .pub used to produce 'command="...",restrict ' with NOTHING
  # after it. sshd skips a keyless line in silence, the count below still saw a non-empty
  # line, and enrolment reported success over an authorized_keys file in which not one
  # key could authenticate. The failure then surfaced two phases later as a bare
  # "Permission denied", pointing at sshd.
  [[ -s "$KEYDIR/$k.pub" ]] || die "$KEYDIR/$k.pub is missing or empty - this console's action keys are incomplete, so re-run the console installer here before enrolling anything"
  kp=$(cat "$KEYDIR/$k.pub") || die "cannot read $KEYDIR/$k.pub (run this as root or as vantage-console)"
  [[ "$kp" == ssh-* ]] || die "$KEYDIR/$k.pub does not look like a public key"
  printf 'command="/usr/local/bin/%s",restrict %s\n' "${ACTION_SCRIPTS[$k]}" "$kp"
done | $SSH "$RSUDO bash -c 'install -d -m 700 -o takadmin -g takadmin /home/USER/.ssh; touch /home/USER/.ssh/authorized_keys; while IFS= read -r line; do grep -qF \"\$line\" /home/USER/.ssh/authorized_keys || echo \"\$line\" >> /home/USER/.ssh/authorized_keys; done; chown takadmin:takadmin /home/USER/.ssh/authorized_keys; chmod 600 /home/USER/.ssh/authorized_keys'" \
  || die "could not write takadmin's authorized_keys"

# Earned, not announced. Ask the box what it actually has, and refuse to continue if the
# keys are not there - the next three phases all authenticate as these users, and a build
# that stops here with the reason is worth more than one that stops later without it.
# grep -c . counted LINES. A line reading 'command="...",restrict ' with no key on the
# end is a line, and 36 of them counted as 36 keys. Count lines carrying an actual key.
KEYCOUNT=$($SSH "$RSUDO bash -c 'w=0; a=0; [ -s /home/USER/.ssh/authorized_keys ] && w=\$(grep -c \"ssh-[a-z0-9-]* AAAA\" /home/USER/.ssh/authorized_keys); [ -s /home/USER/.ssh/authorized_keys ] && a=\$(grep -c \"ssh-[a-z0-9-]* AAAA\" /home/USER/.ssh/authorized_keys); echo \"\$w \$a\"'" 2>/dev/null || echo "0 0")
read -r WKEYS AKEYS <<<"$KEYCOUNT"
[[ "${WKEYS:-0}" -ge 1 ]] || die "takwatch has no authorized_keys on the box - enrolment cannot have worked"
[[ "${AKEYS:-0}" -ge 1 ]] || die "takadmin has no authorized_keys on the box - enrolment cannot have worked"
echo "keys OK (takwatch $WKEYS, takadmin $AKEYS)"

# ---------------------------------------------------------------- local: configs
echo "[5/6] console configs (locked)"
exec 9>/etc/vantage-console/.enrol.lock
flock 9
python3 - "$NAME" "$LABEL" "$HOST" "$PROFILE" "$EXPECTED_OFFLINE" <<'PY'
import json, sys
name, label, host, profile, off = sys.argv[1:6]
p = "/etc/vantage-console/targets.json"
c = json.load(open(p))
c["targets"] = [t for t in c["targets"] if t.get("name") != name]
c["targets"].append({"name": name, "label": label, "kind": "ssh",
                     "ssh": f"takwatch@{host}",
                     "identity": "/var/lib/vantage-console/.ssh/id_ed25519",
                     "profile": profile, "expected_offline": off == "true"})
json.dump(c, open(p, "w"), indent=2)
p = "/etc/vantage-console/actions.json"
c = json.load(open(p))
c.setdefault("targets", {})[name] = f"takadmin@{host}"
json.dump(c, open(p, "w"), indent=2)
print("configs OK")
PY

# ---------------------------------------------------------------- prove it
echo "[6/6] proof"
# best-effort: the timer collects within minutes anyway, and inside the console's
# sandbox systemctl may be refused - that must not fail the enrolment
systemctl start vantage-console-collect.service 2>/dev/null || true
H=$(sudo -H -u vantage-console ssh -i "$KEYDIR/id_ed25519" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    "takwatch@$HOST" ignored 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['result'], d['version'])" 2>/dev/null || echo "HEALTH-FAIL")
case "$H" in FAIL*|HEALTH-FAIL*) H="$H   (expected before TAK Server is installed - the checker is honest, the box is empty)" ;; esac
# the boundary check EXPECTS a refusal, so the remote exits non-zero; under
# pipefail that must not kill the script - the refusal text is the pass
B=$(sudo -H -u vantage-console ssh -i "$KEYDIR/id_action_logs" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    "takadmin@$HOST" "wrong-verb x" 2>&1 | grep -v "known hosts" | head -1 || true)
echo "health check:   $H"
# A refusal from the forced command is the pass. "Permission denied (publickey)" is NOT:
# it means no action key authenticates as takadmin at all, and every phase after this one
# connects as takadmin. This printed the auth failure and called it CORRECT, so enrolment
# announced success and phase 3 died on the same denial with nothing to point at.
case "$B" in
  *"Permission denied"*|*"Too many authentication failures"*|*"Host key verification failed"*)
    echo "boundary check: $B"
    die "no action key authenticates as takadmin@$HOST, so provisioning cannot run. The keys were written but sshd will not accept them: check that /home/USER and its .ssh are owned by takadmin, that authorized_keys lines carry a key after 'restrict', and that sshd_config does not exclude takadmin (AllowUsers/AllowGroups)." ;;
  *) echo "boundary check: $B   (an ERR refusal here is CORRECT)" ;;
esac
echo
echo "'$NAME' is enrolled. It appears on the console at the next poll; its software"
echo "inventory is judged against the baseline, and its server page carries its actions."
