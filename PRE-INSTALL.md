![Vantage](assets/vantage-banner.svg)

# Before you install Vantage

Vantage's [README](README.md) begins at a box that already exists: *a server running Ubuntu
22.04 or 24.04 LTS, reachable, with root access, and a DNS name pointing at it*. This guide is
everything before that line. It covers buying the kit, configuring the router, getting a name
and a certificate, and installing Ubuntu, and it ends by handing you a filled-in record you
type straight into the Vantage installer.

It is written from builds we have actually done, so where a choice cost us an afternoon the
reason is written next to it. Follow it in order. Every step produces a value the next step
needs.

---

## Decide the shape first

Two shapes of box, and almost everything below forks on the answer.

| | **Connected server** | **Private kit** |
|---|---|---|
| Where it lives | A hosting provider, on the public internet | Behind your own router, on a field LAN |
| Its address | Public, given by the provider | Private, given by your router |
| Its name | A public DNS record pointing at it | A public record *and* a local override |
| Its certificate | The Vantage build earns it during the install | Pre-issued off-box before the install |
| Private network | Optional | Usual, and it decides what the public record points at |
| Router steps | None | All of them |
| This guide | Steps 2, 4, 5 | Steps 1 to 5 |

A laptop or small box that goes out with a team is a private kit, even if it has a public name
and even if it spends most of its life online.

---

## What to buy

**The minimum, whatever you buy.** Two CPU cores and 4GB of memory run a TAK server; 8GB is
comfortable. An NVMe or SSD, not spinning disk, because the database is the thing that hurts.
Wired ethernet on the box. A screen and keyboard if it will run as a kiosk, and if it will not,
you still want them for the install.

**A private kit needs a router you can log into over SSH.** The split-horizon DNS in step 1
is written with `uci`, so the router needs OpenWrt underneath. Consumer router firmware
generally will not do it.

### What we use

The worked examples throughout are this kit, so you can price the whole thing.

| Part | What we run | Why this one |
|---|---|---|
| Server | Dell Inspiron desktop, 512GB Phison NVMe | Ordinary hardware, deliberately. Nothing here needs a server chassis |
| Kiosk box | A laptop | Screen, keyboard and battery in one, which is most of a field appliance already |
| Router | GL.iNet GL-MT3000 (Beryl AX) | OpenWrt underneath, so `uci` works; wifi repeater on the WAN side; pocket-sized |
| Switch | Unmanaged 5-port | The router has one LAN port. Anything past one wired client needs this |
| Installer media | USB 3 stick, 8GB or larger | See step 4.2 before you buy the cheapest one |
| Handset | Samsung S23 | Any ATAK-capable Android |

---

## The build record

Fill this in before you touch anything. Every field is asked for later, by the router, by your
DNS provider, by the Ubuntu installer or by Vantage itself, and deciding them on the fly is how
a subnet clash or a name mismatch gets built in.

> **Open [`PRE-INSTALL-PLANNER.html`](PRE-INSTALL-PLANNER.html) in a browser and fill it in
> there instead.** It is a single file that runs with no internet and sends nothing anywhere.
> Put your details in once and every command in this guide appears with your values already
> in it, ready to copy, so there are no blanks left to fill in by hand at a terminal. It also
> checks the obvious mistakes: an address outside your own network, the router's address given
> to the box, a wifi MAC where the wired one belongs.

| Field | Yours | Example |
|---|---|---|
| Box name, for humans | | A deployable kit |
| Hostname | | `kit1` |
| FQDN | | `kit1.example.com` |
| Estate name in Vantage | | `kit1` |
| Kit LAN | | `192.168.50.0/24` |
| Router address | | `192.168.50.1` |
| Box address | | `192.168.50.10` |
| Box ethernet MAC | | read off the box at step 1.4 |
| Already on this LAN | | anything else with a reservation. Do not clash |
| Public A record points at | | the box's tailnet address |
| Certificate | | pre-issued off-box, DNS-01 |
| Console bind address | | `192.168.50.10` |

Keep it. It is the configuration record for the box, and the first thing you will want when
someone asks why the kit stopped resolving in eighteen months.

---

## Step 1 — the router

Private kit only. Do the whole of this step before the box exists, because two of its outputs
(the reserved address, the local name) are things the Ubuntu installer will use.

### 1.1 Update the firmware, and do not keep settings

