#!/usr/bin/env bash
#
# tak-health.sh - one read-only health check for every MilUX TAK server.
#
# Replaces the two divergent scripts:
#   servers/nuc/scripts/tak-health.sh   (credential-free discipline, on-the-wire cert reads)
#   servers/deployed/verify-kit.sh      (output-based checks, real handshakes, tile counts)
#
# Principle, from the lessons log lesson 1: check what a service PRODUCES, not whether
# systemd says it is active. An mbtileserver serving an empty directory is active.
# A mediamtx with every port firewalled off is active. Neither is working.
#
# Deliberately requires NO credentials and NO sudo. It does not read CoreConfig.xml,
# any keystore, cert-metadata.sh, or the CA. Certificate expiry is read off the wire.
# Safe for Sam (OpenClaw) to run on any box, as often as it likes.
#
# Runs LOCALLY on the box it is checking. Remote execution and aggregation are the
# console's job, not this script's.
#
#     bash tak-health.sh --profile firmbase
#     bash tak-health.sh --profile deployed --json
#     bash tak-health.sh --self-test          # lesson 2: check the checker
#
# Exit: 0 all OK, 1 warnings only, 2 one or more failures.
#
# MilUX, 2026-08-23.

set -uo pipefail

# Version is EMBEDDED, not read from a file, because this script is copied
# standalone to /usr/local/bin on three servers. Repo VERSION and CHANGELOG.md
# carry the same number; bump both together.
#
# This exists because after five pushes in one afternoon there was no way to tell
# which version was actually running on which box, and the deployable kit is away with the
# first one on it. A checker you cannot version is a checker you cannot trust
# across an estate.
VERSION="1.12.0"
SCHEMA="milux.tak-health/2"
PROFILE=""
JSON=0
# Mesh heartbeat (Spec 001): populated only when the gateway unit exists on this box.
MESH_GATEWAY=""
MESH_RADIO=""
MESH_RADIO_PRESENT=false
MESH_LAST=""
MESH_NODES=0
FORCE=0
SELFTEST=0
OVERRIDE_FQDN=""
OVERRIDE_HOSTMATCH=""
OVERRIDE_PROBE=""
OVERRIDE_TSCERT=""
CONF_FILE="${TAK_HEALTH_CONF:-/etc/tak-health.conf}"

# The declared loadout (Spec 002, ADR 004): what THIS box actually carries. Its own
# file with one writer per file (enrolment seeds it, the gated loadout action edits
# it), so enrolment's wholesale rewrite of tak-health.conf can never clobber it.
LOADOUT_FILE="${TAK_HEALTH_LOADOUT:-/etc/tak-health.d/loadout.conf}"
LOADOUT=""              # validated csv; empty = no declaration in force
LOADOUT_SOURCE="none"   # none | loadout.conf | rejected (reason)
LOADOUT_TILESETS=""     # TILESETS= line from the loadout file
P_SKIPPED_SERVICES=""   # profile-expected services a declaration switched off
PRINTPROFILE=0

# ---------------------------------------------------------------------------
# Results collection
# ---------------------------------------------------------------------------
CHK_CAT=(); CHK_NAME=(); CHK_STATUS=(); CHK_DETAIL=(); CHK_VALUE=(); CHK_UNIT=()
N_OK=0; N_WARN=0; N_FAIL=0; N_SKIP=0

# Software inventory (1.1.0). Separate from checks on purpose: a check carries a
# verdict, an inventory row carries a fact. "mediamtx v1.20.0, active" is not OK
# or FAIL by itself; whether it SHOULD be v1.20.0 is the console's judgement,
# made against the estate's desired-state manifest, not this box's.
SW_NAME=(); SW_VERSION=(); SW_STATE=()

sw_record() { # name version state
    SW_NAME+=("$1"); SW_VERSION+=("$2"); SW_STATE+=("$3")
}

record() { # category name status detail [value] [unit]
    # value/unit are the schema/2 addition: numbers as NUMBERS, so the console can
    # store and trend them. Parsing free text back out of detail is fragile and
    # was already breaking (handover, item 1).
    CHK_CAT+=("$1"); CHK_NAME+=("$2"); CHK_STATUS+=("$3"); CHK_DETAIL+=("$4")
    CHK_VALUE+=("${5:-}"); CHK_UNIT+=("${6:-}")
    case "$3" in
        OK)   N_OK=$((N_OK+1))   ;;
        WARN) N_WARN=$((N_WARN+1)) ;;
        FAIL) N_FAIL=$((N_FAIL+1)) ;;
        SKIP) N_SKIP=$((N_SKIP+1)) ;;
    esac
}

# ---------------------------------------------------------------------------
# Portability helpers
#
# the lessons log lesson 11: macOS is not Linux, and these scripts run on both.
# `date -d` is GNU only; BSD date needs -j -f. --self-test must run on Matt's Air.
# `bc` is not installed everywhere, so all arithmetic is bash or awk.
# ---------------------------------------------------------------------------
to_epoch() { # <date string> -> epoch seconds, or empty
    local s="$1" e=""
    e=$(date -d "$s" +%s 2>/dev/null) && { printf '%s' "$e"; return 0; }
    e=$(date -j -f "%b %d %T %Y %Z" "$s" +%s 2>/dev/null) && { printf '%s' "$e"; return 0; }
    e=$(date -j -f "%b %e %T %Y %Z" "$s" +%s 2>/dev/null) && { printf '%s' "$e"; return 0; }
    return 1
}

days_until() { # <date string> -> whole days from now, or empty
    local target now
    target=$(to_epoch "$1") || return 1
    now=$(date +%s)
    printf '%s' "$(( (target - now) / 86400 ))"
}

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
    s="${s//$'\t'/\\t}"; s="${s//$'\r'/\\r}"; s="${s//$'\n'/\\n}"
    printf '%s' "$s"
}

# ---------------------------------------------------------------------------
# JUDGES - pure functions, no I/O. These are what --self-test exercises.
#
# Every judgement in this script goes through one of these. That is the whole
# point: the lessons log lesson 2 records four "faults" on 8 August that were bugs in
# the checking code, not the system. A checker nobody has watched fail is not a
# checker.
# ---------------------------------------------------------------------------

judge_service() { # <systemctl-state> -> OK|FAIL
    [[ "$1" == "active" ]] && echo OK || echo FAIL
}

judge_enabled() { # <systemctl-is-enabled> -> OK|WARN
    [[ "$1" == "enabled" || "$1" == "static" || "$1" == "indirect" ]] && echo OK || echo WARN
}

judge_port() { # <listening 0|1> -> OK|FAIL
    [[ "$1" == "1" ]] && echo OK || echo FAIL
}

judge_cert_days() { # <days> -> OK|WARN|FAIL
    local d="$1"
    [[ -z "$d" ]] && { echo FAIL; return; }
    if   (( d < 0 ));  then echo FAIL
    elif (( d < 30 )); then echo WARN
    else echo OK; fi
}

judge_ts_cert_days() { # <days> -> OK|WARN|FAIL  (tailscale certs are 90d, warn earlier)
    local d="$1"
    [[ -z "$d" ]] && { echo FAIL; return; }
    if   (( d < 0 ));  then echo FAIL
    elif (( d < 21 )); then echo WARN
    else echo OK; fi
}

judge_disk_pct() { # <percent-used> -> OK|WARN|FAIL
    local p="$1"
    [[ -z "$p" ]] && { echo WARN; return; }
    if   (( p >= 90 )); then echo FAIL
    elif (( p >= 80 )); then echo WARN
    else echo OK; fi
}

judge_tilesets() { # <count> <expected> -> OK|WARN|FAIL
    local c="$1" e="$2"
    [[ -z "$c" ]] && { echo FAIL; return; }
    if   (( c == 0 ));  then echo FAIL
    elif (( c < e ));   then echo WARN
    else echo OK; fi
}

judge_firewall() { # <anywhere-rule-found 0|1> <public-expected 0|1> -> OK|FAIL
    local found="$1" public="$2"
    if [[ "$public" == "1" ]]; then
        [[ "$found" == "1" ]] && echo OK || echo FAIL
    else
        [[ "$found" == "1" ]] && echo FAIL || echo OK
    fi
}

judge_error_count() { # <n> -> OK|WARN
    # A raw count of ERROR log lines is a weak health signal: a working TAK server
    # logs hundreds over 48h (every client drop, every malformed CoT from a flaky
    # radio). What matters is the TREND, which the console draws from this value's
    # sparkline, not the absolute number. So this is OK by default and carries the
    # count as context; it warns only at a flood that means something is genuinely
    # looping. Reclassified 25 Aug 2026 after 383-546/48h read as three false WARNs
    # across a healthy estate: a monitor whose alarm is always on gets ignored, and
    # then it is useless on the day something is actually wrong.
    local n="$1"
    [[ -z "$n" ]] && { echo OK; return; }   # unreadable is not a fault
    (( n > 2000 )) && echo WARN || echo OK
}

judge_handshake_failures() { # <n> [public 0|1] -> OK|WARN
    # Rejecting a bad client-cert handshake IS the server working, so never a FAIL.
    # On a PUBLIC endpoint it is not even a WARN: a server on the open internet is
    # scanned and probed continuously, and ~1400 rejected handshakes over 48h is
    # background noise, not a fault (cloud, 25 Aug 2026, read as a WARN it should
    # never have been). The count still rides in the JSON so the console can trend
    # it and show a genuine spike. On a private/tailnet box a sustained rate is
    # worth a look: nothing there should be failing handshakes, so a broken client
    # actually shows.
    local n="$1" public="${2:-0}"
    [[ -z "$n" ]] && { echo OK; return; }
    [[ "$public" == "1" ]] && { echo OK; return; }
    (( n >= 50 )) && echo WARN || echo OK
}

