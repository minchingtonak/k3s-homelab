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