Update first, and decline the offer to carry your settings across. Everything below is quick to
redo and a settings blob carried across a firmware version is a fault you cannot see.

### 1.2 Move the LAN off the vendor default

Every GL.iNet router ships on `192.168.8.0/24`. That matters the moment your kit meets another
one, or any venue whose network uses the same range: **if the WAN hands out an address in the
same range as the LAN, routing breaks**, and the symptom reads as a dead internet connection
rather than an addressing clash. It costs nothing to move and removes the whole class of fault.

Use a different range on every kit you build, so two of your own kits can never clash
either. The examples below use `192.168.50.0/24`.

Settle the subnet before anything else in this step. The DNS entry and the certificate mapping
both point at an address inside it.

### 1.3 Wifi

**WPA2-PSK/WPA3-SAE mixed mode.** Not WPA3-SAE alone, which excludes any client that cannot do
WPA3, and in the field "cannot join" costs more than the marginal gain. Not `WPA/WPA2-PSK`,
which looks like the compatible option and is the worst of the set: it drags in WPA1 and TKIP
and forces the radio to legacy rates.

Mixed mode is downgrade-attackable, so the passphrase carries the security. That matters more
here than on an ordinary network, because **TAK binds to the LAN interface on a kit like this,
not to the VPN, so anything that joins this wifi can reach the TAK server.** Use a long
passphrase and record it somewhere controlled.

### 1.4 Reserve the box's address

**Pin the address on the router, and leave the box on DHCP.** This is the recommended route
and it is worth saying why, because setting a static address on the box during the Ubuntu
install also works and is the more obvious thing to do.

- The reservation survives a rebuild. Reinstall Ubuntu and the box comes back on the same
  address with nothing to remember.
- One place holds the truth. A static address on the box and a reservation on the router can
  disagree, and the box wins silently.
- It exists before the box does. The Ubuntu installer runs its own sshd, so the address
  answering during the USB boot tells you the reservation works before the install has
  finished.
- Vantage bakes the address in. The console's bind address is what the kiosk points its
  browser at, so an address that moves takes the kiosk down with it.

#### First, get the MAC, and mind the order

There is a small chicken-and-egg here and it catches people out. **A reservation is keyed to
the box's ethernet MAC, and you cannot know the MAC until the box has been on the network
once.** So the box goes on the network before it has an operating system, purely so the router
can see it.

Plug the box into the router's LAN (through the switch if you have one) and bring it up one of
three ways:

1. **A blank box: boot the Ubuntu installer USB.** The installer brings networking up on its
   own and takes an ordinary lease, which is all you need. Leave it sitting on the first
   screen; you are not installing yet. This means writing the stick before you finish the
   router, so jump ahead to **4.2** and make it now, then come back.
2. **A box with any working OS on it already:** just boot it.
3. **Neither:** many machines show the ethernet MAC in the firmware setup screen, and some
   carry it on a label underneath.

Then open the router's admin page, find the client list, and read the MAC off the entry that
appeared. Write it on the build record.

> **Two traps, both specific to laptops.**
>
> **Take the ethernet MAC, not the wifi one.** A laptop with wifi on shows up twice in the
> client list, and the two entries have different MACs. Reserve the wrong one and the box
> takes the reserved address only when it is on wifi, which is exactly when you do not want
> it. Turn wifi off for this, or match the entry to the wired connection deliberately.
>
> **If the box has no ethernet port, the MAC belongs to the adapter.** A USB or USB-C dongle
> carries its own MAC, so the reservation follows the dongle rather than the box. That is
> workable as long as you know it: label that adapter, keep it with the kit, and record it on
> the build record. Swap it for another one and the reservation is dead.

#### Then write the reservation

On GL.iNet firmware 4.8.1 the Clients page **cannot reserve an address of your choosing**, so
write it with `uci`. A static lease is its own `host` section, not a dnsmasq option:

```bash
ssh root@192.168.50.1 "uci add dhcp host \
  && uci set dhcp.@host[-1].name='kit1' \
  && uci set dhcp.@host[-1].mac='AA:BB:CC:DD:EE:FF' \
  && uci set dhcp.@host[-1].ip='192.168.50.10' \
  && uci commit dhcp && /etc/init.d/dnsmasq restart"
```

