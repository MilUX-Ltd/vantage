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
# Piping a tar stream into ssh + sudo is the same channel that silently delivered nothing
# on edge-laptop1, and an empty tar stream unpacks to no scripts while reporting success.
# The heredoc into `bash -s` is the channel that demonstrably carries on this estate, so
# the payload rides in it, and the box is asked afterwards what it actually has.
SCRIPT_LIST=$(for k in "${!ACTION_SCRIPTS[@]}"; do s="${ACTION_SCRIPTS[$k]}"; printf '%s ' "$s"; [[ -z "${NO_PRIV[$s]:-}" ]] && printf '%s-priv ' "$s"; done)
# tak-enrol-device-priv shells out to this to build the enrolment data package, so it
# has to travel with the action scripts or the package is silently never built.
EXTRA_SCRIPTS+=(build-enrolment-package.py)
# shellcheck disable=SC2086
SCRIPTS_B64=$(tar -C /usr/local/bin -cf - $SCRIPT_LIST "${EXTRA_SCRIPTS[@]}" | base64 | tr -d '\n')
$SSH "$RSUDO bash -s" <<EOF || die "could not unpack the action scripts on the box"
set -euo pipefail
echo $SCRIPTS_B64 | base64 -d | tar -C /usr/local/bin -xf -
EOF
WANT_SCRIPTS=$(printf '%s' "$SCRIPT_LIST" | wc -w | tr -d ' ')
GOT_SCRIPTS=$($SSH "$RSUDO bash -c 'n=0; for f in $SCRIPT_LIST; do [ -x /usr/local/bin/\$f ] && n=\$((n+1)); done; echo \$n'" 2>/dev/null | head -1 | tr -dc '0-9')
[[ "${GOT_SCRIPTS:-0}" -ge "$WANT_SCRIPTS" ]] \
  || die "only ${GOT_SCRIPTS:-0} of $WANT_SCRIPTS action scripts are on the box. The copy reported success and did not happen, so every action would fail with a missing command."
echo "  action scripts: $GOT_SCRIPTS on the box"
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
# tee with nothing on stdin writes an EMPTY sudoers file, and `visudo -c` passes an empty
# file, so this printed "sudoers OK" over a file granting nothing. Built here, sent in the
# heredoc, and counted back off the box.
SUDOERS=$(
  echo "# console actions for the Vantage estate console - written by console-enrol-server"
  for k in "${!ACTION_SCRIPTS[@]}"; do
    s="${ACTION_SCRIPTS[$k]}"
    [[ -n "${NO_PRIV[$s]:-}" ]] || echo "takadmin ALL=(root) NOPASSWD: /usr/local/bin/$s-priv"
  done
)
WANT_SUDO=$(printf '%s\n' "$SUDOERS" | grep -c '^takadmin ALL=' || true)
[[ "$WANT_SUDO" -ge 1 ]] || die "built an empty sudoers block - refusing to write one"
SUDOERS_B64=$(printf '%s\n' "$SUDOERS" | base64 | tr -d '\n')
$SSH "$RSUDO bash -s" <<EOF || die "could not write /etc/sudoers.d/milux-actions"
set -euo pipefail
echo $SUDOERS_B64 | base64 -d > /etc/sudoers.d/milux-actions
chmod 440 /etc/sudoers.d/milux-actions
visudo -c >/dev/null
EOF
GOT_SUDO=$($SSH "$RSUDO grep -c '^takadmin ALL=' /etc/sudoers.d/milux-actions 2>/dev/null || true" | head -1 | tr -dc '0-9')
[[ "${GOT_SUDO:-0}" -ge "$WANT_SUDO" ]] \
  || die "/etc/sudoers.d/milux-actions holds ${GOT_SUDO:-0} rules on the box, expected $WANT_SUDO. It reported success over a file that grants nothing."
echo "sudoers OK ($GOT_SUDO rules)"
$SSH "$RSUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -q qrencode >/dev/null 2>&1 && echo 'qrencode OK' || echo 'qrencode NOT installed (offline box?) - enrol-device will refuse politely'"
if [[ -n "$ENROL_HOST" ]]; then
  # Same class as the key write: this was data on stdin through ssh + sudo, into tee. tee
  # with no input writes an empty file and still reports success, so the box would carry an
  # empty enrol host and say "enrol host OK" over the top of it. Sent as an argument, then
  # read back and compared.
  ENROL_HOST_B64=$(printf '%s\n' "$ENROL_HOST" | base64 | tr -d '\n')
  $SSH "$RSUDO bash -c 'echo $ENROL_HOST_B64 | base64 -d > /etc/tak-enrol-host'" \
    || die "could not write /etc/tak-enrol-host on the box"
  [[ "$($SSH "$RSUDO cat /etc/tak-enrol-host" 2>/dev/null | tr -d '\r\n')" == "$ENROL_HOST" ]] \
    || die "/etc/tak-enrol-host on the box does not hold '$ENROL_HOST' after writing it"
  echo "enrol host OK"
