# wow (AzerothCore 3.3.5a)

Flux reconciles everything in this directory. What follows is the one-time bootstrap that can't be
expressed in a manifest, plus the gotchas that cost the most time the first time around.

## First boot

The worldserver runs four init containers and takes 15+ minutes on a fresh volume — `client-data`
alone downloads ~2.7GB of maps/vmaps/mmaps. Subsequent boots skip the download and the migrations
are no-ops. Watch it with:

```bash
kubectl -n wow logs deploy/wow-worldserver -c client-data -f
```

## Create the first admin account

SOAP authenticates as an existing GM account, so it can't bootstrap itself — the first account has to
come from the worldserver console. The console reads newline-terminated commands from stdin;
`tty` is deliberately off (see the comment in `worldserver.yaml`), so pipe commands in rather than
attaching interactively:

```bash
timeout 15 kubectl -n wow attach -i deploy/wow-worldserver <<'EOF'
account create <name> <throwaway-password>
EOF
```

Then, in a **separate** invocation once the account has committed:

```bash
timeout 15 kubectl -n wow attach -i deploy/wow-worldserver <<'EOF'
account set addon <name> 2
account set gmlevel <name> 3 -1
EOF
```

Console output isn't returned over the attach stream — read it with
`kubectl -n wow logs deploy/wow-worldserver --tail=20`.

Non-obvious bits:

- **`account create` is asynchronous.** Follow-up commands in the same batch fail with
  `Account not exist: <NAME>` because the DB write hasn't landed yet. Split them into two calls.
- **The maximum assignable gmlevel is 3** (`SEC_ADMINISTRATOR`). `4` is `SEC_CONSOLE` and is rejected
  with `You have low security level for this.` — the handler requires the target level be strictly
  below the caller's.
- **`addon 2`** = WotLK content. Without it the account is capped at vanilla.
- **The console echoes every command to the pod's stdout**, so any password typed here lands in the
  container logs and gets shipped to Loki. Use a throwaway password and rotate it over SOAP.

## Rotate a password (SOAP)

Prefer `scripts/wow-accounts.py passwd <name>`. It computes the SRP6 salt/verifier locally and writes
them straight to `acore_auth.account`, so the plaintext never leaves your machine.

SOAP remains a second path and also does **not** echo to the container logs. It is no longer exposed
on the LAN (the services are ClusterIP now), so reach it over a port-forward. Requires gmlevel 3.

```bash
kubectl -n wow port-forward svc/wow-worldserver 7878:7878 &
read -rs "NEWPW?new password: "; echo
curl -s -u "<admin>:<admin-password>" -H 'Content-Type: text/xml' \
  -d "<?xml version=\"1.0\" encoding=\"utf-8\"?><SOAP-ENV:Envelope xmlns:SOAP-ENV=\"http://schemas.xmlsoap.org/soap/envelope/\" xmlns:ns1=\"urn:AC\"><SOAP-ENV:Body><ns1:executeCommand><command>account set password <name> $NEWPW $NEWPW</command></ns1:executeCommand></SOAP-ENV:Body></SOAP-ENV:Envelope>" \
  http://127.0.0.1:7878/ | grep -o '<result>.*</result>'
unset NEWPW
```

## Account management over SOAP