judge_source_spread() { # <distinct sources> [public 0|1] -> OK|WARN
    # The SHAPE of handshake failures, not a server fault either way. On a PUBLIC
    # endpoint many distinct sources is the internet, and expected (OK). On a private
    # box the count weakly hints at one broken client vs several: a domestic or mobile
    # client gets a fresh address per session, so two or three addresses over 48h fit
    # ONE device in a retry loop (cloud, 23 Aug 2026: 31.94.0.0/16 turned out to be
    # AS2856 BT-UK-AS, an ordinary client, not a scan). Threshold deliberately high,
    # and never a verdict on a public box.
    local n="$1" public="${2:-0}"
    [[ -z "$n" ]] && { echo OK; return; }
    [[ "$public" == "1" ]] && { echo OK; return; }
    (( n >= 5 )) && echo WARN || echo OK
}

judge_firewall_observed() { # <observable 0|1> <found 0|1> <public 0|1> -> SKIP|OK|FAIL
    # THE RULE THIS ENCODES: never derive a verdict from a source you could not
    # read. "I could not check" is not "it is wrong".
    #
    # Found live on 23 Aug 2026. Run as root the cloud profile passed; run as the
    # unprivileged `takwatch` the console uses, `ufw status` returns "You need to
    # be root", the grep matched nothing, and a cloud server whose public rule was
    # perfectly fine reported FAIL. Third instance of the same class of bug in one
    # day, after the hostname guard and the file-mtime "bound".
    [[ "$1" == "1" ]] || { echo SKIP; return; }
    judge_firewall "$2" "$3"
}

judge_takbot_workers() { # <observable 0|1> <workers-found 0|1> -> SKIP|OK|FAIL
    # Same rule. An unprivileged user not in systemd-journal reads an empty
    # journal, which is indistinguishable from a bot that never started unless
    # observability is checked separately.
    [[ "$1" == "1" ]] || { echo SKIP; return; }
    [[ "$2" == "1" ]] && echo OK || echo FAIL
}

judge_auth_failures() { # <n> -> OK|WARN
    local n="$1"
    [[ -z "$n" ]] && { echo WARN; return; }
    (( n > 0 )) && echo WARN || echo OK
}

judge_http_code() { # <code> -> OK|FAIL   (404/302 still means the server is answering)
    case "$1" in 200|301|302|401|403|404) echo OK ;; *) echo FAIL ;; esac
}

judge_hostname() { # <actual> <expected-substring> -> OK|FAIL
    [[ "$1" == *"$2"* ]] && echo OK || echo FAIL
}

judge_guard_severity() { # <match OK|FAIL> <force 0|1> -> OK|WARN
    # A wrong profile makes the RESULT meaningless, but it is not a fault in the
    # server. This script writes nothing and touches nothing, so the guard warns;
    # it does not fail. Reporting a healthy box as FAIL because the operator typed
    # the wrong profile is precisely the lesson-2 error: a fault in the checker
    # presented as a fault in the system. (Found on the first live run, 23 Aug 2026,
    # when both healthy servers reported FAIL on a guessed hostname.)
    [[ "$1" == "OK" ]] && { echo OK; return; }
    [[ "$2" == "1" ]] && { echo OK; return; }
    echo WARN
}

# ---------------------------------------------------------------------------
# PROFILES
#
# Each profile is the smallest useful form of a per-server build manifest.
# If this abstraction survives contact, it is what a real manifest grows from.
# ---------------------------------------------------------------------------

load_profile() {
    # Profiles are named for the ROLE, not the hardware (1.5.0): the rear box is
    # "firmbase" whatever it runs on. "nuc" survives as an alias because every
    # deployed forced command and cron line says it; both names load the same
    # profile and the report says firmbase.
    local want="$1"
    [[ "$want" == "nuc" ]] && want="firmbase"
    case "$want" in
      cloud)
        P_DESC="Public cloud VPS, QR-join, Let's Encrypt"
        P_FQDN=""
        P_HOSTMATCH=""           # set per box by enrolment (/etc/tak-health.conf HOSTMATCH=)
        P_PROBE="127.0.0.1"
        P_SERVICES="takserver postgresql"
        P_TCP="8089 8443 8446"
        P_UDP=""
        P_PUBLIC=1          # TAK ports SHOULD be open to Anywhere here
        P_LETSENCRYPT=1
        P_TILESETS=0
        P_MEDIA=0
        P_CHRONY=0
        P_TAKBOT=0
        P_TSCERT=""
        P_LVMSNAP=""
        ;;
      firmbase)
        P_DESC="Rear firmbase box, private network only"
        P_FQDN=""
        P_HOSTMATCH=""
        P_PROBE=""
        P_SERVICES="takserver postgresql"
        P_TCP="8089 8443 8446"
        P_UDP=""
        P_PUBLIC=0          # an Anywhere rule here is a security FAILURE
        P_LETSENCRYPT=0
        P_TILESETS=0
        P_MEDIA=0
        P_CHRONY=0
        P_TAKBOT=0
        P_TSCERT=""
        P_LVMSNAP="/dev/ubuntu-vg/tak-preinstall"
        ;;
      deployed)
        P_DESC="Deployable kit, must work with the cable out"
        P_FQDN=""
        P_HOSTMATCH=""                 # set per box by enrolment (/etc/tak-health.conf HOSTMATCH=)
        P_PROBE="127.0.0.1"
        P_SERVICES="takserver mediamtx mbtileserver chrony postgresql takbot mosquitto node-red ollama tailscaled docker $(mesh_unit)"
        P_TCP="8089 8443 8446 1935 8554 8888 8080"
        P_UDP="123"
        P_PUBLIC=0
        P_LETSENCRYPT=0
        # AS-BUILT.md records six tilesets; verify-kit.sh expected 5. Set to 6,
        # flagged on an internal card for confirmation when the kit is back from the field.
        P_TILESETS=6
        P_MEDIA=1
        P_CHRONY=1
        P_TAKBOT=1
        P_TSCERT=""
        P_LVMSNAP=""
        ;;
      *) return 1 ;;
    esac
    apply_overrides
    apply_loadout
    return 0
}

# Per-box facts beat the profile's defaults (1.5.0). A profile describes the ROLE;
# ---------------------------------------------------------------------------
# The component map (Spec 002). Operator-supplied tokens are only ever map KEYS,
# validated [a-z]+ and matched against these cases; they are never interpolated
# into P_SERVICES or any command line. P_SERVICES is word-split by design, so
# nothing operator-typed may reach it except through this map's fixed outputs.
# The token/unit translation (nodered -> node-red) lives here and only here.
# ---------------------------------------------------------------------------
# The mesh radio is owned by ONE unit: Mesh Manager's bridge where Mesh Manager is installed
# (ADR-005; it stops and disables the old gateway and keeps that unit file as the rollback),
# the the development repository gateway otherwise. Judging the old unit on a box that runs the bridge reported
# a FAIL for a service that was stopped on purpose (3 Sep 2026, the first real install).
mesh_unit() {
    if systemctl cat mesh-manager-bridge >/dev/null 2>&1; then echo "mesh-manager-bridge"; else echo "tak-meshtastic-gateway"; fi
}
component_services() { # token -> service names, or rc 1 for an unknown token
    case "$1" in
      takserver) echo "takserver postgresql" ;;
      cloudtak)  echo "docker" ;;
      mediamtx)  echo "mediamtx" ;;
      maps)      echo "mbtileserver" ;;
      takbot)    echo "takbot" ;;
      mosquitto) echo "mosquitto" ;;
      nodered)   echo "node-red" ;;
      ollama)    echo "ollama" ;;
      lanntp)    echo "chrony" ;;
      syncthing) echo "syncthing@vaultsync" ;;
      mesh)      mesh_unit ;;
      tailscale) echo "tailscaled" ;;
      docker)    echo "docker" ;;
      # infra-TAK (takwerx) is a stack CHOICE alongside the MilUX TAK Server: its own
      # marketplace deploys the TAK ecosystem above it, so what this box owes us is the
      # platform itself, on docker. What the marketplace puts on top is infra-TAK's
      # business, not ours; a box running it declares infratak INSTEAD of takserver.
      infratak)  echo "docker" ;;
      *) return 1 ;;
    esac
}
component_tcp() { # token -> TCP ports it must answer on
    case "$1" in
      takserver) echo "8089 8443 8446" ;;
      infratak)  echo "5001" ;;
      mediamtx)  echo "1935 8554 8888" ;;
      maps)      echo "8080" ;;
      *) echo "" ;;
    esac
}
component_udp() { case "$1" in lanntp) echo "123" ;; *) echo "" ;; esac }

load_loadout() {
    LOADOUT=""; LOADOUT_SOURCE="none"; LOADOUT_TILESETS=""
    [[ -r "$LOADOUT_FILE" ]] || return 0
    local line tok ts
    line=$(grep -m1 '^COMPONENTS=' "$LOADOUT_FILE" 2>/dev/null | cut -d= -f2-)
    [[ -z "$line" ]] && return 0
    # An invalid declaration is rejected AS A WHOLE, loudly, and the profile
    # defaults stay in force: silently dropping one bad token would be silent
    # narrowing, the thing AC3 exists to prevent.
    if [[ ! "$line" =~ ^[a-z]+(,[a-z]+)*$ ]]; then
        LOADOUT_SOURCE="rejected (malformed COMPONENTS line)"
        return 0
    fi
    for tok in ${line//,/ }; do
        if ! component_services "$tok" >/dev/null 2>&1; then
            LOADOUT=""
            LOADOUT_SOURCE="rejected (unknown component '$tok')"
            return 0
        fi
        case ",$LOADOUT," in *",$tok,"*)
            LOADOUT=""
            LOADOUT_SOURCE="rejected (duplicate component '$tok')"
            return 0 ;;
        esac
        LOADOUT="${LOADOUT:+$LOADOUT,}$tok"
    done
    ts=$(grep -m1 '^TILESETS=' "$LOADOUT_FILE" 2>/dev/null | cut -d= -f2-)
    [[ "$ts" =~ ^[0-9]{1,3}$ ]] && LOADOUT_TILESETS="$ts"
    LOADOUT_SOURCE="loadout.conf"
}