**Check the router still answers, before anything else.** Ask it to resolve a name it should
know. That is the test, because a name resolving is the thing you actually need, and it can
only happen if dnsmasq is alive and serving:

```bash
dig @192.168.50.1 kit1.example.com +short
```

An address back is the pass. Silence or a timeout means dnsmasq did not come back, and nothing
on that router is getting DHCP or DNS until it does.

Do not test this by looking for the process. OpenWrt runs BusyBox, `pgrep` is often not there
at all, and a missing command reports as a stopped service. `pidof dnsmasq` works if you want a
second opinion, but the query above is the real answer.

> **A bad entry takes the whole kit LAN down, quietly.** An invalid or empty `mac` makes
> dnsmasq refuse to start, and when it does not start there is no DHCP and no local DNS for
> *anything* on that router, including boxes you built months ago. It fails silently: the
> command returns, the config is written, and the network simply stops handing out addresses.
> Seeing the entry in `uci show` proves nothing, which is why the check above asks the service
> whether it is alive rather than asking the config whether it was saved.
>
> If it says `STOP`, remove the entry you just added by its own index, never `[-1]`, and
> restart: `uci delete dhcp.@host[N]; uci commit dhcp; /etc/init.d/dnsmasq restart`. Then
> `logread | grep -i dnsmasq | tail -20` will name what it objected to.
>
> And if DHCP is down you may not be able to reach the router to fix it, because your own
> machine has no address either. Give yourself a static one on that subnet first.

**Then make the box ask again.** A lease already handed out does not change because you wrote
a reservation. Reboot the box, or pull and replace the cable, and confirm it comes back on the
address you reserved. If you used the Ubuntu installer to get the MAC, reboot it now: its
network screen showing the reserved address is your proof, and it is the last easy moment to
catch a wrong MAC.

Set a static address on the box instead only when there is no router you control.

### 1.5 Split-horizon DNS

The kit's name has to resolve on the kit LAN with the WAN unplugged. The router's dnsmasq
answers for it locally, while the public record answers for everyone else. That is the
split horizon, and it is what lets one name work in both modes.

```bash
ssh root@192.168.50.1 "uci add_list dhcp.@dnsmasq[0].address='/kit1.example.com/192.168.50.10' \
  && uci commit dhcp && /etc/init.d/dnsmasq restart"
```

Two deliberate choices in that line:

- **`uci` over SSH, not the LuCI web UI.** The "Custom options" field moves between tabs across
  LuCI versions and GL.iNet ships a patched build, so the UI path is a hunt. `uci` writes the
  same directive and is version-independent.
- **The `address=/domain/ip` form**, not a host entry, because it answers authoritatively for
  the domain and everything under it, and never forwards upstream.

### 1.6 Verify, and know the trap

```bash
dig @192.168.50.1 kit1.example.com +short
```

Expect the LAN address. If it times out, dnsmasq did not come back after the restart: check
`logread | grep -i dnsmasq | tail -20` on the router, which will name what it objected to.

Test this **with the WAN connected**, not unplugged. Offline only shows that nothing
contradicted the local answer. With an uplink present and the public record pointing somewhere
else, getting the LAN address back proves the local override beats upstream, which is the thing
that could actually fail.

**The trap.** The router intercepts port 53 from its LAN clients and answers itself, so
`dig @1.1.1.1` from inside the kit still returns the *local* answer. This is desirable kit
behaviour and a misleading test. Check the public face from outside the kit, or over
DNS-over-HTTPS.

---

## Step 2 — the name

You need a name before you build, because it becomes the server's certificate and it is what
devices connect to. **Needing a name is not the same as publishing one.**

**Fork here, and it decides how much of this step applies:**

- **Building fully private, nothing on the internet?** You still need a name, and you already
  made it work at step 1.5 on the router. Skip 2.1 and 2.2, read 2.3 so you know what you are
  avoiding, and skip 2.4. Then go to *The fully private build* in step 3.
- **Building something that has to be reachable**, a demonstration or playground server?
  All of this step applies.

The rest of this step assumes the second. Any DNS provider works. The examples below name no
provider: substitute yours wherever one is called for.

### 2.0 If you do not have a domain yet

Skip this if you already have one and can edit its DNS.

1. **Buy a name** from any registrar. A `.com` or a `.uk` costs a few pounds a year, and one
   name is enough: every box you ever build can be a different label under it, so
   `kit1.example.com` and `kit2.example.com` cost nothing extra.
