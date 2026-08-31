# Symptom to cause

Keyed by **what the operator sees**, because that is what you get told. Every entry here
happened on a real box; none is hypothetical.

Each is a hypothesis to test, not an answer. Confirm the cause before acting on it, and say
so plainly when a fault is not here rather than reaching for the nearest match.

## Services and the server

| What you see | Usually because | Confirm it by |
|---|---|---|
| A service is `active` and users see nothing | It started and does not work. Empty data directory, ports firewalled off, or no data to serve | Check what it produces: count tilesets, pull a stream, complete a handshake |
| TAK input counters climb, no client sees a marker | The input has no filter groups, so its events belong to no group any client certificate is in | Watch for the marker on a client that authenticated normally. Counters prove ingestion only |
| A CoT send reports success, the server never ingested it | 8089 is a streaming endpoint and drops clients that connect, send and vanish within milliseconds | Linger about two seconds after the send. Prove it with a second client on a different certificate receiving the relay |
| An input's port or protocol will not change | The Input Manager's Modify dialog edits filter groups but not port or protocol | Delete and recreate the input |
| Trackers are invisible to a client that should see them | Group membership. The client needs the group on both its certificate OU and the user authentication file | Check both; a group change needs a TAK restart, because the x509 mapping is cached |
| A bot or script crash-loops after a certificate change | It is still dialling the old name. Reissuing under a new name silently breaks everything that used the old one | Enumerate everything that connects by name: bots, scripts, monitoring, data packages already handed out |
| A deleted user still connects | Deleting the account does not revoke the certificate. Certificate clients do not use accounts | Revoke on the CRL, then the tailnet. See the security lesson in SKILL.md |
| CloudTAK loads but the map is blank | The events worker is blocked by ufw from reaching :5000 | Check the firewall between the worker and the port, not the map source |
| Contacts vanish or read stale when offline | Clock drift. TAK stamps CoT with staleness times, so drift presents as "TAK is broken" | chrony needs `local stratum 10`, without which it refuses to serve while itself unsynchronised |

## Maps and data packages

| What you see | Usually because | Confirm it by |
|---|---|---|
| Markers appear, the map stays blank | The tiles are served but ATAK was never told they exist | The client needs a `<customMapSource>` definition. Serving is not the same as clients seeing |
| A package imports with no error and draws nothing | The manifest is in the wrong place for that package type, and the two types have **opposite** rules | WinTAK enrolment: `manifest.xml`, lowercase, at the root. ATAK mission: `MANIFEST/manifest.xml`, capitalised, in a subdirectory |
| Tiles render as garbage or upside down | mbtileserver serves XYZ and applies the TMS row-flip itself, so the map source must not also set TMS | Fetch a tile over HTTP and byte-compare it against the same tile pulled from the file |
| A basemap draws transparently over nothing | GDAL writes `type=overlay` into mbtiles metadata; a basemap wants `baselayer` | Read the metadata table |
| Editing mbtiles metadata appends instead of replacing | There is no unique index on `name`, so `INSERT OR REPLACE` adds a duplicate row | DELETE then INSERT |
| Attaching content to a mission returns 400 and leaves it empty | The hash was passed as a query parameter; it needs a JSON body `{"hashes":[...]}` | Read the response body, not just the status |
| Clients see two identically-named files | A superseded package version is still attached | Detach with `DELETE /Marti/api/missions/<m>/contents?hash=`, which works for webadmin even though deleting from Enterprise Sync does not |

## Mesh and radios

| What you see | Usually because | Confirm it by |
|---|---|---|
| Files or messages are not reaching a handset | The data radio is in bootloader mode, not the code path you are debugging | `udevadm` on the serial device: an `ID_MODEL` ending `-BOOT` is the tell. Always address radios by their by-id path, since ports shuffle on re-cable |
| The gateway rejects every packet: "String field had bad UTF-8" | protobuf-python 7.x validates UTF-8 on parse; Meshtastic unishox2-compresses callsigns into fields typed `string` | The venv-local fix retypes two fields to `bytes`, and **any upgrade of the meshtastic package destroys it**. Pin it and document it where the operator will look |
| A tracker's GPS never fixes after a firmware update | A known GPS-detection regression on that hardware and version, not your pipeline | Search the project's issues for hardware plus version before debugging your own stack. Factory erase and downgrade to the newest pre-regression line |
| Positions are coarse, roughly a 5 km grid | `position_precision` in the channel URL, not a GPS problem | Decode the channel URL and read every field. It also carries `region`, which matters enormously: scanning an EU_868 QR in the USA puts the fleet on licensed cellular spectrum |
| The mesh looks quiet but the gateway is fine | A quiet mesh is not a broken gateway | Check what the gateway has actually forwarded before assuming a fault |

## The console, deploys and the estate

| What you see | Usually because | Confirm it by |
|---|---|---|
| A whole tab vanishes between page loads | Two builds are deploying over each other. A deploy is not a fault, so nothing flags it | Compare the running console version before and after. Parallel work coordinates through the human before every ship |
| A file is "not found" at a path where it demonstrably sits | Two sources of truth about one path. The server computed the destination, the browser form held a stale copy | When the server computes a value, the server is the authority. The browser's copy is display, not truth |
| An enrolment or action fails naming a file that should exist | A hot-deploy carried some files and not others, leaving the action catalogue behind the code | Hot-deploys carry the installer's full file set, or run the installer |
| A box running infra-TAK reports its TAK Server broken | It has no TAK Server; it runs a different stack | Declare `infratak` in `loadout.conf` instead of `takserver`. Undeclared components report skipped, never silent |

## Working the boxes

| What you see | Usually because | Confirm it by |
|---|---|---|
| `sudo` fails with "a terminal is required" | Only one box here has unattended root; the others need the operator's password | `ssh -t`. And never pipe an interactive sudo command: the prompt goes to stdout, the pipe swallows it, and it reads as a hang |
| An SSH session dies in the middle of a command | `pkill -f <name>` matched its own command line and killed the shell that launched it | Separate the kill and the relaunch into different calls |
| SSH starts refusing connections that worked before | fail2ban banned the source after repeated failed auth | Stop. One attempt, then check reachability at the TCP level only. Retrying makes it worse |
| A script does nothing at all on a Mac | `timeout` does not exist there, `/dev/tcp` is bash-only, `mapfile` needs bash 4 and macOS ships 3.2 | Anything running on both a Mac and an Ubuntu box must assume the older, poorer userland |
| A server's writes are slow for no visible reason | Stacked LVM snapshots. Every write to the origin copies the original block into every snapshot first | Count them. Three snapshots is a threefold copy-on-write penalty on a live server |
| An offline install fails, differently each time | pip reaching for the network: an isolated build step, a build backend, a wheel fetch | The box builds nothing. Every wheel prebuilt at cut time on the target architecture, the whole dependency set pinned exactly |

## Things that are simply true and cost time anyway

- TAK passwords need 15+ characters with upper, lower, digit and special, and the specials
  must stay within `-_.!~`, because anything needing percent-encoding breaks the `tak://`
  URL inside a QR code.
- The admin certificate may not be called `admin.p12`. On at least one build it is
  `webadmin.p12`. Search for it rather than assuming the name.
- `sync/delete` returns 403 for `webadmin`. Removing published data packages is an admin-UI
  job even though uploading is scriptable.
- TAK's `uploadSizeLimit` defaults to 400 MB.
- `/tmp` is wiped on reboot. Stage work in the home directory.
- `seq -w 1 5` gives `1 2 3 4 5`, not `01` to `05`. It pads to the width of the largest
  number.