apply_loadout() {
    # Narrow the profile's expectations to the declared loadout (ADR 004). A
    # rejected or absent declaration leaves the profile untouched: the deployable kit,
    # which declares nothing, behaves exactly as before this feature existed.
    # (Removal condition for that fallback: when every estate box carries a
    # declaration, the profile service lists shrink to takserver+postgresql
    # and this comment goes.)
    load_loadout
    [[ "$LOADOUT_SOURCE" == "loadout.conf" ]] || return 0
    local tok s p new_services="" new_tcp="" new_udp=""
    for tok in ${LOADOUT//,/ }; do
        for s in $(component_services "$tok"); do
            case " $new_services " in *" $s "*) ;; *) new_services="${new_services:+$new_services }$s" ;; esac
        done
        for p in $(component_tcp "$tok"); do
            case " $new_tcp " in *" $p "*) ;; *) new_tcp="${new_tcp:+$new_tcp }$p" ;; esac
        done
        for p in $(component_udp "$tok"); do
            case " $new_udp " in *" $p "*) ;; *) new_udp="${new_udp:+$new_udp }$p" ;; esac
        done
    done
    P_SKIPPED_SERVICES=""
    for s in $P_SERVICES; do
        case " $new_services " in *" $s "*) ;; *) P_SKIPPED_SERVICES="${P_SKIPPED_SERVICES:+$P_SKIPPED_SERVICES }$s" ;; esac
    done
    P_SERVICES="$new_services"; P_TCP="$new_tcp"; P_UDP="$new_udp"
    # Module checks: declared -> checked (1); profile-expected but undeclared ->
    # the skip state (2), reported as "not fitted (declared)", never silent (AC3);
    # not applicable to the profile at all -> 0, silent as always.
    case ",$LOADOUT," in
      *,mediamtx,*) P_MEDIA=1 ;;
      *) [[ "$P_MEDIA" == "1" ]] && P_MEDIA=2 || P_MEDIA=0 ;;
    esac
    case ",$LOADOUT," in
      *,lanntp,*) P_CHRONY=1 ;;
      *) [[ "$P_CHRONY" == "1" ]] && P_CHRONY=2 || P_CHRONY=0 ;;
    esac
    case ",$LOADOUT," in
      *,takbot,*) P_TAKBOT=1 ;;
      *) [[ "$P_TAKBOT" == "1" ]] && P_TAKBOT=2 || P_TAKBOT=0 ;;
    esac
    # Tilesets: the declared count, or the unasserted sentinel -1 (WARN at zero,
    # OK once tiles arrive: a fresh maps install awaiting content is not a fault);
    # -2 = maps declared off while the profile expected them (reported SKIP).
    case ",$LOADOUT," in
      *,maps,*) P_TILESETS="${LOADOUT_TILESETS:--1}" ;;
      *) [[ "$P_TILESETS" -gt 0 ]] && P_TILESETS=-2 || P_TILESETS=0 ;;
    esac
    return 0
}

# the box's own FQDN and hostname are recorded at enrolment in /etc/tak-health.conf,
# so a second cloud box does not warn forever for not being tak.example.com. The
# file is parsed, never sourced: only FQDN= and HOSTMATCH= lines are read, and only
# hostname-shaped values are accepted. CLI flags beat the file.
apply_overrides() {
    local v
    if [[ -r "$CONF_FILE" ]]; then
        v=$(grep -m1 '^FQDN=' "$CONF_FILE" 2>/dev/null | cut -d= -f2-)
        [[ "$v" =~ ^[A-Za-z0-9.-]{1,253}$ ]] && P_FQDN="$v"
        v=$(grep -m1 '^HOSTMATCH=' "$CONF_FILE" 2>/dev/null | cut -d= -f2-)
        [[ "$v" =~ ^[A-Za-z0-9-]{1,63}$ ]] && P_HOSTMATCH="$v"
    fi
    [[ -n "$OVERRIDE_FQDN" ]] && P_FQDN="$OVERRIDE_FQDN"
    [[ -n "$OVERRIDE_HOSTMATCH" ]] && P_HOSTMATCH="$OVERRIDE_HOSTMATCH"
    [[ -n "$OVERRIDE_PROBE" ]] && P_PROBE="$OVERRIDE_PROBE"
    [[ -n "$OVERRIDE_TSCERT" ]] && P_TSCERT="$OVERRIDE_TSCERT"
    return 0
}

# ---------------------------------------------------------------------------
# CHECKS - gather, then hand the value to a judge
# ---------------------------------------------------------------------------

check_hostname() {
    local actual; actual=$(hostname -s 2>/dev/null || echo unknown)
    local match; match=$(judge_hostname "$actual" "$P_HOSTMATCH")
    local sev;   sev=$(judge_guard_severity "$match" "$FORCE")
    if [[ "$sev" == "WARN" ]]; then
        record host guard WARN "on '$actual', profile '$PROFILE' expects '*${P_HOSTMATCH}*' - results below may be meaningless"
    elif [[ "$match" == "FAIL" ]]; then
        record host guard OK "$actual (guard overridden with --force)"
    else
        record host guard OK "$actual"
    fi
}

check_services() {
    local s state
    for s in $P_SERVICES; do
        state=$(systemctl is-active "$s" 2>/dev/null || true)
        [[ -z "$state" ]] && state="unknown"
        record service "$s" "$(judge_service "$state")" "$state"
    done
    state=$(systemctl is-enabled takserver 2>/dev/null | tail -1 || true)
    [[ -z "$state" ]] && state="unknown"
    record service "takserver@boot" "$(judge_enabled "$state")" "$state"
    # AC3: a declaration narrows expectations visibly, never silently.
    for s in $P_SKIPPED_SERVICES; do
        record service "$s" SKIP "not fitted (declared)"
    done
}

check_loadout() {
    case "$LOADOUT_SOURCE" in
        none)         record host "declared loadout" OK "profile defaults (no declaration)" ;;
        loadout.conf) record host "declared loadout" OK "$LOADOUT (loadout.conf)" ;;
        *)            record host "declared loadout" WARN "$LOADOUT_SOURCE - profile defaults in force" ;;
    esac
}

check_declared_vs_observed() {
    # "Fitted but declared away" must be visible (AC3): a component whose service
    # runs while the declaration excludes it is a monitoring hole the operator
    # opened; WARN it, with the service named. Shared substrates (docker, chrony,
    # postgresql) are excluded: other components legitimately keep them running.
    [[ "$LOADOUT_SOURCE" == "loadout.conf" ]] || return 0
    local tok s active
    for tok in mediamtx maps takbot mosquitto nodered ollama mesh; do
        case ",$LOADOUT," in *",$tok,"*) continue ;; esac
        for s in $(component_services "$tok"); do
            case "$s" in docker|chrony|postgresql) continue ;; esac
            active=$(systemctl is-active "$s" 2>/dev/null || true)
            [[ "$active" == "active" ]] && \
                record host "loadout drift" WARN "fitted but declared away: $s is active"
        done
    done
    return 0
}

check_ports() {
    local p listening
    for p in $P_TCP; do
        listening=0
        ss -ltn 2>/dev/null | grep -qE "[:.]${p}[[:space:]]" && listening=1
        record port "tcp/$p" "$(judge_port "$listening")" \
            "$([[ $listening == 1 ]] && echo listening || echo 'not listening')"
    done
    for p in $P_UDP; do
        listening=0
        ss -lun 2>/dev/null | grep -qE "[:.]${p}[[:space:]]" && listening=1
        record port "udp/$p" "$(judge_port "$listening")" \
            "$([[ $listening == 1 ]] && echo listening || echo 'not listening')"
    done
}

check_firewall() {
    if ! command -v ufw >/dev/null 2>&1; then
        record firewall ufw SKIP "ufw not installed"
        return
    fi
    local out observable=1 found=0
    out=$(ufw status 2>&1) || observable=0
    # ufw exits non-zero AND prints this when unprivileged; belt and braces.
    grep -qi 'need to be root\|permission denied' <<<"$out" && observable=0
    [[ -z "$out" ]] && observable=0
    grep -qE '^(8089|8443|8446).*ALLOW.*Anywhere' <<<"$out" && found=1

    local st; st=$(judge_firewall_observed "$observable" "$found" "$P_PUBLIC")
    if [[ "$st" == "SKIP" ]]; then
        record firewall "$([[ $P_PUBLIC == 1 ]] && echo 'public exposure' || echo 'tailnet scoping')" \
            SKIP "cannot read ufw unprivileged - run as root to verify this one"
    elif [[ "$P_PUBLIC" == "1" ]]; then
        record firewall "public exposure" "$st" \
            "$([[ $found == 1 ]] && echo 'TAK ports open to Anywhere (expected for cloud)' \
                                 || echo 'no Anywhere rule - public join will not work')"
    else
        record firewall "tailnet scoping" "$st" \
            "$([[ $found == 1 ]] && echo 'TAK ports open to Anywhere - expected tailscale0 only' \
                                 || echo 'no Anywhere rule for TAK ports')"
    fi
}

check_resolution() {
    if getent hosts "$P_FQDN" >/dev/null 2>&1; then
        record dns "$P_FQDN" OK "$(getent hosts "$P_FQDN" | awk '{print $1}' | head -1)"
    elif [[ "$PROFILE" == "deployed" ]]; then
        # a deployed box lives on an isolated mesh with no DNS of its own; its name not
        # resolving locally is expected there, not a fault. The estate console reaches it
        # by address and proves that separately, so DNS is not applicable - SKIP, never a
        # red FAIL that reddens a box that is actually up.
        record dns "$P_FQDN" SKIP "not applicable (deployed box - reachable by address)"
    else
        record dns "$P_FQDN" FAIL "does not resolve"
    fi
}