2. **Point it at a DNS provider whose web page you can actually use.** The registrar you
   bought from will host DNS itself, which is fine. If you want scripted renewals, prefer one
   with an API token and a certbot plugin; most of the large providers have both. Moving a
   name to another provider means
   changing its **nameservers** at the registrar, which the new provider walks you through and
   which takes a few hours to settle.
3. **Check you can add a record** before going further. If you cannot reach the page that
   edits DNS, nothing in step 2 or step 3 will work.

The name does not have to resolve to anything yet. You have to be able to edit it.

### 2.1 The A record

**Skip this if the box publishes nothing.** A fully private build needs no A record at all,
and 2.3 covers why. Add one only if something outside the box's own network has to reach it
by name.

Otherwise, in the zone, add an **A record** for the hostname, pointing at the address the
outside world should reach.

**Set it to DNS only. Any proxying must be off.** Some providers can sit in front of your
traffic rather than just answering for the name; on Cloudflare, for instance, that is the grey
cloud rather than the orange one. The proxy breaks TAK's ports (8089, 8443, 8446) and it breaks the HTTP-01
certificate challenge. This has bitten us on a public box and it is not obvious from the
symptom.

### 2.2 What the record points at

- **Connected server:** its public address.
- **Private kit:** its VPN address, once the box has one. The kit LAN gets its answer from the
  router (step 1.5), so the public record exists for the connected tier only. On our kits that
  is the tailnet address.

For a private kit the record cannot be finished until the box is built and on the VPN, which
is step 5. Create it then, and put it on the checklist, because a record still pointing at the
previous box sends connected clients to the wrong server.

### 2.3 What publishing a name gives away

Worth deciding before you publish anything, because two of these cannot be undone.

**A certificate publishes the name, permanently.** Every publicly-trusted certificate is
written to public Certificate Transparency logs. The moment you issue one for
`kit1.example.com`, anyone can search for it and learn that the box exists, what you called it,
your naming convention, and when you built it. That has nothing to do with your DNS record and
you cannot take it back. For a small estate that is usually fine. For kit whose existence is
itself worth knowing about, it is not.

- **A wildcard certificate publishes only the wildcard.** One certificate for `*.example.com`
  covers every box you build, and the individual names never appear in a log. It is the single
  most effective thing you can do here, and it costs one certificate instead of many.
- **Or use a domain that says nothing.** A name nobody can tie to you leaks the least.

**An A record publishes where the box is.** Pointing a public name at an address inside your
network tells anyone who asks what your internal addressing looks like. It grants no access,
but it is free reconnaissance and it confirms the box is real.

- **You do not need an A record to get a certificate.** DNS-01 validates against a TXT record.
  Nothing in the certificate process requires you to publish where the box lives. If nobody
  needs to reach it by name from outside, publish no A record at all: the name still resolves
  on the kit LAN from step 1.5.
- **If you do publish one, point it at the VPN address**, not the LAN address. It still tells a
  reader which VPN you use, but not how your network is laid out.

The tightest posture that still gives you a trusted certificate is therefore: a wildcard
certificate, no A record, and the name served locally. Decide it now rather than after the log
entry exists.

### 2.4 The API token, if you want scripted certificates

Step 3 issues the certificate with certbot's plugin for your DNS provider, which needs a
token. Install the plugin for yours: the package is usually `python3-certbot-dns-<provider>`
and the flags follow the same shape, `--dns-<provider>` and `--dns-<provider>-credentials`.

In your provider's console, create an API token that can **edit DNS records in that one zone**
and nothing wider.

**Decide where that token lives, deliberately.** It can edit your DNS. Keep it on the machine
that issues certificates, not on a box you take out with you, which is the
whole reason step 3 issues the certificate off-box and carries it. A token on a kit is a token
you have shipped.

---

## Step 3 — the certificate, before the build

**Connected server:** skip this. The Vantage build earns the certificate itself during the
install.

**Private kit:** you have a choice, and it is a real one. A publicly-trusted certificate is
optional, and a kit that publishes nothing still enrols devices by QR. What it buys you is the
absence of a warning on first connect. If you do not want one, go straight to *The fully
private build* below.