fi

# ---------------------------------------------------------------- remote: keys
echo "[4/6] authorized_keys (idempotent)"
# The keys used to be piped into a remote `while read` loop over ssh + sudo. On
# edge-laptop1 that pipe delivered NOTHING: takadmin's authorized_keys came out of
# enrolment 0 bytes, created by the touch and never appended to, and sshd then answered
# every action key with "Connection closed ... [preauth]" because the file was empty.
# Nothing in the chain noticed. So the payload no longer travels on stdin at all: it is
# built and checked here, sent as a base64 argument, and counted back off the box.
#
# The block is also built into a variable BEFORE it is sent. It used to be generated
# inside `for ... done | ssh`, which puts it in a subshell, where `die` exits the subshell
# and the script carries on regardless.
build_keyblock() {
    local k kp out=""
    for k in "${!ACTION_SCRIPTS[@]}"; do
        [[ -s "$KEYDIR/$k.pub" ]] || die "$KEYDIR/$k.pub is missing or empty - this console's action keys are incomplete, so re-run the console installer here before enrolling anything"
        kp=$(cat "$KEYDIR/$k.pub") || die "cannot read $KEYDIR/$k.pub (run this as root or as vantage-console)"
        [[ "$kp" == ssh-* ]] || die "$KEYDIR/$k.pub does not look like a public key"
        out+="command=\"/usr/local/bin/${ACTION_SCRIPTS[$k]}\",restrict $kp"$'\n'
    done
    printf '%s' "$out"
}

# user, then the lines to merge in. Sent as one base64 token so no data crosses stdin.
install_authkeys() {
    local u="$1" block="$2" want got b64
    want=$(printf '%s' "$block" | grep -c 'ssh-[a-z0-9-]* AAAA' || true)
    [[ "$want" -ge 1 ]] || die "built no key lines for $u - refusing to write an empty authorized_keys"
    # printf '%s' writes NO trailing newline, and `read` discards a final line that has
    # none: a single-line block (takwatch's is one line) therefore decoded to 95 bytes and
    # the loop ran zero times, leaving the file empty. Terminate it here AND accept an
    # unterminated last line below, so neither end depends on the other being right.
    b64=$(printf '%s\n' "$block" | base64 | tr -d '\n')
    $SSH "$RSUDO bash -c 'set -e
        install -d -m 700 -o $u -g $u /home/$u/.ssh
        touch /home/$u/.ssh/authorized_keys
        echo $b64 | base64 -d | while IFS= read -r line || [ -n \"\$line\" ]; do
            [ -n \"\$line\" ] || continue
            grep -qF \"\$line\" /home/$u/.ssh/authorized_keys || printf \"%s\\n\" \"\$line\" >> /home/$u/.ssh/authorized_keys
        done
        chown $u:$u /home/$u/.ssh/authorized_keys
        chmod 600 /home/$u/.ssh/authorized_keys'" \
        || die "could not write $u's authorized_keys"
    # Count what is actually on the box, and match it against what we meant to put there.
    # grep -c prints 0 AND exits 1 when it matches nothing, so `|| echo 0` appended a
    # SECOND line and [[ "0\n0" -ge 1 ]] was a syntax error rather than a clear refusal.
    got=$($SSH "$RSUDO grep -c 'ssh-[a-z0-9-]* AAAA' /home/$u/.ssh/authorized_keys 2>/dev/null || true" | head -1 | tr -dc '0-9')
    [[ "${got:-0}" -ge "$want" ]] \
        || die "$u's authorized_keys holds ${got:-0} keys on the box, expected at least $want. The write reported success and did not happen; nothing after this could authenticate as $u."
    echo "  $u: $got keys on the box"
}

install_authkeys takwatch "$HEALTH_LINE"
install_authkeys takadmin "$(build_keyblock)"
echo "keys OK"

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