check_tls_handshake() {
    # lesson 1: a real handshake, not a listening socket. AND retry before failing:
    # a single-shot probe flaps. One slow TCP accept under load (a GC pause, a burst
    # of scanner connections on the public box) timed out and reported a hard FAIL,
    # and the estate went red on a server that connected fine a second later (cloud,
    # 25 Aug 2026, flapped twice). A genuine outage fails every attempt; a transient
    # does not. This is the same discipline as the log judges: do not call expected
    # noise a failure.
    local i
    for i in 1 2 3; do
        if echo | timeout 15 openssl s_client -connect "${P_PROBE}:8089" 2>/dev/null | grep -q "CONNECTED"; then
            record tls "8089 handshake" OK "completed$([[ $i -gt 1 ]] && echo " on attempt $i")"
            return
        fi
        (( i < 3 )) && sleep 2
    done
    record tls "8089 handshake" FAIL "no TLS handshake in 3 attempts over ~19s"
}

check_server_cert() {
    # Same transient as the handshake check: reading the cert off the wire can time
    # out under momentary load, and a one-shot read reported a false FAIL. Retry.
    local enddate days i
    for i in 1 2 3; do
        enddate=$(echo | timeout 10 openssl s_client -connect "${P_PROBE}:8089" 2>/dev/null \
                  | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
        [[ -n "$enddate" ]] && break
        (( i < 3 )) && sleep 2
    done
    if [[ -z "$enddate" ]]; then
        record cert "server (8089)" FAIL "could not read certificate off the wire in 3 attempts"
        return
    fi
    days=$(days_until "$enddate" || true)
    record cert "server (8089)" "$(judge_cert_days "$days")" "expires in ${days:-?}d ($enddate)" "$days" days
}

check_letsencrypt() {
    [[ "$P_LETSENCRYPT" == "1" ]] || return 0
    local enddate days issuer
    issuer=$(echo | timeout 10 openssl s_client -connect "${P_PROBE}:8446" -servername "$P_FQDN" 2>/dev/null \
             | openssl x509 -noout -issuer 2>/dev/null)
    if [[ "$issuer" == *"Let's Encrypt"* || "$issuer" == *"R1"* || "$issuer" == *"E1"* ]]; then
        record cert "8446 issuer" OK "$issuer"
    elif [[ -n "$issuer" ]]; then
        record cert "8446 issuer" FAIL "not Let's Encrypt - phones will not trust QR enrolment ($issuer)"
    else
        record cert "8446 issuer" FAIL "could not read certificate on 8446"
    fi
    enddate=$(echo | timeout 10 openssl s_client -connect "${P_PROBE}:8446" -servername "$P_FQDN" 2>/dev/null \
              | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
    if [[ -n "$enddate" ]]; then
        days=$(days_until "$enddate" || true)
        record cert "8446 expiry" "$(judge_cert_days "$days")" "expires in ${days:-?}d" "$days" days
    fi
    local t; t=$(systemctl is-enabled certbot.timer 2>/dev/null | tail -1 || true)
    [[ -z "$t" ]] && t="unknown"
    record cert "certbot.timer" "$(judge_enabled "$t")" "$t"
}

check_tailscale_cert() {
    [[ -n "$P_TSCERT" ]] || return 0
    if [[ ! -r "$P_TSCERT" ]]; then
        record cert "tailscale" SKIP "not readable at $P_TSCERT"
        return
    fi
    local enddate days
    enddate=$(openssl x509 -in "$P_TSCERT" -noout -enddate 2>/dev/null | cut -d= -f2)
    days=$(days_until "$enddate" || true)
    record cert "tailscale" "$(judge_ts_cert_days "$days")" \
        "expires in ${days:-?}d (renew: tailscale cert $P_FQDN)" "$days" days
}

check_tilesets() {
    if [[ "$P_TILESETS" == "-2" ]]; then
        record maps "tilesets served" SKIP "not fitted (declared)"
        return 0
    fi
    [[ "$P_TILESETS" == "0" ]] && return 0
    # lesson 2: `grep -oc` counts matching LINES, not occurrences, and /services
    # is a single line of JSON. Count occurrences.
    local n
    n=$(curl -s --max-time 8 "http://127.0.0.1:8080/services" 2>/dev/null | grep -o '"name"' | wc -l | tr -dc '0-9')
    if [[ "$P_TILESETS" == "-1" ]]; then
        # Maps fitted, no declared count: awaiting content is a WARN, not a fault.
        if [[ -z "$n" || "$n" == "0" ]]; then
            record maps "tilesets served" WARN "0 served - maps fitted, no tiles pushed yet" "${n:-0}" tilesets
        else
            record maps "tilesets served" OK "$n served (no declared count)" "$n" tilesets
        fi
        return 0
    fi
    record maps "tilesets served" "$(judge_tilesets "$n" "$P_TILESETS")" \
        "${n:-0} served, expected $P_TILESETS" "$n" tilesets
}

check_media() {
    [[ "$P_MEDIA" == "2" ]] && { record media "mediamtx http" SKIP "not fitted (declared)"; return 0; }
    [[ "$P_MEDIA" == "1" ]] || return 0
    local code
    code=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' "http://127.0.0.1:8888/" 2>/dev/null)
    record media "mediamtx http" "$(judge_http_code "$code")" "HTTP ${code:-none} on 8888"
}

check_chrony() {
    [[ "$P_CHRONY" == "2" ]] && { record time "chrony" SKIP "not fitted (declared)"; return 0; }
    [[ "$P_CHRONY" == "1" ]] || return 0
    if chronyc tracking >/dev/null 2>&1; then
        local stratum
        stratum=$(chronyc tracking 2>/dev/null | awk -F: '/Stratum/{gsub(/ /,"",$2); print $2}')
        record time "chrony" OK "tracking, stratum ${stratum:-?}"
    else
        record time "chrony" FAIL "not tracking - CoT staleness will make contacts vanish"
    fi
}

check_takbot() {
    [[ "$P_TAKBOT" == "2" ]] && { record bot "takbot workers" SKIP "not fitted (declared)"; return 0; }
    [[ "$P_TAKBOT" == "1" ]] || return 0
    local log since observable=1
    log=$(journalctl -u takbot --no-pager -n 300 2>&1) || observable=0
    grep -qi 'no journal files\|not.*permitted\|permission denied' <<<"$log" && observable=0

    local found=0
    grep -q "RXWorker" <<<"$log" && found=1
    local st; st=$(judge_takbot_workers "$observable" "$found")
    if [[ "$st" == "SKIP" ]]; then
        record bot "takbot workers" SKIP "cannot read the journal as this user"
        return
    fi
    record bot "takbot workers" "$st" \
        "$([[ $found == 1 ]] && echo started || echo 'has never started its workers')"

    # Errors ARE time-bounded: an old error from a since-fixed fault must not
    # fail a healthy kit. Only the current run counts.
    since=$(systemctl show takbot -p ActiveEnterTimestamp --value 2>/dev/null)
    if [[ -n "$since" ]] && journalctl -u takbot --no-pager --since "$since" 2>/dev/null \
         | grep -qiE "SyntaxError|Could not verify|Traceback"; then
        record bot "takbot current run" FAIL "errors in the current run"
    else
        record bot "takbot current run" OK "no errors since start"
    fi
}

check_capacity() {
    local pct
    pct=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
    record capacity "root filesystem" "$(judge_disk_pct "$pct")" "${pct:-?}% used" "$pct" percent
    local mem
    mem=$(free -h 2>/dev/null | awk 'NR==2{printf "%s used of %s",$3,$2}')
    [[ -n "$mem" ]] && record capacity "memory" OK "$mem"
}

check_lvm_snapshot() {
    [[ -n "$P_LVMSNAP" ]] || return 0
    # No sudo: if lvs is not readable unprivileged we simply skip rather than prompt.
    local pct
    pct=$(lvs --noheadings -o data_percent "$P_LVMSNAP" 2>/dev/null | tr -dc '0-9.')
    if [[ -n "$pct" ]]; then
        record capacity "LVM snapshot" WARN "tak-preinstall still present (${pct}% used) - delete once the build is accepted"
    fi
}

check_logs() {
    if [[ ! -r /opt/tak/logs/takserver-api.log ]]; then
        record logs "takserver" SKIP "logs not readable unprivileged"
        return
    fi

    # LESSON (23 Aug 2026): file mtime is NOT a time bound. `find -mtime -1`
    # selects the active log file, which is appended to continuously, so every
    # line in it since rotation was still being counted. Cumulative 1175 became
    # "1167 in the last 24h" — a 0.7% drop, which is the evidence the bound did
    # nothing. Filter on the LINE timestamp.
    #
    # Format, read off the server rather than assumed (lesson 14):
    #   2026-08-23-00:00:02.872 [thread] ERROR class - message
    # First 10 characters are YYYY-MM-DD. Note takserver-config.log uses a
    # different, dateless format, so lines that carry no date are excluded
    # rather than silently counted.
    local today yesterday recent hs other
    today=$(date '+%Y-%m-%d')
    yesterday=$(date -d 'yesterday' '+%Y-%m-%d' 2>/dev/null \
                || date -v-1d '+%Y-%m-%d' 2>/dev/null) || yesterday="$today"

    recent=$(grep -hE "^(${today}|${yesterday})-" /opt/tak/logs/takserver-*.log 2>/dev/null \
             | grep -iE '\bERROR\b' | grep -viE 'class,load')

    if [[ -z "$recent" ]]; then
        record logs "ERROR lines (48h)" OK "0 dated lines"
        record logs "TLS handshake failures (48h)" OK "0"
        return
    fi

    # Split the two apart. A client hammering 8089 with a bad certificate should
    # not drown out a genuine application error in a single undifferentiated count.
    hs=$(grep -c 'SSLHandshakeException' <<<"$recent" | tr -dc '0-9')
    other=$(grep -vc 'SSLHandshakeException' <<<"$recent" | tr -dc '0-9')

    record logs "ERROR lines (48h)" "$(judge_error_count "$other")" \
        "${other:-0} application errors" "$other" lines

    if (( ${hs:-0} > 0 )); then
        # Naming only the worst source hides the shape of the problem. On the
        # cloud server, 23 Aug 2026, the top source looked like one broken client
        # until the earlier bursts showed four different addresses across three
        # days, several inside 31.94.0.0/16. DISTINCT SOURCE COUNT is what
        # separates "one client stuck in a retry loop" from "being scanned", so
        # it is reported first and the top three are listed after it.
        local srcs distinct top3 note=""
        srcs=$(grep 'SSLHandshakeException' <<<"$recent" \
               | sed -nE 's/.*Remote address: ([0-9a-fA-F.:]+).*/\1/p' \
               | sort | uniq -c | sort -rn)
        distinct=$(wc -l <<<"$srcs" | tr -dc '0-9')
        top3=$(head -3 <<<"$srcs" | awk '{printf "%s(%s) ", $2, $1}')
        # On a public box the point is not the count, it is that it is expected. Say
        # so in the detail, so the number never reads as an unaddressed fault.
        [[ "$P_PUBLIC" == "1" ]] && note=" - expected internet probing; the server correctly refused them"
        record logs "TLS handshake failures (48h)" "$(judge_handshake_failures "$hs" "$P_PUBLIC")" \
            "${hs} rejected from ${distinct:-?} source(s): ${top3% }${note}" "$hs" handshakes
        # One reading, not two. Two records off the same number (a distinct-count and a
        # separate "spread") double-counted the same signal into the verdict; folded.
        record logs "distinct handshake sources (48h)" "$(judge_source_spread "$distinct" "$P_PUBLIC")" \
            "${distinct:-?} distinct address(es); a domestic/mobile client gets a fresh address per session, so a few can be one device" "$distinct" sources
    else
        record logs "TLS handshake failures (48h)" OK "0"
    fi

    local auth
    auth=$(grep -hicE 'authentication fail|login fail|invalid credential' /opt/tak/logs/takserver-*.log 2>/dev/null \
           | awk '{s+=$1} END{print s+0}')
    record logs "failed auth" "$(judge_auth_failures "$auth")" "${auth:-0} lines"
}

# ---------------------------------------------------------------------------
# SOFTWARE INVENTORY (1.1.0)
#
# What is installed, what version, whether it is running. Discovery, not
# judgement: a component is reported when its detection finds it and silently
# omitted when it is absent, so one gatherer serves all three boxes without a
# per-profile component list to keep in step. Everything is best-effort and
# unprivileged; a source this user cannot read yields an empty version, never
# an error and never a guess.
# ---------------------------------------------------------------------------

check_federation() {
    # Observable federation state, no CoreConfig read (the checker stays credential-free):
    # is the v2 federation server listening, and how many federates are connected. Federation
    # OFF is normal and never a fault - most boxes are standalone. When it is on, the connected
    # count is the useful signal (a federate that should be up but is not). 1.3.0.
    local port=9001 listening=0 connected=0
    ss -ltn 2>/dev/null | grep -qE "[:.]${port} " && listening=1
    if (( listening )); then
        connected=$(ss -tn state established 2>/dev/null | grep -cE "[:.]${port}([^0-9]|$)" || true)
        [[ "$connected" =~ ^[0-9]+$ ]] || connected=0
        record federation "v2 server" OK "on ${port}; ${connected} federate(s) connected" "$connected" federates
        if (( connected )); then
            # WHO is federated, still socket-only: the remote address of every established
            # federation conversation, either direction, ports and brackets stripped. The
            # console's map draws these, so a federate nobody recorded on the map - a partner
            # federated by hand, or from the far side - still shows up as a node. 1.4.0.
            local peers
            peers=$(ss -tn state established "( sport = :${port} or dport = :${port} )" 2>/dev/null \
                    | awk 'NR>1 {a=$4; gsub(/^\[|\]/, "", a); sub(/:[0-9]+$/, "", a); if (a != "") print a}' \
                    | sort -u | paste -sd' ' -)
        [[ -n "${peers:-}" ]] && record federation "peers" OK "$peers"
        fi
    else
        record federation "v2 server" OK "not enabled (standalone)"
    fi
}

gather_software() {
    local v s cdir ct http

    # OS and kernel first: the substrate everything else sits on.
    v=$(. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-}")
    sw_record os "${v:-unknown}" "kernel $(uname -r 2>/dev/null || echo '?')"

    # TAK Server: dpkg is authoritative for the installed version.
    v=$(dpkg-query -W -f '${Version}' takserver 2>/dev/null || true)
    if [[ -n "$v" ]]; then
        s=$(systemctl is-active takserver 2>/dev/null || true)
        sw_record takserver "$v" "${s:-unknown}"
    fi

    # This checker. The console compares this row across the estate.
    sw_record tak-health "$VERSION" installed

    # CloudTAK: liveness off the API port (lesson 1: check what it produces), version
    # from a world-readable breadcrumb. The checkout usually lives in a home directory
    # that this deliberately-unprivileged checker cannot read,
    # so root writes the version to /etc/tak-cloudtak-version (the update-cloudtak
    # action refreshes it on every apply, and enrolment seeds it). Fall back to a git
    # checkout only where one is actually readable; safe.directory='*' because the
    # checkout is owned by another user. Was "?" on every box with CloudTAK until 1.2.1.
    v=""
    [[ -r /etc/tak-cloudtak-version ]] && v=$(head -1 /etc/tak-cloudtak-version | tr -dc 'A-Za-z0-9._+-')
    ct=""
    for cdir in /opt/CloudTAK /root/CloudTAK /srv/CloudTAK; do
        [[ -r "$cdir/docker-compose.yml" ]] && { ct="$cdir"; break; }
    done
    [[ -z "$v" && -n "$ct" ]] && v=$(git -c safe.directory='*' -C "$ct" describe --tags --always 2>/dev/null || true)
    http=000
    if command -v curl >/dev/null 2>&1; then
        local addr code
        for addr in 127.0.0.1 $(hostname -I 2>/dev/null); do
            [[ "$addr" == *:* ]] && continue    # v4 probes only
            code=$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://${addr}:5000/" 2>/dev/null)
            [[ "$code" =~ ^[0-9]{3}$ ]] || code=000
            [[ "$code" != "000" ]] && { http="$code"; break; }
        done
    fi
    if [[ -n "$v" || -n "$ct" || "$http" != "000" ]]; then
        s="not answering on 5000"
        [[ "$http" != "000" ]] && s="serving (http $http)"
        sw_record cloudtak "$v" "$s"
    fi

    # mediamtx (the media stream).
    if command -v mediamtx >/dev/null 2>&1; then
        v=$(timeout 5 mediamtx --version 2>/dev/null | head -1 || true)
        s=$(systemctl is-active mediamtx 2>/dev/null || true)
        sw_record mediamtx "$v" "${s:-unknown}"
    fi

    # Meshtastic tracker gateway. Version lives inside a venv this user may not
    # be able to read; running state is always observable. Beyond the software row,
    # gather the mesh heartbeat for the console's Networks page (Spec 001): output,
    # not process - the unit being active proves nothing about the mesh, so the
    # heartbeat (written by the gateway on each forwarded packet) is the evidence,
    # and its absence renders honestly as "quiet".
    if systemctl cat mesh-manager-bridge >/dev/null 2>&1 || systemctl cat tak-meshtastic-gateway >/dev/null 2>&1; then
        mu=$(mesh_unit)
        s=$(systemctl is-active "$mu" 2>/dev/null || true)
        sw_record "$mu" "" "${s:-unknown}"
        MESH_GATEWAY="${s:-unknown}"
        MESH_RADIO=""; MESH_RADIO_PRESENT=false
        if [[ -r /etc/vantage-mesh.conf ]]; then
            MESH_RADIO=$(sed -n 's/^SERIAL=//p' /etc/vantage-mesh.conf | head -1)
            [[ -n "$MESH_RADIO" && -e "$MESH_RADIO" ]] && MESH_RADIO_PRESENT=true
        fi
        MESH_LAST=""; MESH_NODES=0; MESH_NODE_JSON=""
        if [[ -r /var/lib/vantage-mesh/heartbeat.json ]]; then
            MESH_LAST=$(python3 -c 'import json;d=json.load(open("/var/lib/vantage-mesh/heartbeat.json"));print(d.get("ts",""))' 2>/dev/null || true)
            MESH_NODES=$(python3 -c 'import json;d=json.load(open("/var/lib/vantage-mesh/heartbeat.json"));print(int(d.get("nodes_seen",0)))' 2>/dev/null || echo 0)
            # The node list the console's Networks map plots (Spec 003). Re-emitted through
            # a whitelist rather than passed through: a snapshot is read by the console and
            # by the estate, so nothing reaches it that this script did not name. A gateway
            # too old to write "nodes" leaves this empty, and the key is then omitted below.
            MESH_NODE_JSON=$(python3 - <<'PYEOF' 2>/dev/null || true
import json
K = ("id", "name", "lat", "lon", "battery", "heard", "snr", "hops",
     # heard_here separates "this gateway has heard it" from "the radio's stored
     # database mentions it". Omitting it from this whitelist silently dropped the
     # field (found live 31 Aug): the console inferred it from `heard` and happened
     # to be right, but a distinction that survives only by inference is one bad
     # refactor from being lost.
     "heard_here")
try:
    src = json.load(open("/var/lib/vantage-mesh/heartbeat.json")).get("nodes")
    out = [{k: n[k] for k in K if k in n and n[k] is not None}
           for n in (src or []) if isinstance(n, dict)][:250]
    print(json.dumps(out) if out else "")
except Exception:
    pass
PYEOF
)
        fi
    fi

    # takbot.
    if systemctl cat takbot >/dev/null 2>&1; then
        s=$(systemctl is-active takbot 2>/dev/null || true)
        sw_record takbot "" "${s:-unknown}"
    fi

    # Syncthing (the vault-sync bearer).
    if command -v syncthing >/dev/null 2>&1; then
        v=$(timeout 5 syncthing --version 2>/dev/null | awk '{print $2}' || true)
        s=$(systemctl is-active syncthing@vaultsync 2>/dev/null || true)
        sw_record syncthing "$v" "${s:-unknown}"
    fi

    # Docker: the substrate for CloudTAK where present.
    if command -v docker >/dev/null 2>&1; then
        v=$(docker --version 2>/dev/null | sed -E 's/^Docker version ([^,]+),.*/\1/' || true)
        s=$(systemctl is-active docker 2>/dev/null || true)
        sw_record docker "$v" "${s:-unknown}"
    fi

    # PostgreSQL: TAK Server's database. Ubuntu's psql is a wrapper that can fail
    # on a stale cluster config even when the server is healthy, so only trust its
    # output when it exits 0, and fall back to dpkg for the server package.
    if command -v psql >/dev/null 2>&1; then
        v=""
        v=$(psql --version 2>/dev/null) && v=$(awk '{print $3}' <<<"$v") || v=""
        [[ -z "$v" ]] && v=$(dpkg-query -W -f '${Version}\n' 'postgresql-1*' 2>/dev/null | head -1 || true)
        s=$(systemctl is-active postgresql 2>/dev/null || true)
        sw_record postgresql "$v" "${s:-unknown}"
    fi

    # certbot (public box TLS renewal).
    if command -v certbot >/dev/null 2>&1; then
        v=$(certbot --version 2>/dev/null | awk '{print $2}' || true)
        s=$(systemctl is-active certbot.timer 2>/dev/null || true)
        sw_record certbot "$v" "timer ${s:-unknown}"
    fi

    # The kit modules (Spec 002): presence rows for everything in the component
    # vocabulary not already covered above, so the console's Modules panel and
    # the loadout cross-check see what is actually fitted.
    # Mesh Manager (its own product, GPL, installed as a module the way CloudTAK is): the
    # bridge that owns the radio and the screen. Presence rows so the console can link to it.
    for s in mosquitto node-red ollama mbtileserver chrony tailscaled mesh-manager-bridge mesh-manager-web; do
        if systemctl cat "$s" >/dev/null 2>&1; then
            sw_record "$s" "" "$(systemctl is-active "$s" 2>/dev/null || echo unknown)"
        fi
    done
}

# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

overall() {
    if   (( N_FAIL > 0 )); then echo FAIL
    elif (( N_WARN > 0 )); then echo WARN
    else echo OK; fi
}

exit_code() {
    case "$(overall)" in FAIL) echo 2 ;; WARN) echo 1 ;; *) echo 0 ;; esac
}