If you do want one, **you must arrange it before the build**. Let's Encrypt cannot reach a box
behind a travel router, and the build's certificate stage runs `certbot --standalone`, which
needs to be reached. It is not a fault you can wait out. The build skips certbot entirely when
it finds `/etc/letsencrypt/live/<your-fqdn>/` already in place, which is the door routes A and
B walk through.

### Three routes, and you only need one

| | **A. Your own laptop** | **B. A token on a machine you keep** | **C. No public certificate** |
|---|---|---|---|
| You need | A laptop and your DNS provider's web page | A machine you control, plus an API token | Nothing |
| Renewal | By hand, about every 60 days | Scripted, wherever the token lives | Not applicable |
| Good for | **A first box.** Nothing to set up | An estate, once there are several to renew | A genuinely closed network |
| What it costs you | Remembering the date | The token can edit your DNS, so mind where it lives | A browser warning your operators click through |

**Route A is the one to take for a first box.** No second machine, no account, no API key.

### Route A: from your own laptop, no token

Install certbot: on macOS with Homebrew, `brew install certbot`; on Ubuntu,
`sudo apt install certbot`.

Then run it with everything under your own home directory, so it needs no administrator rights
at all:

```bash
certbot certonly --manual --preferred-challenges dns \
  --config-dir ~/certbot/config --work-dir ~/certbot/work --logs-dir ~/certbot/logs \
  -d kit1.example.com
```

Certbot stops and prints a record for you to create, something like:

```
_acme-challenge.kit1.example.com   TXT   "gfj9Xq...Rg85nM"
```

Add exactly that as a **TXT** record in your DNS provider's page. **Do not press Enter yet.**
From another terminal, check the record is actually visible, because pressing Enter too early
fails the run and you start over:

```bash
dig _acme-challenge.kit1.example.com TXT +short
```

When that returns your value, press Enter. The certificate lands under
`~/certbot/config/live/kit1.example.com/`. Carry it to the box with `tar` as below, adjusting
the path, then delete the TXT record: it has done its job.

**Write the expiry date on your build record now.** Route A does not renew itself and nothing
will remind you.

### Route B: a token on a machine you keep

Issue it on the machine that holds the token, which is not the box you are building and is
probably not the laptop in front of you. **Name that machine in the command** rather than
pasting a bare `sudo` line into whichever terminal is on top:

```bash
ssh -t you@your-console 'sudo certbot certonly --dns-PROVIDER \
  --dns-PROVIDER-credentials /etc/letsencrypt/dns.ini \
  -d kit1.example.com'
```

Replace `PROVIDER` with certbot's plugin name for your DNS provider, and put that provider's
credentials in the file you name. `-t` because `sudo` needs a terminal. If certbot is not
there yet, `sudo apt install certbot python3-certbot-dns-PROVIDER` on that machine first.

If your provider has no plugin, `certbot certonly --manual --preferred-challenges dns` works
with any of them: it prints a TXT record for you to add by hand, and you repeat that at every
renewal.

Then, **on that same machine**, carry `/etc/letsencrypt/live/<fqdn>/` and `/etc/letsencrypt/archive/<fqdn>/` to the same
paths on the box. **Carry them with `tar`, not `cp`**, so the live-to-archive symlinks survive.
Work under `umask 077`, and leave no intermediate copies behind on whatever machine you staged
through.

### Route C: no public certificate at all

Worth saying plainly, because for some kits it is the honest answer, and because it is easy to
believe you need a public certificate to enrol devices. **You do not.**

The Vantage build gives TAK its own certificate authority either way, and on a box with no
public certificate that CA's certificate is used on **all three connectors**: 8089 for CoT,
8443 for web and admin, and 8446 for device enrolment. **Everything works, including enrolling
handsets by QR.** What you do not get is a *browser-trusted* certificate, so browsers and
devices warn the first time they connect. On a closed network that is expected, not a sign the
build went wrong.

Make the choice deliberately with `--no-letsencrypt` on the build, and skip the A record in
step 2 as well.

**You can remove the warning without Let's Encrypt.** Install your own CA's certificate into
the trust store of the browsers and devices that will use the box. Then the warning goes away
for the people who should see no warning, and stays for everyone else, which is arguably the
better posture for a closed network. It costs you a distribution step per device.

What you give up if you do neither is that every operator meets a security warning and learns
to click through it. That is a habit worth choosing on purpose rather than drifting into.

### The fully private build: nothing published at all

