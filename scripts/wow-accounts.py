#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""CRUD for AzerothCore accounts on the `wow` namespace.

Passwords never travel through the worldserver console. That console echoes every
command it receives to the pod's stdout, so `account create <name> <password>`
puts the password in the container log and ships it to Loki. Anything involving a
password is therefore written straight to `acore_auth.account` as an SRP6
salt/verifier pair computed locally; the plaintext never leaves this process.

Everything else — create, delete, gmlevel, addon — goes through the console so
AzerothCore's own logic applies (notably `account delete`, which cascades to the
account's characters; deleting the row by hand would orphan them).

Usage:
    scripts/wow-accounts.py list
    scripts/wow-accounts.py online
    scripts/wow-accounts.py stats
    scripts/wow-accounts.py create <name> [--addon 2] [--gmlevel N]
    scripts/wow-accounts.py passwd <name>
    scripts/wow-accounts.py delete <name>
    scripts/wow-accounts.py gmlevel <name> <0-3>
    scripts/wow-accounts.py addon <name> <0-2>
    scripts/wow-accounts.py verify <name>

Passwords are prompted for without echo, or read from stdin with --stdin.
"""
import argparse
import getpass
import hashlib
import re
import secrets
import subprocess
import sys
import time

NAMESPACE = "wow"
DB_DEPLOY = "deploy/wow-database"
WORLD_DEPLOY = "deploy/wow-worldserver"

# WoW SRP6: N is the well-known 256-bit safe prime, g = 7.
SRP6_N = 0x894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7
SRP6_G = 7

# AzerothCore's MAX_ACCOUNT_STR, and also the 3.3.5a client's field limit.
MAX_LEN = 16
# SEC_ADMINISTRATOR. 4 is SEC_CONSOLE and the console refuses to assign it.
MAX_GMLEVEL = 3
# Account names are uppercased before hashing, so they are case-insensitive.
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class Failure(Exception):
    """Anything the user should see as a clean error rather than a traceback."""


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise Failure(f"{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def mysql(sql, batch=True):
    """Run SQL in the database pod. Returns rows as lists of strings when batch."""
    flags = "-N -B" if batch else "--table"
    out = run([
        "kubectl", "-n", NAMESPACE, "exec", DB_DEPLOY, "--",
        "sh", "-c", f'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" {flags} -e "{sql}"',
    ])
    if not batch:
        return out
    return [line.split("\t") for line in out.splitlines() if line.strip()]


def console(*commands, timeout=20):
    """Feed commands to the worldserver's stdin console.

    Never pass a password here — see the module docstring.
    """
    payload = "".join(f"{c}\n" for c in commands)
    subprocess.run(
        ["timeout", str(timeout), "kubectl", "-n", NAMESPACE, "attach", "-i", WORLD_DEPLOY],
        input=payload, capture_output=True, text=True,
    )


def account_row(name):
    rows = mysql(
        "select id,username,expansion,locked from acore_auth.account "
        f"where username='{name.upper()}'"
    )
    return rows[0] if rows else None


def wait_for(name, should_exist, seconds=30):
    """`account create` and `account delete` are queued and applied asynchronously."""
    for _ in range(seconds):
        if bool(account_row(name)) is should_exist:
            return True
        time.sleep(1)
    return False


def verifier_for(name, password, salt):
    h1 = hashlib.sha1(f"{name.upper()}:{password.upper()}".encode()).digest()
    x = int.from_bytes(hashlib.sha1(salt + h1).digest(), "little")
    return pow(SRP6_G, x, SRP6_N).to_bytes(32, "little")


def set_password(name, password):
    """Write a fresh salt/verifier pair straight to the DB, bypassing the console."""
    salt = secrets.token_bytes(32)
    v = verifier_for(name, password, salt)
    mysql(
        f"update acore_auth.account set salt=UNHEX('{salt.hex()}'), "
        f"verifier=UNHEX('{v.hex()}'), session_key=NULL where username='{name.upper()}'"
    )
    stored_salt, stored_verifier = mysql(
        f"select hex(salt),hex(verifier) from acore_auth.account where username='{name.upper()}'"
    )[0]
    recomputed = verifier_for(name, password, bytes.fromhex(stored_salt)).hex().upper()
    if recomputed != stored_verifier.upper():
        raise Failure("password write did not verify — account left in an unknown state")


def read_password(args, prompt="password: "):
    if args.stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass(prompt)
        if getpass.getpass("confirm: ") != password:
            raise Failure("passwords do not match")
    if not 1 <= len(password) <= MAX_LEN:
        raise Failure(f"password must be 1-{MAX_LEN} characters (client field limit)")
    if not password.isascii() or not password.isprintable():
        raise Failure("password must be printable ASCII")
    return password


def check_name(name):
    if not NAME_RE.match(name):
        raise Failure(f"invalid account name {name!r}: use 1-{MAX_LEN} of [A-Za-z0-9_-]")
    return name


def require(name, exists=True):
    row = account_row(name)
    if exists and not row:
        raise Failure(f"no such account: {name}")
    if not exists and row:
        raise Failure(f"account already exists: {name}")
    return row


def cmd_list(args):
    print(mysql(
        "select a.id,a.username,a.expansion,IFNULL(aa.gmlevel,0) as gmlevel,a.locked,"
        "a.last_login,a.last_ip from acore_auth.account a "
        "left join acore_auth.account_access aa on aa.id=a.id order by a.id",
        batch=False,
    ), end="")


# 3.3.5a race/class ids. Hardcoded because the names live in the client's DBC
# files, not the database — there is nothing to join against. Zone ids are left
# raw for the same reason.
RACES = {
    1: "Human", 2: "Orc", 3: "Dwarf", 4: "NightElf", 5: "Undead", 6: "Tauren",
    7: "Gnome", 8: "Troll", 10: "BloodElf", 11: "Draenei",
}
CLASSES = {
    1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
    6: "DeathKnight", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid",
}


def cmd_online(args):
    """Who is on the realm right now.

    `characters.online` is authoritative: the worldserver clears it on logout and
    on a clean shutdown. It can go stale if the pod is SIGKILLed mid-session,
    which is why `stats` cross-checks it against acore_auth.account.online.
    """
    rows = mysql(
        "select c.name,c.level,c.race,c.class,c.zone,c.map,"
        "a.username,IFNULL(aa.gmlevel,0),a.last_ip,a.last_login,"
        "SEC_TO_TIME(c.totaltime) "
        "from acore_characters.characters c "
        "join acore_auth.account a on a.id=c.account "
        "left join acore_auth.account_access aa on aa.id=a.id "
        "where c.online=1 order by c.name"
    )
    if not rows:
        print("nobody online")
        return
    header = f"{'CHARACTER':<14}{'LVL':>4}  {'RACE':<10}{'CLASS':<12}{'ZONE':>6}{'MAP':>5}  " \
             f"{'ACCOUNT':<14}{'GM':>3}  {'IP':<16}{'SINCE':<20}{'PLAYED':>10}"
    print(header)
    print("-" * len(header))
    for name, lvl, race, cls, zone, map_, acct, gm, ip, since, played in rows:
        print(
            f"{name:<14}{lvl:>4}  {RACES.get(int(race), race):<10}"
            f"{CLASSES.get(int(cls), cls):<12}{zone:>6}{map_:>5}  "
            f"{acct:<14}{gm:>3}  {ip:<16}{since:<20}{played:>10}"
        )
    print(f"\n{len(rows)} online")


def cmd_stats(args):
    """Realm totals. Cheap enough to poll, but see `online` for the live list."""
    def scalar(sql):
        rows = mysql(sql)
        return rows[0][0] if rows else "0"

    chars_online = scalar("select count(*) from acore_characters.characters where online=1")
    accts_online = scalar("select count(*) from acore_auth.account where online=1")

    print(f"{'accounts':<26}{scalar('select count(*) from acore_auth.account'):>10}")
    print(f"{'characters':<26}{scalar('select count(*) from acore_characters.characters'):>10}")
    print(f"{'characters online':<26}{chars_online:>10}")
    print(f"{'accounts online':<26}{accts_online:>10}")
    # A mismatch means a session ended without the worldserver clearing the flag
    # — usually a SIGKILL rather than a graceful shutdown. Harmless, but it makes
    # any "players online" graph read high until those characters log in again.
    if chars_online != accts_online:
        print("  ^ mismatch: stale online flag from an unclean shutdown")
    print(f"{'logged in last 24h':<26}"
          f"{scalar('select count(*) from acore_auth.account where last_login > NOW() - INTERVAL 1 DAY'):>10}")
    print(f"{'logged in last 7d':<26}"
          f"{scalar('select count(*) from acore_auth.account where last_login > NOW() - INTERVAL 7 DAY'):>10}")
    print(f"{'never logged in':<26}"
          f"{scalar('select count(*) from acore_auth.account where last_login is null'):>10}")
    print(f"{'total playtime':<26}"
          f"{scalar('select IFNULL(SEC_TO_TIME(sum(totaltime)),0) from acore_characters.characters'):>10}")

    top = mysql(
        "select c.name,c.level,SEC_TO_TIME(c.totaltime) from acore_characters.characters c "
        "order by c.totaltime desc limit 5"
    )
    if top:
        print("\nmost played:")
        for name, lvl, played in top:
            print(f"  {name:<14}{lvl:>4}  {played}")


def cmd_create(args):
    check_name(args.name)
    require(args.name, exists=False)
    password = read_password(args)

    # The console needs *a* password to create the account; use a disposable one
    # and overwrite it below, so only the throwaway is ever written to the log.
    console(f"account create {args.name} {secrets.token_hex(8)}")
    if not wait_for(args.name, should_exist=True):
        raise Failure("account did not appear — check `kubectl -n wow logs deploy/wow-worldserver`")

    console(f"account set addon {args.name} {args.addon}")
    if args.gmlevel:
        console(f"account set gmlevel {args.name} {args.gmlevel} -1")
    set_password(args.name, password)
    print(f"created {args.name} (addon {args.addon}, gmlevel {args.gmlevel or 0})")


def cmd_passwd(args):
    check_name(args.name)
    require(args.name)
    set_password(args.name, read_password(args, "new password: "))
    print(f"password updated for {args.name}")


def cmd_delete(args):
    check_name(args.name)
    require(args.name)
    if not args.yes:
        confirm = input(f"delete {args.name} and all its characters? type the name to confirm: ")
        if confirm.strip().upper() != args.name.upper():
            raise Failure("aborted")
    console(f"account delete {args.name}")
    if not wait_for(args.name, should_exist=False):
        raise Failure("account still present — check the worldserver log")
    print(f"deleted {args.name}")


def cmd_gmlevel(args):
    check_name(args.name)
    require(args.name)
    if not 0 <= args.level <= MAX_GMLEVEL:
        raise Failure(f"gmlevel must be 0-{MAX_GMLEVEL} ({MAX_GMLEVEL} is SEC_ADMINISTRATOR)")
    console(f"account set gmlevel {args.name} {args.level} -1")
    print(f"{args.name} gmlevel -> {args.level}")


def cmd_addon(args):
    check_name(args.name)
    require(args.name)
    if not 0 <= args.expansion <= 2:
        raise Failure("addon must be 0 (vanilla), 1 (TBC) or 2 (WotLK)")
    console(f"account set addon {args.name} {args.expansion}")
    print(f"{args.name} addon -> {args.expansion}")


def cmd_verify(args):
    check_name(args.name)
    require(args.name)
    password = getpass.getpass("password: ") if not args.stdin else sys.stdin.readline().rstrip("\n")
    salt, stored = mysql(
        f"select hex(salt),hex(verifier) from acore_auth.account where username='{args.name.upper()}'"
    )[0]
    match = verifier_for(args.name, password, bytes.fromhex(salt)).hex().upper() == stored.upper()
    print("MATCH" if match else "NO MATCH")
    return 0 if match else 1


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stdin", action="store_true", help="read the password from stdin")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list all accounts").set_defaults(func=cmd_list)
    sub.add_parser("online", help="show characters currently online").set_defaults(func=cmd_online)
    sub.add_parser("stats", help="realm totals and activity").set_defaults(func=cmd_stats)

    create = sub.add_parser("create", help="create an account")
    create.add_argument("name")
    create.add_argument("--addon", type=int, default=2, help="0 vanilla, 1 TBC, 2 WotLK (default)")
    create.add_argument("--gmlevel", type=int, default=0)
    create.set_defaults(func=cmd_create)

    passwd = sub.add_parser("passwd", help="change an account password")
    passwd.add_argument("name")
    passwd.set_defaults(func=cmd_passwd)

    delete = sub.add_parser("delete", help="delete an account and its characters")
    delete.add_argument("name")
    delete.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    delete.set_defaults(func=cmd_delete)

    gmlevel = sub.add_parser("gmlevel", help="set the GM level")
    gmlevel.add_argument("name")
    gmlevel.add_argument("level", type=int)
    gmlevel.set_defaults(func=cmd_gmlevel)

    addon = sub.add_parser("addon", help="set the expansion level")
    addon.add_argument("name")
    addon.add_argument("expansion", type=int)
    addon.set_defaults(func=cmd_addon)

    verify = sub.add_parser("verify", help="check a password against the stored verifier")
    verify.add_argument("name")
    verify.set_defaults(func=cmd_verify)

    return parser


def main():
    args = build_parser().parse_args()
    try:
        return args.func(args) or 0
    except Failure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
