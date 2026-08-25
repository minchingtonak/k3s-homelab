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

SOAP is on `192.168.20.93:7878` and does **not** echo to the container logs, so it's the safe path
for anything containing a password. Requires gmlevel 3.

```bash
read -rs "NEWPW?new password: "; echo
curl -s -u "<admin>:<admin-password>" -H 'Content-Type: text/xml' \
  -d "<?xml version=\"1.0\" encoding=\"utf-8\"?><SOAP-ENV:Envelope xmlns:SOAP-ENV=\"http://schemas.xmlsoap.org/soap/envelope/\" xmlns:ns1=\"urn:AC\"><SOAP-ENV:Body><ns1:executeCommand><command>account set password <name> $NEWPW $NEWPW</command></ns1:executeCommand></SOAP-ENV:Body></SOAP-ENV:Envelope>" \
  http://192.168.20.93:7878/ | grep -o '<result>.*</result>'
unset NEWPW
```

The password is interpolated raw into an XML body, so `&`, `<` and `>` will be mangled — stick to
alphanumerics, or set the SRP6 salt/verifier directly in `acore_auth.account` instead.

## Realm settings

- The client's `realmlist.wtf` must point at the **authserver** (`192.168.20.92`), not the
  worldserver.
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
Data/<locale>/realmlist.wtf     ->  set realmlist 192.168.20.92
WTF/Config.wtf                  ->  SET realmList "192.168.20.92"
```

`<locale>` is the client's locale directory, e.g. `Data/enUS/`. Many installs read the locale copy and
ignore a `realmlist.wtf` at the install root, so edit the locale one — or both.

The address here is the **authserver** (`192.168.20.92:3724`). The worldserver (`192.168.20.93:8085`)
is never configured client-side: the authserver hands it to the client after login, from
`acore_auth.realmlist`. If login succeeds but the client hangs on "Logging in to game server", that
row is wrong — not the client.

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