The most likely posture for a deployable kit, and the one the steps above describe in pieces
without ever assembling. **You can enrol devices by QR with nothing whatsoever on public DNS.**

The enrolment QR contains a name, a username and a password:

```
tak://com.atakmap.app/enroll?host=kit1.example.com&username=<user>&token=<password>
```

No certificate travels in it. So exactly two things have to be true on the handset, and neither
of them needs the internet:

**1. The device must resolve the name.** It is on the kit's own wifi, so it asks the router,
and the router answers from the entry you made at step 1.5. That is the thing you put on the
router, and it is the whole mechanism. No A record anywhere.

**2. The device must accept the server's TLS on the enrolment port.** With no public
certificate the server presents its own CA's certificate, and the handset warns once and lets
you accept. Enrolment then completes normally and the device receives its client certificate.
This is expected on a closed network, not a fault.

So the recipe is:

| | |
|---|---|
| Public A record | **None** |
| Public TXT record | **None** |
| Certificate Transparency entry | **None** |
| Certificate | TAK's own CA, built during the install. Use `--no-letsencrypt` |
| Name resolution | The router's dnsmasq entry, step 1.5, plus `/etc/hosts` on the box |
| Device experience | One warning at enrolment, accepted once |

Nothing about the kit reaches the internet, and nobody outside it can learn the box exists.

**The warning is the only cost, and today it is unavoidable on this path.** Removing it means
the handset trusting your CA before it connects, and Vantage does not currently distribute the
CA to devices: enrolment issues a username and a password, and nothing else. Pre-trusting the
CA is therefore a manual step per device rather than something the product does for you. If the
warning is unacceptable in your setting, that pushes you back to a public certificate, and the
lightest form of that is a wildcard with no A record.

**Keep the public path for the boxes that want it.** A demonstration or playground server is
supposed to be reachable, and everything in step 2 applies to it. The two postures coexist in
one estate; decide per box rather than once.

### Using your own DNS server instead of a provider

A reasonable thought at this point is to run DNS on your own hardware rather than hand a token
to a cloud provider. You can, but only for one of the two jobs, and the two look like one.

**An internal resolver does not satisfy a certificate authority.** That is what step 1.5 does
with dnsmasq on the router, and any box on the LAN could do it. It makes names work offline,
which matters. But Let's Encrypt checks the record from *its own* resolvers out on the public
internet, so a record only your network can see is invisible to it, however correctly your
server answers on the LAN.

**An authoritative public nameserver does.** This is the part that is genuinely possible on
your own hardware, and it is a different thing from an internal resolver:

1. At your registrar, delegate a zone to your server with NS records.
2. Your server answers queries for that zone **from the public internet**, on port 53.
3. Certbot writes the challenge record there, and Let's Encrypt reads it from your box.

What it costs you is real. The box needs a stable public address and port 53 reachable from
anywhere, so it is now public infrastructure with a public attack surface. If it is down when a
renewal runs, the renewal fails. And if you delegate your whole zone to it, everything you own
depends on that one box.

**Delegate only the challenge, not the zone.** This is the pattern worth using. Keep your real
zone where it is, and add one CNAME per box:

```
_acme-challenge.kit1.example.com   CNAME   <token>.acme.example.com
```

`acme.example.com` is delegated to a small server of yours whose only job is to serve challenge
TXT records. `acme-dns` is a purpose-built implementation of exactly this. The blast radius is
then bounded: if that server is compromised, an attacker can issue certificates for the names
that point at it and nothing else, and your real DNS is untouched.

**One practical check before you plan this.** A box that lives on a VPN and has no public
address cannot do this job as it stands. Serving public DNS means giving it public ingress,
which is a deliberate change to its posture rather than a configuration tweak.

So the offline answer remains local DNS for the names plus route C for the trust. Your own
authoritative DNS is a third thing, worth it when you want scripted renewals without handing a
token to anyone.

> **Renewal cannot run on a box the internet cannot reach.** Re-issue off-box and re-carry
> before the expiry date. Write that date on the build record and somewhere you will actually
> meet it. An expired certificate in the field is indistinguishable from a dead server.

---

## Step 4 — Ubuntu Server

### 4.1 Which edition, and which version

**Ubuntu Server, not Desktop.** Even for a laptop, and especially for a kiosk: the Vantage
kiosk runs its own compositor on the box's first virtual terminal and displaces the console
there. Ubuntu Desktop already has a display manager holding that seat.

