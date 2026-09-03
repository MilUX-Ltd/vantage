#!/usr/bin/env bash
# vantage-tak-provision.sh - stand up a MilUX TAK Server on a bare Ubuntu box, non-interactively.
#
# COMPAT MARKER (do not remove until every enrolled box has been re-enrolled with a 2.0
# tak-provision gate): a box enrolled before the rename carries a tak-provision forced command
# that greps the pushed payload for the literal 'the development repository-provision' before it will run it
# (the sha256 is the actual security; this grep only checks the payload looks like a known
# provisioner). This file was formerly named the development repository-provision.sh; the token on the next
# line keeps those pre-2.0 boxes accepting it. 2.0 boxes gate on 'vantage-tak-provision',
# already present above. Marker token: the development repository-provision
#
# This is the deterministic, no-AI, no-TUI replacement for driving installTAK's ncurses
# wizard by hand. It reimplements the same install (the build-log's stated right answer:
# "skip the wizard, template CoreConfig.xml and run makeCert non-interactively") so a
# product operator fills a form in the console and the console runs THIS, streaming progress.
#
# It runs ON the target box, as root. The console pushes it plus the takserver .deb over the
# provisioning channel; there is no interactive input and no arbitrary command - every input
# is a validated parameter, so this stays a known operation, not a shell.
#
# STAGED and RE-RUNNABLE where it can be: each stage checks whether its work is already done
# and skips it, so a failed run can be re-run. The TAK install itself is NOT idempotent (the
# .deb refuses to reinstall over itself), so stage 3 guards hard and refuses rather than
# corrupting an existing install - deliberately, after the cloud build's reinstall breakage.
#
#   vantage-tak-provision.sh \
#       --fqdn tak.example.org --le-email admin@example.org \
#       --org "Example" --org-unit "Ops" --country GB --state England --city London \
#       --deb /root/takserver_5.7-RELEASE43_all.deb \
#       [--components cloudtak,mediamtx,mosquitto,maps,ollama,lanntp,nodered,takbot]
#       [--stage <name>] [--dry-run]
#
# On success the server is running and this prints ENROL-READY with the facts the console
# needs to auto-enrol it to the estate.
#
# MilUX, 25 August 2026. Reference: myTeckNet installTAK (sequence),
# servers/cloud/build-log.md (decisions), the working cloud box (ground truth).
set -uo pipefail

# ---------------------------------------------------------------------------- parameters
FQDN="" LE_EMAIL="" ORG="MilUX" ORG_UNIT="TAK" COUNTRY="GB" STATE="England" CITY="London"
DEB="" COMPONENTS="" ONLY_STAGE="" DRY=0 CA_PASS_CHOICE=""
OFFLINE_REPO=""      # --offline-repo: build from a carried-in package bundle
NO_LE=0              # --no-letsencrypt: skip the public-certificate stage
# --cert-file / --key-file: a certificate you already hold, from wherever you got it.
# This is how a box on a private address ends up with a publicly trusted certificate: you
# obtain one by whatever means your DNS provider offers, and hand it over. The build has
# no opinion about who issued it or how, which is the point - an earlier version wired one
# particular DNS provider into the product because that is what WE use, and that provider
# is nobody else's business.
CERT_FILE="" KEY_FILE=""
# needrestart list-mode: apt inside a provision must never auto-restart services. On a
# single-box install the console IS on this box, and needrestart restarting it kills the
# job that is driving the provision (bitten live, 27 Aug). Deferred restarts land at the
# operator's next reboot, which a fresh build gets anyway.
export NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1
CApass=""   # generated, never a parameter

die() { echo "PROVISION-ERROR: $*" >&2; exit 2; }
log() { echo "== $* =="; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fqdn) FQDN="${2:-}"; shift 2 ;;
        --le-email) LE_EMAIL="${2:-}"; shift 2 ;;
        --org) ORG="${2:-}"; shift 2 ;;
        --org-unit) ORG_UNIT="${2:-}"; shift 2 ;;
        --country) COUNTRY="${2:-}"; shift 2 ;;
        --state) STATE="${2:-}"; shift 2 ;;
        --city) CITY="${2:-}"; shift 2 ;;
        --deb) DEB="${2:-}"; shift 2 ;;
        # point at a bundle's apt/ directory to build with no internet at all
        --offline-repo) OFFLINE_REPO="${2:-}"; shift 2 ;;
        # skip the browser-trusted 8446 connector: no public DNS, or you do not want it
        --no-letsencrypt) NO_LE=1; shift ;;
        --cert-file) CERT_FILE="${2:-}"; shift 2 ;;
        --key-file) KEY_FILE="${2:-}"; shift 2 ;;
        --components) COMPONENTS="${2:-}"; shift 2 ;;
        --stage) ONLY_STAGE="${2:-}"; shift 2 ;;
        --ca-pass) CA_PASS_CHOICE="${2:-}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

# ---------------------------------------------------------------------------- validation
[[ "$(id -u)" == 0 ]] || die "run as root"
# --stage components installs extras on an already-built box: the PKI identity and the deb
# are not used, so they are not required in that mode (the server page's Modules panel).
if [[ "$ONLY_STAGE" == "components" ]]; then
    [[ -n "$COMPONENTS" ]] || die "--stage components needs --components"
    [[ "$COMPONENTS" =~ ^[a-z,]+$ ]] || die "invalid --components"