render_human() {
    local colour=1
    [[ -t 1 ]] || colour=0
    c() { [[ "$colour" == "1" ]] && printf '\033[%sm' "$1"; }
    printf '=== TAK health %s: profile %s (%s) ===\n' "$VERSION" "$PROFILE" "$P_DESC"
    printf '    host %s   %s\n\n' "$(hostname -s 2>/dev/null)" "$(date -u '+%Y-%m-%d %H:%M:%SZ')"
    local i last=""
    for i in "${!CHK_NAME[@]}"; do
        if [[ "${CHK_CAT[$i]}" != "$last" ]]; then
            printf -- '--- %s ---\n' "${CHK_CAT[$i]}"
            last="${CHK_CAT[$i]}"
        fi
        case "${CHK_STATUS[$i]}" in
            OK)   c 32 ;; WARN) c 33 ;; FAIL) c 31 ;; SKIP) c 90 ;;
        esac
        printf '  %-4s' "${CHK_STATUS[$i]}"
        c 0
        printf '  %-24s %s\n' "${CHK_NAME[$i]}" "${CHK_DETAIL[$i]}"
    done
    if (( ${#SW_NAME[@]} > 0 )); then
        printf -- '\n--- software ---\n'
        for i in "${!SW_NAME[@]}"; do
            printf '  %-24s %-22s %s\n' "${SW_NAME[$i]}" \
                "${SW_VERSION[$i]:--}" "${SW_STATE[$i]}"
        done
    fi
    printf '\nRESULT: %s  (%d ok, %d warn, %d fail, %d skipped)\n' \
        "$(overall)" "$N_OK" "$N_WARN" "$N_FAIL" "$N_SKIP"
}

sw_json() { # name version state -> one JSON object (no trailing comma)
    # Pure, like check_json, so --self-test can drive it. Version may be empty
    # (a source this user cannot read); it is emitted as "" rather than dropped,
    # because "unknown version" and "component absent" are different facts.
    printf '{ "name": "%s", "version": "%s", "state": "%s" }' \
        "$(json_escape "$1")" "$(json_escape "$2")" "$(json_escape "$3")"
}

check_json() { # category name status detail value unit -> one JSON object (no trailing comma)
    # Pure: no globals, no I/O, so --self-test can drive it in both directions.
    # A value must be numeric to be emitted; anything else is dropped with the
    # unit, because a non-number in "value" breaks every consumer downstream.
    local cat="$1" name="$2" status="$3" detail="$4" value="${5:-}" unit="${6:-}"
    local base
    base=$(printf '{ "category": "%s", "name": "%s", "status": "%s", "detail": "%s"'         "$(json_escape "$cat")" "$(json_escape "$name")" "$status" "$(json_escape "$detail")")
    if [[ "$value" =~ ^-?[0-9]+$ ]]; then
        printf '%s, "value": %s, "unit": "%s" }' "$base" "$value" "$(json_escape "$unit")"
    else
        printf '%s }' "$base"
    fi
}

render_json() {
    printf '{\n'
    printf '  "schema": "%s",\n' "$SCHEMA"
    printf '  "version": "%s",\n' "$VERSION"
    printf '  "profile": "%s",\n' "$(json_escape "$PROFILE")"
    printf '  "loadout": { "declared": "%s", "source": "%s" },\n' \
        "$(json_escape "$LOADOUT")" "$(json_escape "$LOADOUT_SOURCE")"
    printf '  "description": "%s",\n' "$(json_escape "$P_DESC")"
    printf '  "host": "%s",\n' "$(json_escape "$(hostname -s 2>/dev/null)")"
    printf '  "fqdn": "%s",\n' "$(json_escape "$P_FQDN")"
    printf '  "timestamp": "%s",\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf '  "result": "%s",\n' "$(overall)"
    printf '  "exit_code": %s,\n' "$(exit_code)"
    printf '  "counts": { "ok": %d, "warn": %d, "fail": %d, "skip": %d },\n' \
        "$N_OK" "$N_WARN" "$N_FAIL" "$N_SKIP"
    if [[ -n "$MESH_GATEWAY" ]]; then
        # last_forwarded is null, not "", when nothing has ever been forwarded: the
        # console's tile logic keys on that honestly (quiet, never ok).
        local mesh_last="null"
        [[ -n "$MESH_LAST" ]] && mesh_last="\"$(json_escape "$MESH_LAST")\""
        # nodes is OMITTED, not empty, when the gateway does not report one: the console
        # then says the gateway reports no node positions, which is the truth, rather than
        # drawing an empty mesh that reads as "everything is gone".
        local mesh_nodes=""
        [[ -n "$MESH_NODE_JSON" ]] && mesh_nodes=", \"nodes\": $MESH_NODE_JSON"
        printf '  "mesh": { "gateway": "%s", "radio": "%s", "radio_present": %s, "last_forwarded": %s, "nodes_seen": %d%s },\n' \
            "$(json_escape "$MESH_GATEWAY")" "$(json_escape "$MESH_RADIO")" \
            "$MESH_RADIO_PRESENT" "$mesh_last" "$MESH_NODES" "$mesh_nodes"
    fi
    printf '  "software": [\n'
    local j m=${#SW_NAME[@]}
    for j in "${!SW_NAME[@]}"; do
        printf '    %s' "$(sw_json "${SW_NAME[$j]}" "${SW_VERSION[$j]}" "${SW_STATE[$j]}")"
        (( j < m - 1 )) && printf ','
        printf '\n'
    done
    printf '  ],\n'
    printf '  "checks": [\n'
    local i n=${#CHK_NAME[@]}
    for i in "${!CHK_NAME[@]}"; do
        printf '    %s' "$(check_json "${CHK_CAT[$i]}" "${CHK_NAME[$i]}" \
            "${CHK_STATUS[$i]}" "${CHK_DETAIL[$i]}" "${CHK_VALUE[$i]}" "${CHK_UNIT[$i]}")"
        (( i < n - 1 )) && printf ','
        printf '\n'
    done
    printf '  ]\n}\n'
}

# ---------------------------------------------------------------------------
# SELF-TEST - the lessons log lesson 2, "check the checker"
#
# Every judge is driven in both directions with synthetic values. This is the
# part that would have caught the four false faults of 8 August: the /dev/tcp
# blocking under zsh, `grep -oc` counting lines, and the ten-minute log window.
# Runs anywhere, including macOS. Needs no TAK server.
# ---------------------------------------------------------------------------

T_PASS=0; T_FAIL=0
expect() { # <description> <expected> <actual>
    if [[ "$2" == "$3" ]]; then
        printf '  \033[32mpass\033[0m  %s\n' "$1"; T_PASS=$((T_PASS+1))
    else
        printf '  \033[31mFAIL\033[0m  %s (expected %s, got %s)\n' "$1" "$2" "$3"; T_FAIL=$((T_FAIL+1))
    fi
}

self_test() {
    echo "=== self-test: driving every judge in both directions ==="
    echo
    echo "--- services ---"
    expect "active service passes"            OK   "$(judge_service active)"
    expect "inactive service fails"           FAIL "$(judge_service inactive)"
    expect "failed service fails"             FAIL "$(judge_service failed)"
    expect "unknown service fails"            FAIL "$(judge_service unknown)"
    expect "enabled at boot passes"           OK   "$(judge_enabled enabled)"
    expect "static counts as enabled"         OK   "$(judge_enabled static)"
    expect "disabled at boot warns"           WARN "$(judge_enabled disabled)"

    echo "--- ports ---"
    expect "listening port passes"            OK   "$(judge_port 1)"
    expect "silent port fails"                FAIL "$(judge_port 0)"

    echo "--- certificates ---"
    expect "cert with 90d passes"             OK   "$(judge_cert_days 90)"
    expect "cert with 29d warns"              WARN "$(judge_cert_days 29)"
    expect "cert with 30d passes (boundary)"  OK   "$(judge_cert_days 30)"
    expect "expired cert fails"               FAIL "$(judge_cert_days -1)"
    expect "unreadable cert fails"            FAIL "$(judge_cert_days '')"
    expect "tailscale cert 20d warns"         WARN "$(judge_ts_cert_days 20)"
    expect "tailscale cert 21d passes"        OK   "$(judge_ts_cert_days 21)"

    echo "--- capacity ---"
    expect "disk 50% passes"                  OK   "$(judge_disk_pct 50)"
    expect "disk 80% warns"                   WARN "$(judge_disk_pct 80)"
    expect "disk 90% fails"                   FAIL "$(judge_disk_pct 90)"
    expect "unreadable disk warns"            WARN "$(judge_disk_pct '')"

    echo "--- tilesets (the grey-map fault) ---"
    expect "6 of 6 tilesets passes"           OK   "$(judge_tilesets 6 6)"
    expect "7 of 6 tilesets passes"           OK   "$(judge_tilesets 7 6)"
    expect "3 of 6 tilesets warns"            WARN "$(judge_tilesets 3 6)"
    expect "0 tilesets FAILS"                 FAIL "$(judge_tilesets 0 6)"
    expect "unreadable tile count fails"      FAIL "$(judge_tilesets '' 6)"

    echo "--- handshake source spread (private box) ---"
    expect "4 distinct sources passes"        OK   "$(judge_source_spread 4)"
    expect "5 distinct sources warns"         WARN "$(judge_source_spread 5)"
    expect "unparsed sources stays OK"        OK   "$(judge_source_spread '')"

    echo "--- check_json (schema/2 value emission) ---"
    local cj
    cj="$(check_json cert test OK detail 12 days)"
    expect "numeric value emitted"            yes  "$([[ "$cj" == *'"value": 12, "unit": "days"'* ]] && echo yes || echo no)"
    cj="$(check_json cert test OK detail '' '')"
    expect "no value: key omitted"            yes  "$([[ "$cj" != *'"value"'* ]] && echo yes || echo no)"
    cj="$(check_json cert test OK detail 'n/a' days)"
    expect "non-numeric value: key omitted"   yes  "$([[ "$cj" != *'"value"'* ]] && echo yes || echo no)"
    cj="$(check_json cert test OK detail -1 days)"
    expect "negative value emitted"           yes  "$([[ "$cj" == *'"value": -1'* ]] && echo yes || echo no)"

    echo "--- firewall posture ---"
    expect "nuc with Anywhere rule FAILS"     FAIL "$(judge_firewall 1 0)"
    expect "nuc scoped to tailnet passes"     OK   "$(judge_firewall 0 0)"
    expect "cloud with Anywhere rule passes"  OK   "$(judge_firewall 1 1)"
    expect "cloud closed to public FAILS"     FAIL "$(judge_firewall 0 1)"

    echo "--- logs: error count is a trend signal, not an absolute alarm ---"
    expect "5 ERROR lines passes"             OK   "$(judge_error_count 5)"
    expect "500 ERROR lines OK (weak signal)" OK   "$(judge_error_count 500)"
    expect "healthy 546/48h is OK not WARN"   OK   "$(judge_error_count 546)"
    expect "3000 ERROR lines flood-warns"     WARN "$(judge_error_count 3000)"
    expect "unreadable error count is OK"     OK   "$(judge_error_count '')"
    expect "any failed auth warns"            WARN "$(judge_auth_failures 3)"
    echo "--- logs: handshakes - private box judges, public box expects ---"
    expect "10 handshakes OK (private)"       OK   "$(judge_handshake_failures 10)"
    expect "50 handshakes warn (private)"     WARN "$(judge_handshake_failures 50)"
    expect "1167 handshakes warn (private)"   WARN "$(judge_handshake_failures 1167)"
    expect "1437 handshakes OK on PUBLIC"     OK   "$(judge_handshake_failures 1437 1)"
    expect "5000 handshakes OK on PUBLIC"     OK   "$(judge_handshake_failures 5000 1)"
    expect "1 source = retry loop, OK"        OK   "$(judge_source_spread 1)"
    expect "3 sources still OK (ISP churn)"   OK   "$(judge_source_spread 3)"
    expect "5 sources warn (private)"         WARN "$(judge_source_spread 5)"
    expect "14 sources OK on PUBLIC box"      OK   "$(judge_source_spread 14 1)"
    expect "5000 sources OK on PUBLIC box"    OK   "$(judge_source_spread 5000 1)"
    expect "unknown spread OK"                OK   "$(judge_source_spread '')"
    expect "no failed auth passes"            OK   "$(judge_auth_failures 0)"

    echo "--- http ---"
    expect "200 passes"                       OK   "$(judge_http_code 200)"
    expect "404 passes (server answering)"    OK   "$(judge_http_code 404)"
    expect "000 fails (nothing answering)"    FAIL "$(judge_http_code 000)"

    echo "--- unobservable must SKIP, never produce a verdict ---"
    expect "readable ufw, cloud open, OK"     OK   "$(judge_firewall_observed 1 1 1)"
    expect "readable ufw, cloud shut, FAIL"   FAIL "$(judge_firewall_observed 1 0 1)"
    expect "readable ufw, nuc open, FAIL"     FAIL "$(judge_firewall_observed 1 1 0)"
    expect "UNREADABLE ufw SKIPS, not FAIL"   SKIP "$(judge_firewall_observed 0 0 1)"
    expect "UNREADABLE ufw SKIPS on nuc too"  SKIP "$(judge_firewall_observed 0 1 0)"
    expect "readable journal, workers up"     OK   "$(judge_takbot_workers 1 1)"
    expect "readable journal, no workers"     FAIL "$(judge_takbot_workers 1 0)"
    expect "UNREADABLE journal SKIPS"         SKIP "$(judge_takbot_workers 0 0)"

    echo "--- hostname guard ---"
    expect "matching host passes its profile" OK   "$(judge_hostname boxone boxone)"
    expect "matching host passes cloud"       OK   "$(judge_hostname boxtwo boxtwo)"
    expect "wrong host is refused"            FAIL "$(judge_hostname boxtwo boxone)"
    # The severity mapping is the part that got it wrong live on 23 Aug.
    expect "matching host is OK"              OK   "$(judge_guard_severity OK 0)"
    expect "wrong host WARNS, never FAILS"    WARN "$(judge_guard_severity FAIL 0)"
    expect "--force silences the guard"       OK   "$(judge_guard_severity FAIL 1)"

    echo "--- profiles load ---"
    # 1.5.1: the self-test must not read the box's real conf - on an enrolled box
    # /etc/tak-health.conf legitimately overrides the profile defaults these tests
    # assert, and the gate refused its own artefact on the first box to carry one.
    CONF_FILE="/nonexistent-selftest"
    # 1.6.0: same rule for the loadout file - the self-test must not read the
    # box's real declaration, which legitimately narrows the very defaults these
    # tests assert.
    LOADOUT_FILE="/nonexistent-selftest"
    local p
    for p in cloud firmbase deployed; do
        if load_profile "$p"; then expect "profile $p loads" OK OK
        else expect "profile $p loads" OK FAIL; fi
    done
    if load_profile nonsense 2>/dev/null; then expect "unknown profile rejected" FAIL OK
    else expect "unknown profile rejected" OK OK; fi
    if load_profile nuc && [[ "$P_DESC" == *firmbase* ]]; then
        expect "alias nuc loads firmbase" OK OK
    else expect "alias nuc loads firmbase" OK FAIL; fi

    echo "--- per-box overrides beat profile defaults (1.5.0) ---"
    OVERRIDE_FQDN="dev.example.org"; OVERRIDE_HOSTMATCH="devbox"
    load_profile cloud
    expect "CLI fqdn override applies"        yes  "$([[ "$P_FQDN" == dev.example.org ]] && echo yes || echo no)"
    expect "CLI hostmatch override applies"   yes  "$([[ "$P_HOSTMATCH" == devbox ]] && echo yes || echo no)"
    OVERRIDE_FQDN=""; OVERRIDE_HOSTMATCH=""
    local tconf; tconf=$(mktemp)
    printf 'FQDN=conf.example.org\nHOSTMATCH=confbox\n' > "$tconf"
    CONF_FILE="$tconf"; load_profile cloud
    expect "conf fqdn override applies"       yes  "$([[ "$P_FQDN" == conf.example.org ]] && echo yes || echo no)"
    expect "conf hostmatch override applies"  yes  "$([[ "$P_HOSTMATCH" == confbox ]] && echo yes || echo no)"
    CONF_FILE="/nonexistent-selftest"; load_profile cloud
    local defq="$P_FQDN"
    printf 'FQDN=bad value; rm -rf /\n' > "$tconf"
    CONF_FILE="$tconf"; load_profile cloud
    expect "malformed conf value ignored"     yes  "$([[ "$P_FQDN" == "$defq" ]] && echo yes || echo no)"
    rm -f "$tconf"; CONF_FILE="/nonexistent-selftest"

    echo "--- component map and declared loadout (Spec 002, 1.6.0) ---"
    expect "takserver maps to its services"   yes \
        "$([[ "$(component_services takserver)" == "takserver postgresql" ]] && echo yes || echo no)"
    expect "lanntp maps to chrony"            yes \
        "$([[ "$(component_services lanntp)" == "chrony" ]] && echo yes || echo no)"
    expect "syncthing maps to its unit"      yes \
        "$([[ "$(component_services syncthing)" == "syncthing@vaultsync" ]] && echo yes || echo no)"
    expect "nodered token maps to node-red"   yes \
        "$([[ "$(component_services nodered)" == "node-red" ]] && echo yes || echo no)"
    if component_services wibble >/dev/null 2>&1; then
        expect "unknown component rejected" OK FAIL
    else
        expect "unknown component rejected" OK OK
    fi
    expect "mediamtx brings its three ports"  yes \
        "$([[ "$(component_tcp mediamtx)" == "1935 8554 8888" ]] && echo yes || echo no)"
    expect "lanntp brings udp 123"            yes \
        "$([[ "$(component_udp lanntp)" == "123" ]] && echo yes || echo no)"
    expect "infratak is declarable, on its platform port" yes \
        "$([[ "$(component_services infratak)" == "docker" \
           && "$(component_tcp infratak)" == "5001" ]] && echo yes || echo no)"
    local tload; tload=$(mktemp)
    printf 'COMPONENTS=takserver,mediamtx\n' > "$tload"
    LOADOUT_FILE="$tload"; load_profile deployed
    expect "declaration narrows services"     yes \
        "$([[ "$P_SERVICES" == *mediamtx* && "$P_SERVICES" != *ollama* ]] && echo yes || echo no)"
    expect "undeclared services move to skipped" yes \
        "$([[ "$P_SKIPPED_SERVICES" == *ollama* ]] && echo yes || echo no)"
    expect "undeclared takbot enters skip state" yes \
        "$([[ "$P_TAKBOT" == "2" ]] && echo yes || echo no)"
    printf 'COMPONENTS=takserver,wibble\n' > "$tload"
    load_profile deployed
    expect "unknown token rejects the whole declaration" yes \
        "$([[ "$P_SERVICES" == *ollama* && "$LOADOUT_SOURCE" == rejected* ]] && echo yes || echo no)"
    printf 'COMPONENTS=takserver,maps\nTILESETS=3\n' > "$tload"
    load_profile deployed
    expect "declared tileset count honoured"  yes \
        "$([[ "$P_TILESETS" == "3" ]] && echo yes || echo no)"
    rm -f "$tload"; LOADOUT_FILE="/nonexistent-selftest"; load_profile deployed
    expect "no declaration keeps the full deployed list" yes \
        "$([[ "$P_SERVICES" == *ollama* && "$P_SERVICES" == *takbot* ]] && echo yes || echo no)"

    echo "--- software inventory (sw_json, 1.1.0) ---"
    local sj
    sj="$(sw_json mediamtx v1.20.0 active)"
    expect "sw_json emits name"               yes  "$([[ "$sj" == *'"name": "mediamtx"'* ]] && echo yes || echo no)"
    expect "sw_json emits version"            yes  "$([[ "$sj" == *'"version": "v1.20.0"'* ]] && echo yes || echo no)"
    expect "sw_json emits state"              yes  "$([[ "$sj" == *'"state": "active"'* ]] && echo yes || echo no)"
    sj="$(sw_json gateway '' unknown)"
    expect "empty version emitted as empty string" yes \
        "$([[ "$sj" == *'"version": ""'* ]] && echo yes || echo no)"
    sj="$(sw_json 'a"b' 'v"1' 'ok')"
    expect "sw_json escapes quotes"           yes  "$([[ "$sj" == *'a\"b'* && "$sj" == *'v\"1'* ]] && echo yes || echo no)"

    echo "--- version ---"
    expect "version is set"                   yes  "$([[ -n "$VERSION" ]] && echo yes || echo no)"
    # 1.0.0 ended the proof-of-concept 0.1.x rule; from here the shape is semver.
    expect "version is x.y.z semver"          yes \
        "$([[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && echo yes || echo no)"

    echo "--- date portability (lesson 11: macOS is not Linux) ---"
    if to_epoch "Jan  1 00:00:00 2030 GMT" >/dev/null 2>&1; then
        expect "openssl date parses" OK OK
    else
        expect "openssl date parses" OK FAIL
    fi

    echo
    printf 'SELF-TEST: %d passed, %d failed\n' "$T_PASS" "$T_FAIL"
    (( T_FAIL > 0 )) && return 2
    return 0
}

usage() {
    cat <<USAGE
tak-health.sh - read-only health check for MilUX TAK servers

  --profile <cloud|firmbase|deployed>   which ROLE this box plays (nuc = old name
                                        for firmbase, still accepted)
  --fqdn <name>                    this box's FQDN, overriding the profile default
  --hostmatch <substr>             this box's hostname guard, overriding the default
                                   (both also read from /etc/tak-health.conf)
  --json                           machine-readable output (for the console, Sam, MilUX)
  --print-profile                  print the resolved expectations (profile + conf +
                                   declared loadout) and exit; nothing is probed
  --self-test                      drive every judge in both directions, no server needed
  --list-profiles                  show what each profile expects
  --force                          run even if the hostname guard says wrong box
  --version                        print the version and exit
  -h, --help                       this

Exit: 0 all OK, 1 warnings only, 2 one or more failures.
No credentials, no sudo, no writes. Runs locally on the box being checked.
USAGE
}

list_profiles() {
    local p
    for p in cloud firmbase deployed; do
        load_profile "$p"
        printf '%s\n' "$p"
        printf '  %s\n' "$P_DESC"
        printf '  fqdn      %s\n' "$P_FQDN"
        printf '  hostname  *%s*\n' "$P_HOSTMATCH"
        printf '  services  %s\n' "$P_SERVICES"
        printf '  tcp       %s\n' "$P_TCP"
        [[ -n "$P_UDP" ]] && printf '  udp       %s\n' "$P_UDP"
        printf '  firewall  %s\n' \
            "$([[ $P_PUBLIC == 1 ]] && echo 'TAK ports open to Anywhere (expected)' || echo 'must NOT be open to Anywhere')"
        [[ $P_TILESETS -gt 0 ]] && printf '  tilesets  %s expected\n' "$P_TILESETS"
        printf '\n'
    done
}

# ---------------------------------------------------------------------------
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --profile) PROFILE="${2:-}"; shift 2 ;;
            --fqdn) OVERRIDE_FQDN="${2:-}"; shift 2 ;;
            --hostmatch) OVERRIDE_HOSTMATCH="${2:-}"; shift 2 ;;
            --probe) OVERRIDE_PROBE="${2:-}"; shift 2 ;;
            --tscert) OVERRIDE_TSCERT="${2:-}"; shift 2 ;;
            --json) JSON=1; shift ;;
            --print-profile) PRINTPROFILE=1; shift ;;
            --self-test) SELFTEST=1; shift ;;
            --list-profiles) list_profiles; exit 0 ;;
            --force) FORCE=1; shift ;;
            --version) echo "tak-health.sh $VERSION"; exit 0 ;;
            -h|--help) usage; exit 0 ;;
            *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
        esac
    done

    if [[ "$SELFTEST" == "1" ]]; then self_test; exit $?; fi

    if [[ -z "$PROFILE" ]]; then
        echo "error: --profile is required (cloud|firmbase|deployed). There is no safe default." >&2
        exit 64
    fi
    # the report carries the role name even when invoked by the old alias
    [[ "$PROFILE" == "nuc" ]] && PROFILE="firmbase"
    if ! load_profile "$PROFILE"; then
        echo "error: unknown profile '$PROFILE'. Try --list-profiles." >&2
        exit 64
    fi

    if [[ "$PRINTPROFILE" == "1" ]]; then
        # Introspection for the loadout fixture tests (Spec 002 AC7): the resolved
        # expectations, one KEY=value per line, nothing probed on the box.
        printf 'PROFILE=%s\n'            "$PROFILE"
        printf 'P_SERVICES=%s\n'         "$P_SERVICES"
        printf 'P_SKIPPED_SERVICES=%s\n' "$P_SKIPPED_SERVICES"
        printf 'P_TCP=%s\n'              "$P_TCP"
        printf 'P_UDP=%s\n'              "$P_UDP"
        printf 'P_TILESETS=%s\n'         "$P_TILESETS"
        printf 'P_MEDIA=%s\n'            "$P_MEDIA"
        printf 'P_CHRONY=%s\n'           "$P_CHRONY"
        printf 'P_TAKBOT=%s\n'           "$P_TAKBOT"
        printf 'LOADOUT=%s\n'            "$LOADOUT"
        printf 'LOADOUT_SOURCE=%s\n'     "$LOADOUT_SOURCE"
        exit 0
    fi

    check_hostname
    check_loadout
    check_services
    check_declared_vs_observed
    check_ports
    check_firewall
    check_resolution
    check_tls_handshake
    check_server_cert
    check_letsencrypt
    check_tailscale_cert
    check_tilesets
    check_media
    check_chrony
    check_takbot
    check_capacity
    check_lvm_snapshot
    check_logs
    check_federation
    gather_software

    if [[ "$JSON" == "1" ]]; then render_json; else render_human; fi
    exit "$(exit_code)"
}

main "$@"