**Take the current LTS that Vantage documents**, which is 22.04 or 24.04. We have also built an
estate box on 26.04 as a deliberate platform trial and it works, but a first build is not the
place to also be testing the platform.

### 4.2 Write the installer media

Three things here have each cost us a build.

- **Verify the ISO checksum before you write it.** A corrupt write presents as
  `/casper/vmlinuz not found` at boot, which reads like a broken stick rather than a broken
  file.
- **On Windows, write with Rufus in DD Image mode.** ISO mode produces media that boots and
  then fails. On macOS or Linux, Raspberry Pi Imager, Balena Etcher or `dd` all write an image
  faithfully.
- **Use a rear motherboard USB port, or a port you trust.** A front-panel port dropped out under
  sustained read on one build, giving casper errors and an installer crash-loop that looked like
  bad media for an hour.

### 4.3 The installer answers, in order

| Screen | Answer | Why |
|---|---|---|
| Language | English | |
| Keyboard | English (UK) | Get this right or every password you type later is wrong in an interesting way |
| Type of install | **Ubuntu Server**, not minimized | Minimized strips tooling the kiosk and any later diagnosis expect to find |
| Network | It should already show your reserved address | If it shows a different one, stop and fix the router. Do not carry on and correct it later |
| Proxy | Blank | |
| Mirror | Default | |
| Storage | **Guided, use an entire disk, with LVM.** Then edit the volume size, see 4.4 | LVM is what makes the snapshot rollback possible |
| Encryption (LUKS) | Off, unless you have decided otherwise | It prompts for a passphrase at every boot, which defeats a kiosk that is meant to power on into the console unattended |
| Confirm destructive write | Yes | |
| Your name | Yours | |
| Server name | The hostname from the build record | |
| Username | The admin user | This is the user Vantage installs as, and the user the estate console will reach |
| Password | Set one | |
| Ubuntu Pro | Skip | |
| SSH | **Tick Install OpenSSH server** | This is the route in for everything after this screen |
| Featured snaps | None | |

Install, reboot, remove the stick.

### 4.4 Storage, the one screen worth slowing down for

**Guided LVM does not allocate the whole disk.** On our 512GB NVMe the installer defaulted
`ubuntu-lv` to 100G of a 473.9G volume group. We set it to **380G by hand, leaving about 94G
free in the volume group**.

Both halves of that matter:

- The default would have wasted three quarters of the disk.
- Taking all of it would leave no room for **LVM snapshots**, which are the rollback point
  before every risky install on these boxes. We have used one in anger to recover a build.

So: on the storage screen, select `ubuntu-lv`, edit it, and size it to leave roughly 20 per cent
of the volume group free.

**If you missed it, it is not worth reinstalling.** This one is fixable on a running box, with
the filesystem mounted and no reboot. Check what you have, then grow it:

```bash
sudo vgs        # VSize is the group, VFree is what is unallocated
sudo lvs        # LSize is what root actually got
```

```bash
sudo lvextend -L 380G /dev/ubuntu-vg/ubuntu-lv && sudo resize2fs /dev/ubuntu-vg/ubuntu-lv
```

`-L` sets the new total size, not the amount to add. Take it from the table above, or leave
about a fifth of `VSize` free. `resize2fs` grows the filesystem into the space `lvextend` just
gave it, online, which is why no reboot is needed. Do it before the box has data on it and it
costs nothing. Snapshots are not backups and they cost write performance while they
exist, so take them before risky work and remove them once the work is accepted.

---

## Step 5 — after Ubuntu, before Vantage

### Clear the installer's SSH host key

The live installer runs its own sshd with its own host key, so anything that accepted it will
see a host-key mismatch on first real login. Clear it **once the installed system is up, not
before**: clearing it early and then polling simply re-adds the installer's key.

```bash
ssh-keygen -R 192.168.50.10
```

**`not found in known_hosts` is the pass, not a failure.** It means nothing needed clearing,
because you never connected to the box over SSH while the installer was running. Only someone
who did will have a key stored, and only they will see one removed. Either answer means you can
carry on.

### Pin the name in the box's own hosts file

So the box's services survive a cold boot without the router.

```bash
echo "192.168.50.10  kit1.example.com kit1" | sudo tee -a /etc/hosts
```

### The private network