`scripts/wow-accounts.py` drives `create`, `delete`, `gmlevel` and `addon` through
the worldserver's SOAP endpoint, not the stdin console: it holds a `kubectl
port-forward` to the ClusterIP service for the duration of each call and posts
the `executeCommand` envelope with urllib (no extra dependencies). This is why
those subcommands need an existing gmlevel 3 account — credentials come from
`WOW_SOAP_USER` / `WOW_SOAP_PASS`, or an interactive prompt when unset.

Password-bearing flows are unchanged: `account create` still uses a disposable
password that is immediately overwritten with a locally-computed SRP6
salt/verifier written straight to the database, so the plaintext never leaves
the script — the SOAP request body would otherwise pass through the
port-forward and the worldserver process.

The one thing that still needs the stdin console is the **first-account
bootstrap** above, on a fresh realm where no GM account exists yet for SOAP to
authenticate as. That is why `stdin: true` stays on the worldserver for now;
dropping it (e.g. if the console is ever implicated in another stdout-pipe
wedge, as suspected on 2026-09-06) requires replacing that one-time flow with
a direct-DB insert first.

The password is interpolated raw into an XML body, so `&`, `<` and `>` will be mangled — stick to
alphanumerics, or use the script above.

## Realm settings

- The client's `realmlist.wtf` must point at the **authserver** (`wow.minch.zone`), not the
  worldserver. Both services are ClusterIP — the tunnel is the only route in, so there is no LAN
  address to use instead.
- The realm's own name and client-facing address live in `acore_auth.realmlist`, maintained by the
  `realmlist` init container. Change them there so the value survives a pod restart — editing the DB
  by hand gets overwritten on the next boot.

## Pointing a fresh 3.3.5a client at the realm

**1. Use a clean 3.3.5a client, build 12340.** The build must match `realmlist.gamebuild` (`12340`) or
the client is rejected at the version check with a different error than a bad login. Prefer a plain
Blizzard 3.3.5a client over a server repack — repacks ship their own MPQs in `Data/` that have to be
stripped. Confirm the build in-game on the login screen, bottom right.

The [ChromieCraft download](https://chromiecraft.com/en/downloads/) is a reasonable source and was
verified against [anzz1/wow-client-checksums](https://github.com/anzz1/wow-client-checksums), an archive
of original unmodified client hashes:

- `Wow.exe` is byte-identical to the Blizzard original (`45892bdedd0ad70aed4ccd22d9fb5984`)
- 16 of 19 MPQs match exactly, including every large content archive
- no custom archives — no `patch-4.MPQ`, no `patch-A.MPQ`–`patch-Z.MPQ`, which is where repacks inject
- `backup-enUS.MPQ` differs because ChromieCraft baked their own `realmlist.wtf` into it. That is the
  Repair tool's backup archive, so **running Repair restores their realmlist, not yours** — the one
  real gotcha with this client
- `patch-2.MPQ` and `patch-enUS-2.MPQ` differ but contain only stock Blizzard paths (verified by
  listing them, including all 139 DBCs), so they appear repacked rather than modified

To check any client, download the matching list and verify from the client root:

```bash
curl -sO https://raw.githubusercontent.com/anzz1/wow-client-checksums/master/3.3.5.12340-enUS.md5
grep -E '\*(Data/.*\.MPQ|Wow\.exe)$' 3.3.5.12340-enUS.md5 | md5sum -c -
```

**2. Close the client before editing any config.** WoW rewrites `WTF/Config.wtf` on clean exit and
strips comments while doing so, so edits made while it's running are silently lost.

**3. Point it at the authserver.** Two separate files carry the address and both take effect:

```
Data/<locale>/realmlist.wtf     ->  set realmlist wow.minch.zone
WTF/Config.wtf                  ->  SET realmList "wow.minch.zone"
```

`<locale>` is the client's locale directory, e.g. `Data/enUS/`. Many installs read the locale copy and
ignore a `realmlist.wtf` at the install root, so edit the locale one — or both.

The address here is the **authserver** (`wow.minch.zone:3724`). The worldserver (`:8085`) is never
configured client-side: the authserver hands it to the client after login, from
`acore_auth.realmlist`. If login succeeds but the client hangs on "Logging in to game server", that
row is wrong — not the client.

This is the same address for LAN and internet players. `wow.minch.zone` resolves to the public
Pangolin edge with no split horizon, and the realmlist `localAddress`/`localSubnetMask` split that
would have routed local play direct to MetalLB is deliberately collapsed to a `/32` — see the note on
the `realmlist` init container in `worldserver.yaml`.

**4. Log in and verify server-side.** A successful login is the only thing that sets `last_login`:

```bash
kubectl -n wow exec deploy/wow-database -- sh -c \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e \
   "select username,last_login,last_ip from acore_auth.account;"'