else
    [[ "$FQDN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || die "invalid --fqdn"
    # Only needed when a public certificate is actually being requested. A private
    # build has nowhere to send an expiry notice and no certificate to expire, so
    # demanding an address for one is a question with no purpose.
    if (( NO_LE )); then
        [[ -z "$LE_EMAIL" || "$LE_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] \
            || die "invalid --le-email"
    else
        [[ "$LE_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || die "invalid --le-email"
    fi
    [[ "$COUNTRY" =~ ^[A-Z]{2}$ ]] || die "--country must be a 2-letter code"
    for v in "$ORG" "$ORG_UNIT" "$STATE" "$CITY"; do
        [[ "$v" =~ ^[A-Za-z0-9._\ -]{1,40}$ ]] || die "PKI fields must be [A-Za-z0-9._ -], max 40: '$v'"
    done
    [[ -z "$COMPONENTS" || "$COMPONENTS" =~ ^[a-z,]+$ ]] || die "invalid --components"
    # an operator-chosen certificate password: fine for experimental builds. Charset is
    # what survives every context it enters (XML attributes, sed with # delimiter, the
    # enrolment QR); 8+ because keystores refuse shorter.
    [[ -z "$CA_PASS_CHOICE" || "$CA_PASS_CHOICE" =~ ^[A-Za-z0-9._!-]{8,64}$ ]] \
        || die "--ca-pass must be 8-64 chars from letters, digits and . _ ! -"
fi

run() { if (( DRY )); then echo "DRY: $*"; else eval "$*"; fi; }
want_stage() { [[ -z "$ONLY_STAGE" || "$ONLY_STAGE" == "$1" ]]; }

# Derived, deterministic identities. CA names follow the estate convention (cloud used
# MILUX-ROOT-CA / MILUX-ISSUING-CA). The CA password is generated once and persisted to a
# root-only file so re-runs and the LE renew hook use the same one.
ORG_UP="${ORG^^}"; ORG_UP="${ORG_UP// /-}"
ROOT_CA="${ORG_UP}-ROOT-CA"
INT_CA="${ORG_UP}-ISSUING-CA"
CAPASS_FILE=/opt/tak/certs/.milux-capass
LE_JKS=/opt/tak/certs/files/le.jks
CERTS=/opt/tak/certs
capass() {
    # precedence: a password already on the box wins (re-runs must never rotate it);
    # then the operator's choice; then a strong generated one. Whatever it is, it
    # persists to the root-only file and is NEVER printed to the log.
    if [[ -f "$CAPASS_FILE" ]]; then cat "$CAPASS_FILE"; return; fi
    # dry runs must not write the password file (and could not: /opt/tak may not exist)
    if (( DRY )); then printf 'DRY-PLACEHOLDER'; return; fi
    local p
    if [[ -n "$CA_PASS_CHOICE" ]]; then p="$CA_PASS_CHOICE"
    else p=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24); fi
    install -m 600 -o root -g root /dev/null "$CAPASS_FILE"; printf '%s' "$p" > "$CAPASS_FILE"
    printf '%s' "$p"
}

# ============================================================================ STAGE 1: harden
# apt fully up to date, ufw scoped to exactly the TAK ports, unattended-upgrades on.
# Re-runnable. Ports mirror the cloud box: 22 ssh, 80 for the LE HTTP-01 challenge,
# 8089 CoT, 8443/8446 the web/admin/enrolment connectors.
# ---------------------------------------------------------------------------- offline apt
# With --offline-repo pointing at a bundle's apt/ directory, every package the build needs is
# taken from there and the box never reaches for the internet. apt still resolves the
# dependencies: the bundle is a real repository with an index, not a pile of .debs, so a
# missing dependency is a clear apt error here rather than a half-installed server later.
#
# The restricted sourcelist matters. Updating with the box's normal sources on a
# disconnected box fails on every unreachable one; pointing apt at ONLY the local list makes
# the update succeed and the install deterministic.
APT_LOCAL_LIST=/etc/apt/sources.list.d/vantage-offline.list
APT_ONLY_LOCAL=""
apt_prepare() {
    export DEBIAN_FRONTEND=noninteractive
    if [[ -n "$OFFLINE_REPO" ]]; then
        [[ -d "$OFFLINE_REPO" ]] || die "--offline-repo $OFFLINE_REPO is not a directory"
        [[ -f "$OFFLINE_REPO/Packages" || -f "$OFFLINE_REPO/Packages.gz" ]] \
            || die "--offline-repo has no package index; point at the bundle's apt/ directory"
        log "using the offline package bundle at $OFFLINE_REPO"
        run "sh -c 'echo \"deb [trusted=yes] file:$OFFLINE_REPO ./\" > $APT_LOCAL_LIST'"
        APT_ONLY_LOCAL="-o Dir::Etc::sourcelist=$APT_LOCAL_LIST -o Dir::Etc::sourceparts=/dev/null -o APT::Get::List-Cleanup=0"
        run "apt-get update -qq $APT_ONLY_LOCAL"
    else
        run "apt-get update -qq"
    fi
}
apt_install() { run "apt-get install -y -qq $APT_ONLY_LOCAL $*"; }

stage_harden() {
    log "STAGE 1/9 harden"
    export DEBIAN_FRONTEND=noninteractive
    apt_prepare
    # a disconnected box has nothing to upgrade FROM, and asking wastes minutes failing
    [[ -n "$OFFLINE_REPO" ]] || run "apt-get -y -qq upgrade"
    apt_install ufw unattended-upgrades ca-certificates curl gnupg
    for p in 22 80 8089 8443 8446; do run "ufw allow $p/tcp"; done
    # A box that carries a Vantage console keeps its console reachable: enabling the
    # firewall without this walled off the very page watching the build (bitten live,
    # 27 Aug - the operator's polls timed out while the job ran on, deaf). The port
    # comes from the console's own unit; 8090 if it does not say.
    if [[ -f /etc/vantage-console/console-host ]]; then
        cport=$(grep -ho "VANTAGE_CONSOLE_PORT=[0-9]*" /etc/systemd/system/vantage-console.service \
                /etc/systemd/system/vantage-console.service.d/*.conf 2>/dev/null | tail -1 | cut -d= -f2)
        run "ufw allow ${cport:-8090}/tcp"
    fi
    run "ufw --force enable"
    run "systemctl enable --now unattended-upgrades"
    echo "STAGE-OK harden"
}

# ============================================================================ STAGE 2: deps
# PostgreSQL + PostGIS from the pgdg apt repo (installTAK lines 174-177), and the JDK.
# TAK 5.7's .deb declares its Java dependency; `apt -f install` in stage 3 pulls it, but
# staging the repo + a known-good JDK here keeps stage 3 a clean dpkg.
stage_deps() {
    log "STAGE 2/9 dependencies (PostgreSQL, PostGIS, Java)"
    export DEBIAN_FRONTEND=noninteractive
    if [[ -n "$OFFLINE_REPO" ]]; then
        # the bundle carries PostgreSQL and PostGIS already; adding a network repository on a
        # disconnected box would only fail, slowly
        log "PostgreSQL comes from the offline bundle"
    elif [[ ! -f /etc/apt/sources.list.d/pgdg.list ]]; then
        run "install -d /usr/share/postgresql-common/pgdg"
        run "curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc"
        run "sh -c 'echo \"deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt \$(lsb_release -cs)-pgdg main\" > /etc/apt/sources.list.d/pgdg.list'"
        run "apt-get update -qq"
    fi
    # TAK 5.8's .deb depends on PostgreSQL 18 + PostGIS 3; installing it here keeps stage 3
    # a clean dpkg and avoids an orphan older cluster (5.7 wanted 15 - the dev box build of
    # 26 Aug 2026 ended up with both).
    apt_install postgresql-18 postgresql-18-postgis-3
    # OpenJDK 17 (temurin/openjdk both work; the .deb depends on a 17 JRE).
    apt_install openjdk-17-jre-headless
    # This script runs without `set -e` and run() ignores exit status, so apt failing here
    # used to print STAGE-OK and hand a broken box to stage 3. Worse, apt is SILENT when a
    # package is already installed - which is exactly the state a half-finished teardown
    # leaves: the postgresql-18 package present, but its data directory deleted. TAK's own
    # setup then says "Cannot find PostgreSQL data directory", the .deb fails to configure,
    # and every stage after it reports OK over a server that cannot start. Seen live on
    # edge-laptop1, 3 Sep 2026. So the stage proves what it is for instead of announcing it.
    if (( ! DRY )); then
        dpkg-query -W -f '${Status}' postgresql-18 2>/dev/null | grep -q "install ok installed" \
            || die "PostgreSQL 18 is not installed after the dependency stage"
        pg_lsclusters 2>/dev/null | tail -n +2 | grep -q . \
            || die "PostgreSQL 18 is installed but has NO cluster, so TAK cannot set up its database. A purge that left the package while its data directory was deleted does this. Create one with: sudo pg_createcluster 18 main --start"
        pg_isready -q 2>/dev/null \
            || die "PostgreSQL has a cluster but is not accepting connections. Start it with: sudo systemctl start postgresql"
    fi
    echo "STAGE-OK deps"
}

# ============================================================================ STAGE 3: install TAK
# dpkg the .deb, let apt settle its dependencies. Hard guard: refuse if TAK is already
# installed, because the .deb corrupts an existing install rather than upgrading it
# (cloud build-log, 2026-07-17 reinstall breakage). This stage is deliberately not
# re-runnable over a live install.
stage_install() {
    log "STAGE 3/9 install TAK Server"
    if dpkg-query -W -f '${Status}' takserver 2>/dev/null | grep -q "install ok installed"; then
        die "takserver is already installed on this box; provisioning refuses to reinstall over it"
    fi
    [[ -f "$DEB" ]] || die "--deb not found at $DEB"
    export DEBIAN_FRONTEND=noninteractive
    apt_install "'$DEB'"   # apt resolves the .deb's own dependencies, offline or not
    (( DRY )) || [[ -d /opt/tak ]] || die "install did not create /opt/tak"
    # /opt/tak exists as soon as the .deb UNPACKS. It is the postinst that sets up the
    # database and it can fail on its own, leaving the package half-configured with the
    # directory in place - which passed the check above and printed STAGE-OK over
    # "dpkg: error processing package takserver (--configure)".
    if (( ! DRY )); then
        local st; st=$(dpkg-query -W -f '${Status}' takserver 2>/dev/null || echo "not installed")
        [[ "$st" == "install ok installed" ]] \
            || die "the takserver package unpacked but did not configure (dpkg says: $st). Its postinst sets up the database, so the usual cause is PostgreSQL having no data directory. Nothing after this stage can work; fix that and re-run."
    fi
    echo "STAGE-OK install"
}

# ============================================================================ STAGE 4: database
# TAK ships its own DB setup: /opt/tak/db-utils/takserver-setup-db.sh creates the cot
# database and role in the local PostgreSQL. Re-runnable (it is written to be).
stage_database() {
    log "STAGE 4/9 database"
    (( DRY )) || [[ -x /opt/tak/db-utils/takserver-setup-db.sh ]] || die "no takserver-setup-db.sh - is TAK installed?"
    run "systemctl enable --now postgresql"
    run "/opt/tak/db-utils/takserver-setup-db.sh"
    # takserver-setup-db.sh prints its own ERROR and exits 0-ish, and run() ignores the
    # status either way, so this printed STAGE-OK directly under "Cannot find PostgreSQL
    # data directory". The database either exists or it does not: ask.
    if (( ! DRY )); then
        su - postgres -c "psql -Atqc \"SELECT 1 FROM pg_database WHERE datname='cot'\"" 2>/dev/null \
            | grep -q 1 \
            || die "the 'cot' database does not exist after the database stage. TAK cannot run without it; the log above says why, and a missing PostgreSQL data directory is the usual cause."
    fi
    echo "STAGE-OK database"
}

# ============================================================================ STAGE 5: certificates
# cert-metadata.sh carries the PKI identity and the CA password (env-overridable). Then, in
# order: root CA, issuing CA, the server cert (named by FQDN), and the webadmin client cert.
# `yes` answers makeCert's one "overwrite?" style prompt. Everything is chowned to tak.
stage_certs() {
    log "STAGE 5/9 certificates (root CA, issuing CA, server, webadmin)"
    local pass; pass=$(capass)
    (( DRY )) || [[ -f "$CERTS/cert-metadata.sh" ]] || die "no cert-metadata.sh"
    # Set the PKI identity fields and pin the CA password (installTAK edits these in place).
    run "sed -i -E \"s/^COUNTRY=.*/COUNTRY='$COUNTRY'/; s/^STATE=.*/STATE='$STATE'/; s/^CITY=.*/CITY='$CITY'/; s/^ORGANIZATION=.*/ORGANIZATION='$ORG'/; s/^ORGANIZATIONAL_UNIT=.*/ORGANIZATIONAL_UNIT='$ORG_UNIT'/\" $CERTS/cert-metadata.sh"
    # SECURITY: the .deb ships cert-metadata.sh with CAPASS defaulting to 'atakatak'. Even though
    # makeCert below uses our generated password via the env override, the weak default must not
    # remain in the file, or a later makeCert run without the env would mint certs under it.
    # Replace the default with the generated password (matching how installTAK pins it).
    run "sed -i -E 's/^CAPASS=.*/CAPASS=\${CAPASS:-$pass}/; s/^PASS=.*/PASS=\${PASS:-\$CAPASS}/' $CERTS/cert-metadata.sh"
    if [[ -f "$CERTS/files/${INT_CA}-signing.jks" ]]; then
        echo "certs already present, skipping generation"
    else
        run "cd $CERTS && CAPASS='$pass' PASS='$pass' ./makeRootCa.sh --ca-name $ROOT_CA"
        run "cd $CERTS && CAPASS='$pass' PASS='$pass' bash -c 'yes | ./makeCert.sh ca $INT_CA'"
        run "cd $CERTS && CAPASS='$pass' PASS='$pass' ./makeCert.sh server $FQDN"
        run "cd $CERTS && CAPASS='$pass' PASS='$pass' bash -c 'yes | ./makeCert.sh client webadmin'"
    fi
    run "chown -R tak:tak /opt/tak/certs"
    echo "STAGE-OK certs"
}

# ============================================================================ STAGE 6: CoreConfig
# Template CoreConfig from the example the .deb ships: point the server TLS at the FQDN
# keystore, harden the auth block (x509groups), and add the certificate auto-enrolment block
# so QR enrolment issues client certs. Federation stays OFF (not added). The LE connector is
# added in stage 7, once its keystore exists.
stage_coreconfig() {
    log "STAGE 6/9 CoreConfig"
    local ex=/opt/tak/CoreConfig.example.xml cc=/opt/tak/CoreConfig.xml pass; pass=$(capass)
    (( DRY )) || [[ -f "$ex" ]] || die "no CoreConfig.example.xml"
    run "cp -n $ex ${ex}.orig"
    run "cp $ex $cc"
    # server keystore -> this box's FQDN cert (installTAK line 548)
    run "sed -i 's#certs/files/takserver.jks#certs/files/${FQDN}.jks#g' $cc"
    # trust the ISSUING CA, not the root: enrolment signs device certs with the
    # intermediate, and a truststore holding only the root refuses every enrolled
    # device at the 8089 handshake - 'peer not verified', an ATAK that scans fine
    # and then IO-errors forever (bitten live, 28 Aug, the first product-built device)
    run "sed -i 's#certs/files/truststore-root.jks#certs/files/truststore-${INT_CA}.jks#g' $cc"
    # harden auth (installTAK line 570); only if still the bare <auth>
    run "sed -i 's#<auth>#<auth x509groups=\"true\" x509groupsDefaultRDN=\"true\" x509addAnonymous=\"false\" x509useGroupCache=\"true\" x509checkRevocation=\"true\">#g' $cc"
    # CRITICAL: the example carries the default keystore password 'atakatak', but makeCert built
    # every keystore with our generated CA password. Left unchanged, TAK cannot open the server
    # cert, the OAuth JWT key loads null, and the whole server fails to boot (dev box, 25 Aug
    # 2026: jwkSource NPE 'RSAPublicKey pub is null'). Point every store password at the real one.
    run "sed -i 's#keystorePass=\"atakatak\"#keystorePass=\"$pass\"#g; s#truststorePass=\"atakatak\"#truststorePass=\"$pass\"#g' $cc"
    # certificate auto-enrolment: the certificateSigning block is what lets a QR-scanned
    # device get its certificate signed. The old eval+sed injection mangled its own
    # quoting into a silent no-op (bitten live, 28 Aug: a built server whose QR could
    # never connect) - so this is now a quoted python insertion, VERIFIED, and the stage
    # dies rather than ship a server that cannot enrol.
    if (( DRY )); then
        echo "DRY: insert certificateSigning (CA ${INT_CA}) before <security> and verify"
    else
        SIGN_INT_CA="$INT_CA" SIGN_PASS="$pass" SIGN_O="$ORG" SIGN_OU="$ORG_UNIT" \
        python3 - "$cc" <<'PYCC'
import os, re, sys
p = sys.argv[1]
s = open(p).read()
# TAK's example ships a certificateSigning block INSIDE A COMMENT - a decoy that fooled
# both this guard and the verifier while TAK's first-boot rewrite stripped the comment,
# leaving every built server signing-less (28 Aug, two boxes running). Decide on real
# elements only: judge the comment-stripped text.
s_nc = re.sub(r"<!--.*?-->", "", s, flags=re.S)
if "<certificateSigning" not in s_nc:
    ca, pw = os.environ["SIGN_INT_CA"], os.environ["SIGN_PASS"]
    o, ou = os.environ["SIGN_O"], os.environ["SIGN_OU"]
    blk = ('    <certificateSigning CA="TAKServer">\n'
           '        <certificateConfig><nameEntries>\n'
           f'            <nameEntry name="O" value="{o}"/>\n'
           f'            <nameEntry name="OU" value="{ou}"/>\n'
           '        </nameEntries></certificateConfig>\n'
           f'        <TAKServerCAConfig keystore="JKS" keystoreFile="certs/files/{ca}-signing.jks" '
           f'keystorePass="{pw}" validityDays="30" signatureAlg="SHA256WithRSA" '
           f'CAkey="/opt/tak/certs/files/{ca}" CAcertificate="/opt/tak/certs/files/{ca}"/>\n'
           '    </certificateSigning>\n')
    m = re.search(r"^[ \t]*<security>", s, re.M)
    if not m:
        raise SystemExit("no <security> element to anchor on")
    s = s[:m.start()] + blk + s[m.start():]
    open(p, "w").write(s)
    # verify OUR OWN write before claiming success - the example's tab-indented
    # <security> made a four-space replace a silent no-op (28 Aug, strain three
    # of the silent-edit disease). Never again: no verification, no exit 0.
    ca_needle = "certs/files/%s-signing.jks" % os.environ["SIGN_INT_CA"]
    if ca_needle not in open(p).read():
        raise SystemExit("certificateSigning insertion did not verify")
PYCC
        # verify on OUR concrete CA filename - a needle no commented example can contain
        grep -q "certs/files/${INT_CA}-signing.jks" "$cc" \
            || die "certificateSigning did not land in CoreConfig"
    fi
    run "chown tak:tak $cc"
    echo "STAGE-OK coreconfig"
}

# ============================================================================ STAGE 7: Let's Encrypt
# certbot --standalone on port 80 (needs the FQDN resolving here; the console preflights it),
# export to a JKS keystore, and add the browser-trusted admin/enrolment connector on 8446.
# The renew hook re-exports the keystore after each certbot renewal.
# The 8446 connector, in ONE place. ORDER MATTERS: TAK makes the FIRST connector Spring
# Boot's primary server.ssl, and clientAuth="false" is valid for TAK's own additional-connector
# handling (it triggers loading the per-connector keystore) but INVALID for Spring Boot's
# primary (which wants the NONE|WANT|NEED enum). So this connector must be LAST, after 8443
# (the primary, which carries no clientAuth): then 8443 is a clean primary and 8446 loads its
# own keystore. Placed before 8443 it crashed the API; placed last with clientAuth=NONE the
# API ran but 8446 fell back to the CA cert. Last + "false" is the combination proven on a
# live box (dev box, 25 Aug 2026). Both the public-certificate and own-certificate paths call
# this, so that hard-won arrangement is written once.
add_8446_connector() {   # $1 keystore path (relative to /opt/tak)  $2 password  $3 _name
    local ks="$1" pass="$2" nm="$3" cc=/opt/tak/CoreConfig.xml
    if (( DRY )) || ! grep -q "$nm" "$cc" 2>/dev/null; then
        run "sed -i '/<connector port=\"8446\"/d' $cc"
        local con="<connector port=\"8446\" clientAuth=\"false\" _name=\"$nm\" keystore=\"JKS\" keystoreFile=\"$ks\" keystorePass=\"$pass\" enableAdminUI=\"true\" enableWebtak=\"false\" enableNonAdminUI=\"false\"/>"
        run "sed -i 's#</network>#${con}</network>#' $cc"
    fi
}

stage_letsencrypt() {
    log "STAGE 7/9 Let's Encrypt (8446 admin/enrolment)"
    # Let's Encrypt proves control of a public name over the public internet: it needs DNS
    # that resolves to this box and inbound :80. A disconnected box has neither, so this is
    # skipped rather than attempted - certbot would sit and fail, and the build would stop
    # one stage from finished. The server is complete without it: TAK still serves 8089 for
    # CoT and 8443 for the web and admin interface, on the certificates this box's own CA
    # issued in stage 5. What is missing is only the browser-TRUSTED certificate on 8446, and
    # nothing on a closed network can obtain one.
    # capass BEFORE the branch below: the own-certificate path uses $pass to build the
    # 8446 keystore and the assignment used to sit after it, so that path ran with an
    # empty password. It is the path a private build takes.
    local pass; pass=$(capass)
    # A certificate we were handed. No authority is contacted, no challenge is answered,
    # and nothing about this box is published by us - whatever was published happened when
    # the certificate was issued, wherever that was.
    if [[ -n "$CERT_FILE" ]]; then
        if (( DRY )); then
            echo "DRY: install the supplied certificate on 8446"
            echo "STAGE-OK letsencrypt (supplied certificate)"
            return 0
        fi
        if [[ ! -s "$CERT_FILE" || ! -s "$KEY_FILE" ]]; then
            log "the supplied certificate or key is missing or empty"
            log "  falling back to this box's own certificate on 8446, so the API still starts"
            local own0="$CERTS/files/${FQDN}.jks"
            [[ -f "$own0" ]] && add_8446_connector "certs/files/${FQDN}.jks" "$pass" "cert_https_own"
            echo "STAGE-OK letsencrypt (own certificate: nothing usable was supplied)"
            return 0
        fi
        # It has to be a real pair, and it has to be for this name, or 8446 comes up
        # serving something no device will accept and the build calls that success.
        if ! openssl x509 -in "$CERT_FILE" -noout >/dev/null 2>&1; then
            die "--cert-file is not a certificate"
        fi
        local cmod kmod
        cmod=$(openssl x509 -in "$CERT_FILE" -noout -modulus 2>/dev/null | openssl md5)
        kmod=$(openssl rsa  -in "$KEY_FILE"  -noout -modulus 2>/dev/null | openssl md5)
        [[ -n "$cmod" && "$cmod" == "$kmod" ]] || die "that key does not match that certificate"
        if ! openssl x509 -in "$CERT_FILE" -noout -checkhost "$FQDN" >/dev/null 2>&1; then
            log "WARNING: the certificate does not name $FQDN - devices will refuse it"
        fi
        run "openssl pkcs12 -export -in '$CERT_FILE' -inkey '$KEY_FILE' \
             -out /opt/tak/certs/files/le.p12 -name $FQDN -passout pass:$pass"
        run "rm -f $LE_JKS"
        run "keytool -importkeystore -destkeystore $LE_JKS -srckeystore /opt/tak/certs/files/le.p12 \
             -srcstoretype pkcs12 -deststorepass '$pass' -destkeypass '$pass' -srcstorepass '$pass'"
        run "chown tak:tak $LE_JKS /opt/tak/certs/files/le.p12"
        add_8446_connector "certs/files/le.jks" "$pass" "cert_https_LE"
        log "  8446 serves the certificate you supplied"
        log "  renewal is yours: this box did not obtain it and cannot renew it"
        echo "STAGE-OK letsencrypt (supplied certificate)"
        return 0
    fi
    if (( NO_LE )) || [[ -n "$OFFLINE_REPO" ]]; then
        local why="--no-letsencrypt"
        [[ -n "$OFFLINE_REPO" ]] && why="an offline build"
        log "no public certificate ($why): using this box's own instead"
        # 8446 is the connector devices enrol against, so leaving it out would give an offline
        # estate a working server it could never enrol a handset onto. The box's own CA issued
        # a server keystore in stage 5 under the same password, so 8446 is built exactly as the
        # public-certificate path builds it - same position, same clientAuth - and differs only
        # in which keystore it loads. The certificate is not browser-trusted, which on a closed
        # network is unavoidable and expected.
        local own="$CERTS/files/${FQDN}.jks"
        if (( DRY )) || [[ -f "$own" ]]; then
            add_8446_connector "certs/files/${FQDN}.jks" "$pass" "cert_https_own"
            log "  8446 serves this box's own certificate; browsers and devices will warn about it"
        else
            log "  no server keystore at $own, so no 8446 connector was added"
            log "  devices cannot enrol by QR without it; issue client certificates instead"
        fi
        log "  the server is usable either way: 8089 for CoT, 8443 for web and admin"
        echo "STAGE-OK letsencrypt (own certificate)"
        return 0
    fi
    # A certificate that was handed to us needs no certbot and no challenge at all.
    if [[ -z "$CERT_FILE" ]]; then
        run "apt-get install -y -qq certbot"
    fi
    if [[ ! -d /etc/letsencrypt/live/$FQDN ]]; then
        run "certbot certonly -d $FQDN -m $LE_EMAIL --standalone --agree-tos --no-eff-email --non-interactive"
    fi
    # certbot is allowed to fail: a box with no public A record cannot be validated, which
    # is CORRECT on a private build. What is not allowed is carrying on as though it
    # worked. This stage used to run the export hook against a private key that was never
    # written, point the 8446 connector at a keystore that does not exist, and print
    # STAGE-OK. TAK's API process will not start with a connector whose keystore is
    # missing, so 8443 went down with 8446 and the box served only 8089 - while the build
    # reported success. Seen live on edge-laptop1, 2 Sep 2026.
    if (( ! DRY )) && [[ ! -s /etc/letsencrypt/live/$FQDN/privkey.pem ]]; then
        log "no public certificate was issued for $FQDN"
        # The two routes fail for completely different reasons, and telling an operator
        # to check a public A record when they asked for the DNS route sends them to the
        # wrong place entirely.
        log "  the certificate authority could not reach this box to check the name. It"
        log "  needs to connect to port 80 from the internet, which nothing behind a router"
        log "  can offer. That is expected on a private box."
        log "  To have a trusted certificate here, get one however your DNS provider allows"
        log "  and build again with 'I already have a certificate'."
        log "  falling back to this box's own certificate on 8446, so the API still starts"
        local own="$CERTS/files/${FQDN}.jks"
        if [[ -f "$own" ]]; then
            add_8446_connector "certs/files/${FQDN}.jks" "$pass" "cert_https_own"
            log "  8446 serves this box's own certificate; browsers and devices will warn"
        else
            log "  no server keystore at $own, so no 8446 connector was added"
        fi
        log "  8089 for CoT and 8443 for web and admin are unaffected"
        echo "STAGE-OK letsencrypt (own certificate: no public one could be issued)"
        return 0
    fi
    # renew hook: LE PEM -> p12 -> jks, keyed with the CA password (mirrors the cloud box).
    if (( DRY )); then
        echo "DRY: write /opt/tak/renew-tak-le (certbot PEM -> le.jks export hook)"
    else
    cat > /opt/tak/renew-tak-le <<HOOK
#!/usr/bin/env bash
set -e
openssl pkcs12 -export -in /etc/letsencrypt/live/$FQDN/fullchain.pem \\
  -inkey /etc/letsencrypt/live/$FQDN/privkey.pem -out /opt/tak/certs/files/le.p12 \\
  -name $FQDN -passin pass:$pass -passout pass:$pass
rm -f $LE_JKS
keytool -importkeystore -destkeystore $LE_JKS -srckeystore /opt/tak/certs/files/le.p12 \\
  -srcstoretype pkcs12 -deststorepass "$pass" -destkeypass "$pass" -srcstorepass "$pass"
chown tak:tak $LE_JKS /opt/tak/certs/files/le.p12
HOOK
    fi
    run "chmod 700 /opt/tak/renew-tak-le && /opt/tak/renew-tak-le"
    # Replace the example's default 8446 connector with the browser-trusted LE one. The .deb's
    # CoreConfig.example ships a bare <connector port="8446" _name="cert_https"/>; leaving it in
    # means TWO connectors on 8446, which breaks the API process (dev box, 25 Aug 2026). Delete
    add_8446_connector "certs/files/le.jks" "$pass" "cert_https_LE"
    run "echo '0 */12 * * * root certbot -q renew && /opt/tak/renew-tak-le' > /etc/cron.d/tak-le-renew"
    echo "STAGE-OK letsencrypt"
}

# ============================================================================ STAGE 8: start + admin + report
# Enable and start TAK, wait for 8089 to answer, make webadmin an administrator, then print
# the facts the console needs to auto-enrol this box to the estate.
stage_start() {
    log "STAGE 8/9 start, admin, report"
    run "systemctl daemon-reload"
    run "systemctl enable takserver"
    run "systemctl restart takserver"
    if (( ! DRY )); then
        # TAK 5.7 on a 2 vCPU box opens 8089 in ~2-3 min (the messaging process); the API
        # process (8443/8446) follows a minute or so later. 5.8's FIRST boot runs longer
        # (schema migrations) and beat the old 6-minute wait on a healthy box (dev-cloud,
        # 26 Aug 2026: timed out, then opened 8089 moments later). Wait up to 15 minutes -
        # a slow success is a success, and a genuine failure still fails every probe.
        local i ok=0
        for i in $(seq 1 180); do
            ss -ltn 2>/dev/null | grep -qE '[:.]8089 ' && { ok=1; break; }
            sleep 5
        done
        (( ok )) || die "takserver did not open 8089 within ~15min (check journalctl -u takserver)"
        # 8089 is the MESSAGING process. The API is a separate process serving 8443 (web and
        # admin) and 8446 (enrolment), and it can be dead while 8089 is happily up: a broken
        # connector, or a database that was never created, stops it on its own. Waiting only
        # for 8089 is how two builds reported PROVISION-COMPLETE on a box serving CoT and
        # nothing else (edge-laptop1, 2 and 3 Sep 2026). Wait for the API too.
        local j api=0
        for j in $(seq 1 120); do
            ss -ltn 2>/dev/null | grep -qE '[:.]8443 ' && { api=1; break; }
            sleep 5
        done
        (( api )) || die "takserver opened 8089 but the API never opened 8443 within ~10min. The web and admin interface, device enrolment and QR codes all live in that process, so the box is NOT usable. Look at journalctl -u takserver for the connector or database error that stopped it."
        # 8446 is the enrolment connector and only exists if a keystore was wired for it, so
        # a private build without one is not a failure - but say so rather than leave it to
        # be discovered when a QR code will not scan.
        ss -ltn 2>/dev/null | grep -qE '[:.]8446 ' \
            || echo "NOTE 8446 is not listening, so device enrolment by QR will not work. That is expected only if no 8446 connector was wired for this box."
        java -jar /opt/tak/utils/UserManager.jar certmod -A /opt/tak/certs/files/webadmin.pem || \
            echo "WARN could not mark webadmin admin (do it manually)"
    fi
    echo "STAGE-OK start"
    echo
    if (( DRY )); then
        echo "DRY RUN COMPLETE - nothing was changed on this box. The plan above is what a"
        echo "live run will do. TAK Server is NOT installed and NOT running."
    else
        echo "ENROL-READY fqdn=$FQDN root_ca=$ROOT_CA int_ca=$INT_CA admin_cert=/opt/tak/certs/files/webadmin.p12"
        echo "The server is running. Auto-enrol it to the console with console-enrol-server."
    fi
}


# ============================================================================ STAGE 9: components
# Optional extras, requested as --components mediamtx,cloudtak. Each is idempotent and
# pinned to the estate's known-good versions. Firewall exposure is deliberately NOT
# changed here: MediaMTX's ports and CloudTAK's web port stay closed/loopback until an
# operator opens them on purpose (tailnet, reverse proxy, or ufw - their call, stated
# in the log).
MEDIAMTX_VER="v1.20.0"
MEDIAMTX_SHA256="952d5f7d31d1b448ab4da4509550594c511d42636db9d7bb175d377f4ede81df"
CLOUDTAK_TAG="v13.70.0"
MBTILESERVER_VER="v0.11.0"
MBTILESERVER_SHA256="0f752ad7038af9b3cff671f0e6bcadf26db6faebdf7da28da6716986d213d368"
OLLAMA_VER="v0.33.2"
OLLAMA_SHA256="9785247dea264d9072f09f6c9c0eb4b8e666892826a3d8388eba3e8fb9ed1db9"
# Node-RED runs as a container (npm -g as root pins nothing and runs hundreds of
# mutable lifecycle scripts; the image is one pinned, hash-addressed artefact).
NODERED_IMAGE_DIGEST="nodered/node-red@sha256:7d8a2bc628f80d2efc9a21832eac7c579a2c9ea4cda9040d3b0d4d68a1e36bc4"
NODERED_TAGNAME="4.1.13-22"

# Every fetched artefact is pinned by version AND sha256, and fails closed on a
# mismatch (Spec 002 AC5). The pins above are amd64; other architectures fail
# closed with a plain message rather than fetching something unverified.
fetch_verified() { # url sha256 dest
    local url="$1" sha="$2" dest="$3"
    if (( DRY )); then echo "DRY: fetch $url, verify sha256 $sha, install to $dest"; return 0; fi
    curl -fsSL -o "$dest" "$url" || { echo "ERR fetch failed: $url"; return 1; }
    echo "$sha  $dest" | sha256sum -c - >/dev/null 2>&1 || {
        echo "ERR sha256 MISMATCH for $url - refusing to install (expected $sha)"
        rm -f "$dest"; return 1; }
    return 0
}

require_amd64() { # component-name
    local arch; arch=$(dpkg --print-architecture 2>/dev/null || echo unknown)
    [[ "$arch" == "amd64" ]] && return 0
    echo "component $1: pinned artefacts are amd64 only in this release (box is $arch)"
    return 1
}

comp_mediamtx() {
    echo "-- component: mediamtx $MEDIAMTX_VER --"
    if [[ -x /usr/local/bin/mediamtx ]]; then
        echo "mediamtx already installed ($(/usr/local/bin/mediamtx --version 2>/dev/null || echo present)); skipping"
        return 0
    fi
    require_amd64 mediamtx || return 1
    fetch_verified "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VER}/mediamtx_${MEDIAMTX_VER}_linux_amd64.tar.gz" \
        "$MEDIAMTX_SHA256" /tmp/mediamtx.tgz || return 1
    run "tar -xzf /tmp/mediamtx.tgz -C /tmp mediamtx mediamtx.yml"
    run "install -m 755 /tmp/mediamtx /usr/local/bin/mediamtx"
    run "install -d /usr/local/etc"
    if [[ ! -f /usr/local/etc/mediamtx.yml ]]; then
        run "install -m 644 /tmp/mediamtx.yml /usr/local/etc/mediamtx.yml"
    fi
    if (( DRY )); then
        echo "DRY: write /etc/systemd/system/mediamtx.service (unit mirroring the deployed kit)"
    else
        cat > /etc/systemd/system/mediamtx.service <<'UNIT'
[Unit]
Description=mediamtx RTSP relay
After=network.target
[Service]
ExecStart=/usr/local/bin/mediamtx /usr/local/etc/mediamtx.yml
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
    fi
    run "systemctl daemon-reload"
    run "systemctl enable --now mediamtx"
    run "rm -f /tmp/mediamtx.tgz /tmp/mediamtx /tmp/mediamtx.yml"
    echo "mediamtx installed. Its ports (RTSP 8554, RTMP 1935, HLS 8888) are NOT opened in the firewall; open them deliberately when a producer needs in."
}

comp_cloudtak() {
    echo "-- component: CloudTAK $CLOUDTAK_TAG --"
    export DEBIAN_FRONTEND=noninteractive
    run "apt-get install -y -qq docker.io docker-compose-v2 git"
    run "systemctl enable --now docker"
    if [[ ! -d /opt/CloudTAK ]]; then
        run "git clone --branch $CLOUDTAK_TAG --depth 1 https://github.com/dfpc-coe/CloudTAK.git /opt/CloudTAK"
    else
        echo "/opt/CloudTAK already present; keeping the existing checkout"
    fi
    local pgpass ctadmin
    if (( DRY )); then pgpass="DRY-PLACEHOLDER"; ctadmin="DRY-PLACEHOLDER"; else
        pgpass=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
        ctadmin=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
    fi
    if (( DRY )); then
        echo "DRY: write /opt/CloudTAK/.env (TAK_FQDN=$FQDN, generated postgres password)"
        echo "DRY: write /opt/CloudTAK/docker-compose.override.yml (loopback-bound web port, TAK_FQDN host-gateway)"
    else
        if [[ ! -f /opt/CloudTAK/.env ]]; then
            cat > /opt/CloudTAK/.env <<ENV
TAK_FQDN=$FQDN
POSTGRES_PASSWORD=$pgpass
ENV
            chmod 600 /opt/CloudTAK/.env
        fi
        if [[ ! -f /opt/CloudTAK/docker-compose.override.yml ]]; then
            cat > /opt/CloudTAK/docker-compose.override.yml <<'OVR'
# Written by vantage-tak-provision. Docker's published ports bypass ufw entirely, so
# the stock 0.0.0.0 bindings would expose CloudTAK on every interface regardless of
# the firewall. Everything binds loopback until an operator exposes it on purpose.
# Containers reach the TAK server by its certificate name via the host gateway.
services:
    api:
        extra_hosts:
            - "${TAK_FQDN}:host-gateway"
        ports: !override
            - "127.0.0.1:5000:5000"
    events:
        extra_hosts:
            - "${TAK_FQDN}:host-gateway"
    retention:
        extra_hosts:
            - "${TAK_FQDN}:host-gateway"
    postgis:
        environment:
            - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
OVR
        fi
    fi
    echo "building and starting the CloudTAK stack (first build takes a while on a small box)"
    run "cd /opt/CloudTAK && docker compose up -d --build --quiet-pull"
    if (( ! DRY )); then
        if java -jar /opt/tak/utils/UserManager.jar usermod -A -p "$ctadmin" cloudtak-admin >/dev/null 2>&1; then
            { echo "cloudtak-admin $ctadmin"; } > /root/cloudtak-admin-credentials
            chmod 600 /root/cloudtak-admin-credentials
            echo "TAK user 'cloudtak-admin' (ROLE_ADMIN) minted; credentials in /root/cloudtak-admin-credentials (root only)"
        else
            echo "WARN could not mint cloudtak-admin on TAK (UserManager); create it by hand"
        fi
        ver=$(git -c safe.directory='*' -C /opt/CloudTAK describe --tags --always 2>/dev/null || true)
        [[ -n "$ver" ]] && { printf '%s\n' "$ver" > /etc/tak-cloudtak-version; chmod 644 /etc/tak-cloudtak-version; }
    fi
    echo "CloudTAK is up on 127.0.0.1:5000 (loopback by design). REMAINING BY HAND: browse to it"
    echo "(ssh tunnel or tailnet), complete CloudTAK's own first-run, and add its connection to"
    echo "this TAK server using the cloudtak-admin credentials. Exposure beyond loopback is an"
    echo "operator decision."
}

comp_mosquitto() {
    echo "-- component: mosquitto (distribution package) --"
    if systemctl cat mosquitto >/dev/null 2>&1 && [[ -f /etc/mosquitto/conf.d/vantage-loopback.conf ]]; then
        echo "mosquitto already installed and configured; skipping"
        return 0
    fi
    export DEBIAN_FRONTEND=noninteractive
    run "apt-get install -y -qq mosquitto"
    # Explicit loopback listener rather than trusting the packaged default
    # (ADR 003): the broker serves the box's own integrations, nothing else,
    # until an operator decides otherwise.
    if (( DRY )); then
        echo "DRY: write /etc/mosquitto/conf.d/vantage-loopback.conf (listener 1883 on 127.0.0.1)"
    else
        cat > /etc/mosquitto/conf.d/vantage-loopback.conf <<'CONF'
# Written by vantage-tak-provision: loopback only, by design (ADR 003).
listener 1883 127.0.0.1
allow_anonymous true
CONF
    fi
    run "systemctl enable --now mosquitto"
    run "systemctl restart mosquitto"
    echo "mosquitto installed, listening on 127.0.0.1:1883 only. Exposure beyond loopback is an operator decision."
}

comp_maps() {
    echo "-- component: maps (mbtileserver $MBTILESERVER_VER) --"
    if [[ -x /usr/local/bin/mbtileserver ]] && systemctl cat mbtileserver >/dev/null 2>&1; then
        echo "mbtileserver already installed; skipping (tiles arrive via the Store shelf push)"
        return 0
    fi
    require_amd64 maps || return 1
    fetch_verified "https://github.com/consbio/mbtileserver/releases/download/${MBTILESERVER_VER}/mbtileserver_${MBTILESERVER_VER}_linux_amd64.zip" \
        "$MBTILESERVER_SHA256" /tmp/mbtileserver.zip || return 1
    run "apt-get install -y -qq unzip"
    run "unzip -o -q /tmp/mbtileserver.zip -d /tmp"
    run "install -m 755 /tmp/mbtileserver_${MBTILESERVER_VER}_linux_amd64 /usr/local/bin/mbtileserver"
    run "install -d -m 755 /opt/tak-maps"
    if (( DRY )); then
        echo "DRY: write /etc/systemd/system/mbtileserver.service (serves /opt/tak-maps on 8080)"
    else
        cat > /etc/systemd/system/mbtileserver.service <<'UNIT'
[Unit]
Description=mbtileserver offline map tiles
After=network.target
[Service]
ExecStart=/usr/local/bin/mbtileserver --dir /opt/tak-maps --port 8080
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
    fi
    run "systemctl daemon-reload"
    run "systemctl enable --now mbtileserver"
    run "rm -f /tmp/mbtileserver.zip /tmp/mbtileserver_${MBTILESERVER_VER}_linux_amd64"
    echo "mbtileserver installed serving /opt/tak-maps on 8080. Port 8080 is NOT opened in the"
    echo "firewall; open it when devices need tiles. No tilesets yet: push map packs from the"
    echo "Store shelf, and the health check reads WARN until the first one lands."
}

comp_ollama() {
    echo "-- component: ollama $OLLAMA_VER (needs internet: the release archive is ~1.4GB) --"
    if [[ -x /usr/local/bin/ollama ]]; then
        echo "ollama already installed ($(/usr/local/bin/ollama --version 2>/dev/null | head -1 || echo present)); skipping"
        return 0
    fi
    require_amd64 ollama || return 1
    # The official curl|sh installer is deliberately not used (Spec 002 AC5):
    # pinned release archive, verified hash, our own unit.
    export DEBIAN_FRONTEND=noninteractive
    run "apt-get install -y -qq zstd"
    fetch_verified "https://github.com/ollama/ollama/releases/download/${OLLAMA_VER}/ollama-linux-amd64.tar.zst" \
        "$OLLAMA_SHA256" /tmp/ollama.tar.zst || return 1
    run "tar --zstd -xf /tmp/ollama.tar.zst -C /usr/local"
    run "id -u ollama >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d /var/lib/ollama -m ollama"
    if (( DRY )); then
        echo "DRY: write /etc/systemd/system/ollama.service (User=ollama, 127.0.0.1:11434)"
    else
        cat > /etc/systemd/system/ollama.service <<'UNIT'
[Unit]
Description=Ollama local models
After=network.target
[Service]
User=ollama
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_MODELS=/var/lib/ollama/models
ExecStart=/usr/local/bin/ollama serve
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
    fi
    run "systemctl daemon-reload"
    run "systemctl enable --now ollama"
    run "rm -f /tmp/ollama.tar.zst"
    echo "ollama installed, bound to 127.0.0.1:11434 only. No model is downloaded by this"
    echo "installer (multi-GB, unsigned): pull one deliberately when ready, for example"
    echo "  sudo -u ollama OLLAMA_MODELS=/var/lib/ollama/models ollama pull llama3.2:3b"
}

comp_lanntp() {
    echo "-- component: lan-ntp (chrony serves time to this box's LAN) --"
    export DEBIAN_FRONTEND=noninteractive
    run "apt-get install -y -qq chrony"
    # The box's REAL LAN, not the first kernel route: on a box with Docker that
    # is 172.17.0.0/16, the bridge (the deployable kit build hit the same trap). Prefer
    # the default route's interface; offline (no default route), take the first
    # kernel route on a physical-looking interface.
    local dev net
    dev=$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit}}')
    if [[ -n "$dev" ]]; then
        net=$(ip -4 route show proto kernel dev "$dev" 2>/dev/null | awk '/src/{print $1; exit}')
    else
        net=$(ip -4 -o route show proto kernel 2>/dev/null \
              | awk '$0 ~ /src/ && $3 !~ /^(docker|br-|veth|lo|tailscale)/ {print $1; exit}')
    fi
    [[ "$net" =~ ^[0-9.]+/[0-9]+$ ]] || { echo "ERR cannot determine this box's LAN subnet"; return 1; }
    if (( DRY )); then
        echo "DRY: write /etc/chrony/conf.d/vantage-lanntp.conf (allow $net, local stratum 10)"
    else
        cat > /etc/chrony/conf.d/vantage-lanntp.conf <<CONF
# Written by vantage-tak-provision: serve time to the kit LAN so device clocks
# hold with the cable out. Scoped to this box's own subnet, never all of RFC1918.
allow $net
local stratum 10
CONF
    fi
    run "systemctl enable --now chrony"
    run "systemctl restart chrony"
    echo "chrony serves $net (local stratum 10 when upstream is unreachable). udp/123 is NOT"
    echo "opened in the firewall; open it when devices should sync from this box."
}

comp_nodered() {
    echo "-- component: node-red ${NODERED_TAGNAME} (container; needs internet OR a staged image) --"
    if systemctl cat node-red >/dev/null 2>&1 && systemctl is-active node-red >/dev/null 2>&1; then
        echo "node-red already installed and running; skipping"
        return 0
    fi
    export DEBIAN_FRONTEND=noninteractive
    run "apt-get install -y -qq docker.io"
    run "systemctl enable --now docker"
    if (( ! DRY )); then
        if ! docker image inspect "$NODERED_IMAGE_DIGEST" >/dev/null 2>&1; then
            echo "pulling the pinned image by digest (or stage it offline via the Store's docker-load path)"
            docker pull "$NODERED_IMAGE_DIGEST" || {
                echo "ERR cannot pull $NODERED_IMAGE_DIGEST and no staged image present."
                echo "Offline: capture it on a connected machine (docker save) and load it via the"
                echo "Store's Offline deploy, then run this component again."
                return 1; }
        fi
    else
        echo "DRY: docker pull $NODERED_IMAGE_DIGEST unless already loaded"
    fi
    run "install -d -m 775 -o 1000 -g 1000 /opt/node-red"
    if (( DRY )); then
        echo "DRY: write /etc/systemd/system/node-red.service (container, 127.0.0.1:1880)"
    else
        cat > /etc/systemd/system/node-red.service <<UNIT
[Unit]
Description=Node-RED flows (pinned container)
After=docker.service
Requires=docker.service
[Service]
ExecStartPre=-/usr/bin/docker rm -f node-red
ExecStart=/usr/bin/docker run --rm --name node-red -p 127.0.0.1:1880:1880 -v /opt/node-red:/data ${NODERED_IMAGE_DIGEST}
ExecStop=/usr/bin/docker stop node-red
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
    fi
    run "systemctl daemon-reload"
    run "systemctl enable --now node-red"
    echo "node-red running as a pinned container on 127.0.0.1:1880 only (the loopback publish"
    echo "also keeps Docker from bypassing ufw). Exposure beyond loopback is an operator decision."
}

comp_takbot() {
    echo "-- component: takbot (MilUX-vendored artefact; staged via the Store, never pip) --"
    if systemctl cat takbot >/dev/null 2>&1 && [[ -e /opt/takbot/run.sh ]]; then
        echo "takbot already installed; skipping"
        return 0
    fi
    # TAKBOT is python + pytak + takbot.py. It ships as a self-contained vendored
    # artefact (the ADR 002 vendoring pattern), staged to /opt/takbot through the
    # Store shelf; installing from the internet or pip at deploy time is refused.
    # Its TAK credential is minted by the console's gated enrol-device flow, not
    # by this installer.
    if [[ ! -e /opt/takbot/run.sh ]]; then
        echo "ERR takbot artefact not staged: push the vendored takbot artefact from the"
        echo "Store shelf to /opt/takbot first, then run this component again."
        return 1
    fi
    if (( DRY )); then
        echo "DRY: write /etc/systemd/system/takbot.service (runs /opt/takbot/run.sh)"
    else
        cat > /etc/systemd/system/takbot.service <<'UNIT'
[Unit]
Description=TAKBOT CoT chat bot
After=network.target takserver.service
[Service]
ExecStart=/opt/takbot/run.sh
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT
    fi
    run "systemctl daemon-reload"
    run "systemctl enable --now takbot"
    echo "takbot installed from the staged artefact. Enrol its TAK credential from the console"
    echo "(a gated action) and place it where /opt/takbot/run.sh expects."
}

stage_components() {
    log "STAGE 9/9 components (${COMPONENTS:-none})"
    if [[ -z "$COMPONENTS" ]]; then
        echo "STAGE-OK components (none requested)"
        return 0
    fi
    local c
    IFS=, read -ra _comps <<<"$COMPONENTS"
    for c in "${_comps[@]}"; do
        case "$c" in
            mediamtx)  comp_mediamtx ;;
            cloudtak)  comp_cloudtak ;;
            mosquitto) comp_mosquitto ;;
            maps)      comp_maps ;;
            ollama)    comp_ollama ;;
            lanntp)    comp_lanntp ;;
            nodered)   comp_nodered ;;
            takbot)    comp_takbot ;;
            *) die "unknown component '$c'" ;;
        esac
    done
    echo "STAGE-OK components"
}

# ---------------------------------------------------------------------------- run
main() {
    echo "vantage-tak-provision: fqdn=$FQDN org=$ORG/$ORG_UNIT components=${COMPONENTS:-none} dry=$DRY"
    for s in harden deps install database certs coreconfig letsencrypt start components; do
        want_stage "$s" && "stage_$s"
    done
    echo "PROVISION-COMPLETE $FQDN"
}
main