Most estates put their servers on a private network, and it is the usual advice for TAK. This
is the one part of the pre-work you can often leave to the console, so the question is **when**,
not just how.

**Vantage joins boxes to networks. It does not run the network.** You bring the account or the
server, and it carries whichever one you chose:

| Provider | What you bring | Worth knowing |
|---|---|---|
| **Tailscale** | A tailnet and a **one-use auth key** | Least to run: the control plane is not yours. The only one of the three that can publish a single path publicly, which matters if a cloud AI connector ever has to reach a private console |
| **WireGuard** | A `wg-quick` config from **your own** WireGuard server | No vendor and no account. What much of the TAK community runs. You run the server, so you carry that job |
| **OpenVPN** | A client `.ovpn` with **inline** certificates | Take it if you already run OpenVPN. If you do not, use WireGuard |

**When to join, in order of preference:**

> **On the estate journey the private network is not a preference, it is a prerequisite**, and
> so is one other thing. Before your existing console can build this box, both of these must
> already be true, and the Deploy wizard cannot tell you either — it lets you fill the whole
> form in and fails at phase 1 of 5:
>
> - **The console can reach the box.** Same network, or both on the same VPN. A kit LAN behind
>   its own router is reachable from nowhere else.
> - **The admin user can elevate.** Enrolment needs **root, or passwordless sudo**. An ordinary
>   sudo account that prompts is refused with *neither root nor passwordless-sudo*. Grant it
>   with a file under `/etc/sudoers.d/`, validate with `visudo -c -f`, and **take it off before
>   the box ships** — see the end of this step.

1. **After the console is installed, from the console.** Its Network action installs the
   provider and joins the box: the join material is sent once, written root-only, never
   logged, gated by the operator password, and audited like every other action. Prefer this,
   because it leaves a record and does not need a shell.
2. **Here, by hand, before Vantage.** Only when the VPN is the *only* way to reach the box at
   all, which means a box somewhere you are not. On a kit you are standing next to, it is not
   needed.
3. **Not at all.** A closed network with no remote access is a legitimate posture, and Vantage
   will never ask you to expose a private box to the internet.

If you do join by hand, on Tailscale:

```bash
sudo tailscale up --accept-dns=false
```

> **The flag is not optional on a kit.** A VPN that pushes its own DNS takes over the box's
> resolver and shadows the answer your router serves for the box's own name, so the box stops
> resolving itself the moment the WAN goes away. That is the entry the whole split-horizon
> design rests on. Vantage's own join passes `--accept-dns=false` for the same reason. Never
> use the bare hostname in a client or service config, either.

**One ordering consequence.** For a private kit, the public A record points at the box's VPN
address, so it cannot be finished until the box has joined. Come back to step 2.2 once it has.

### Stop a laptop suspending itself

An appliance that sleeps is a dead appliance, and on a laptop the lid is the usual cause.

```bash
sudo sed -i 's/^#*HandleLidSwitch=.*/HandleLidSwitch=ignore/;
             s/^#*HandleLidSwitchDocked=.*/HandleLidSwitchDocked=ignore/;
             s/^#*HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' \
  /etc/systemd/logind.conf
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

### Place the certificate

If you pre-issued at step 3, put it in place now, before the build runs.

### Update, and reboot

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

### If you grant temporary passwordless sudo

Some builds are easier with it. If you do, **write down that it must come off before the kit
ships**, and prove it afterwards:

```bash
sudo rm /etc/sudoers.d/90-build-<user>
sudo -n true          # must FAIL
```

A build-time convenience that ships is a build-time convenience you have deployed.

---

## Step 6 — hand the record to the installer

You now have the box the Vantage README assumes. These fields go straight across:

| Build record field | Where Vantage asks for it |
|---|---|
| Box address | `--bind` on `install-vantage.sh` |
| Admin username | The account you install from |
| FQDN | The server's public name, on the build screen |
| Certificate identity | Organisation, unit and country, on the build screen or under the gear |
| Estate name | How the box is named in the console |

Keep the record with the kit. Go on to [the README](README.md) to install the console.

---

## A note on internet access

**The build needs the internet even for a box that will be offline afterwards.** It runs a full
system update and installs packages, and the kiosk fetches its own. Keep the WAN connected for
the whole build, and test the offline behaviour deliberately afterwards rather than by starting
that way.