```

Still `NULL` after an attempt means no login has ever succeeded for that account.

Optional: `SET realmName` must match the realm's name exactly for the client to auto-select it.
Mismatched or absent just drops you on the realm-selection screen, which is harmless.

## Debugging a rejected login

The client message "The information you have entered is not valid" maps to `WOW_FAIL_UNKNOWN_ACCOUNT`
(`0x04`), which AzerothCore returns for **both** a missing account and a wrong password —
deliberately, to prevent account enumeration. `WOW_FAIL_INCORRECT_PASSWORD` is never sent. So the
client error alone tells you nothing about which of the two it is.

Things that mislead:

- `failed_logins` and `last_attempt_ip` in `acore_auth.account` are only written when
  `WrongPass.MaxCount > 0`. It defaults to `0`, so both columns stay at their defaults no matter how
  many logins fail. They are not evidence of anything.
- `last_login` is only set on a **successful** login.
- Account names and passwords are uppercased before SRP6 hashing, so both are case-insensitive. Max
  16 characters, printable ASCII — that's also the client's field limit.

Confirm what the server actually has:

```bash
kubectl -n wow exec deploy/wow-database -- sh -c \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e \
   "select id,username,expansion,locked,last_login from acore_auth.account;
    select * from acore_auth.account_access;"'
```

## Player positions on the dashboard

The wow-realm dashboard has a `Player positions` geomap panel plotting online
characters on continent maps, plus a `Players by zone` table. Two things make
it work; both live in this directory:

- **Projection**: world yards → fake lon/lat is computed in the exporter
  (`wow_character_position_lon/lat`) from one math shared with the map tiles
  in `assets/wow-maps/` — see that README for the derivation and ground truth.
  The dashboard joins the lon/lat metric pair on `character_name` and filters
  by the `continent` dashboard variable, which also switches the basemap tiles.
- **Freshness**: an online character's row in `characters` is only written at
  `PlayerSaveInterval` (stock 15 min), so the map would be 15 minutes stale.
  `AC_PLAYER_SAVE_INTERVAL=30000` on the worldserver makes dots move about
  every 30s, matching the exporter's scrape interval so each scrape sees a new
  position instead of repeating every other one. Takes effect on worldserver
  restart. The two timers are independent, so phase drift can still drop the
  occasional point; scraping faster than the save interval is what would make
  capture exact.

The tiles are served from this repo over raw.githubusercontent.com; the panel
URL is versioned with `main`, so tile changes ride ordinary PRs.

## Chat and server logs on the dashboard

The admin dashboard (`wow-realm` — not the public one) has two Loki panels at
the bottom. Both read logs Alloy already ships from every pod, so no new
collection was needed: the console streams land in Loki with `cluster`,
`namespace` and `container` labels, and the panels just select on them.

**Chat** shows player chat. AzerothCore logs chat through two cooperating
pieces, and neither alone is enough:

- `AC_CHAT_LOG_ENABLE=1` on the worldserver enables `ChatLogScript`
  (`src/server/scripts/World/chat_log.cpp`), which emits `LOG_INFO` to
  loggers `chat.say` / `chat.emote` / `chat.yell` / `chat.whisper` /
  `chat.party` / `chat.raid` / `chat.bg` / `chat.guild` /
  `chat.guild.officer` / `chat.channel` (system channels: Trade, General,
  City, LFG) and `chat.channel.<name>` for custom channels.
- Those `Logger.*` lines ship **commented out** in `worldserver.conf`, and an
  unconfigured logger inherits `Logger.root` (level 2 = Error), which drops
  Info. Env vars cannot fix this: AC's env override only re-values keys
  already present in the conf file, and the logging system _enumerates_ file
  keys to discover appenders and loggers. So the `chatlog-config` init
  container appends the wiring to `worldserver.conf` on every boot,
  idempotently — the same pattern as the `realmlist` patch.

`Appender.Chat=1,6,4` is a second console appender with no colour argument —
`AppenderConsole::InitColors` only runs when a 4th token exists, so chat
lines reach Loki without ANSI escapes — and flags=4, which prefixes each
line with its logger name: `[chat.say] Player Foo says (language 1): hi`.
The dashboard's Chat panel selects lines containing `[chat.` and the Server
logs panel excludes them. `Logger.chat.addon=0,Chat` keeps addon comms out:
those are WeakAuras/BigWigs sync blobs, not readable chat — raise that
logger's level in the init container's block to include them.

Chat is also durable on the volume, not just in the console stream:
`Logger.chat=4,Chat ChatFile` routes it to `Appender.ChatFile` — a file
appender in append mode with timestamp + logger-name prefixes — and
`AC_LOGS_DIR=/azerothcore/env/dist/logs` points every file appender at the
PVC mount (the stock empty `LogsDir` resolves against the process cwd,
which is container-local and dies with the pod; `Server.log` used to live
there). So `/azerothcore/env/dist/logs/Chat.log` accumulates chat across
restarts and survives a wedged stdout pipe: on 2026-09-06 the worldserver's
containerd shim stopped draining its stdout pipe mid-day while the server
kept serving — chat emitted into the dead pipe was lost because the
console appender was then the only sink. The init container rewrites its
conf block on every boot (delete + re-append), so changes to the appender
wiring converge without touching the volume by hand.

One more console-path gotcha, fixed the hard way: AC's console appender
writes with `vfprintf` and never flushes, and a piped stdout is
block-buffered at 4KiB — so console output (chat included) used to sit
inside the worldserver for 25–50 minutes before reaching the kubelet log
and Loki. The container `command:` wraps the entrypoint with
`stdbuf -oL -eL`, whose `LD_PRELOAD` survives the exec chain into the
worldserver binary and line-buffers stdout, so lines ship within seconds.
File appenders are unaffected — `Chat.log` always landed immediately, which
is how the buffering was diagnosed.

**Server logs** shows the worldserver and authserver console streams (minus
chat lines and the AHBot `Begin Performing Update Cycle` heartbeat, which
fires continuously and drowns everything else). Authserver login failures
(`account X tried to login with invalid password!`) land here.

Both panel queries are plain label selectors plus substring filters — the
trailing-newline problem that once needed a `regexp` + `line_format` trim
here is fixed at ingestion instead. Alloy's `loki.source.kubernetes` ships
the line terminator as line _content_ (measured: every stored entry
cluster-wide ends with a literal `\n`), so a `loki.process` `replace` stage
in the Alloy HelmRelease strips the trailing CR/LF run before entries are
pushed to Loki (same PR). Entries stored before that fix keep their
trailing `\n` — Loki here has no retention — so within the dashboard's
default 24h window they render a phantom blank line underneath until they
age out of view.

Things worth knowing:

- Chat now lives in Loki, like everything else the consoles print —
  including GM passwords typed over `kubectl attach` (see the warning at the
  top of this file). The public dashboard shows none of it.
- There is no GM-command audit to add: the `commands.gm` logger exists in
  AC's logging framework but has no callers in master — dead code.
- Loki here is a 10Gi filesystem-backed single binary with no retention
  policy configured, so chat and server logs are kept until it fills.

## Long-term metrics retention

The dashboards read the `wow-longterm` datasource, not `prometheus`. It is a
second Prometheus instance (`prometheus-wow-longterm`, monitoring namespace,
defined in `k8s/infrastructure/kube-prometheus-stack-config/`) whose only job
is to keep realm history indefinitely — the main instance is capacity-bound
long before its configured 90d, so anything only it holds ages out after a
couple of months.

At the cutover the long-term store starts empty: dashboards show no history
until the new instance accumulates it, and pre-cutover data remains queryable
on the `prometheus` datasource until it ages out there. Launch-week history
(Aug 29 – Sep 4 2026) was imported once by hand as TSDB blocks — exported from
the main instance, built with `promtool tsdb create-blocks-from`, copied into
the pod — so the dashboards have data from day one. The wow.money recording
rules are not backfilled: each instance accumulates its own earned total from
its own start, and raw series (balances, XP, positions) have no such reset.
