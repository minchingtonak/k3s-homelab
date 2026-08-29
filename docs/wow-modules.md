# WoW realm — building the modded AzerothCore image

Runbook for the custom `worldserver` / `db-import` images that carry **mod-transmog**, **mod-autobalance** and
**mod-aoe-loot**. The reasoning behind picking them, and the survey of everything that was not picked, is in
[wow-server-modules.md](wow-server-modules.md).

Adding another module is one entry in the clone loop in `docker/azerothcore/Dockerfile` plus a `--build-arg`
line in the build script. Nothing else in the pipeline is per-module.

## Why there is an image build at all

AzerothCore modules are C++ statically linked into the core. There is no runtime load path, no plugin
directory, no environment variable that turns one on. The upstream wiki is one sentence about it: "In order for
your module to work you need to recompile the source." So adding a module means owning a build.

Two images come out of that build, not one:

- **worldserver** — the module code itself.
- **db-import** — mod-transmog and mod-aoe-loot ship SQL under `data/sql/`, and the update fetcher looks for it
  at `<source>/modules/<module>/data/sql/` (`UpdateFetcher.cpp`). Upstream's db-import image has an empty
  `modules/` directory, so pairing it with a modded worldserver would leave their schema silently unapplied.
  Building both from one tree also keeps the two binaries agreeing on the database version.

`authserver` and `client-data` stay on the upstream `acore/*` images. Neither loads modules, and client-data is
just a downloader for pre-extracted maps.

mod-autobalance has no SQL at all — it is pure C++ and config.

## Build and push

```bash
docker login forgejo.item.fyi     # once, or whenever the token expires
make wow-image
```

That runs `scripts/build-azerothcore-images.sh`, which builds both targets from
`docker/azerothcore/Dockerfile` and pushes them to `forgejo.item.fyi/akmin/`. It prints the resulting
`repo:tag@sha256:...` lines to paste into the manifests.

A cold build takes about 7 minutes on the 16-core workstation — roughly 4 of that is the compile itself, the
rest apt and the clone. ccache is kept in a BuildKit cache mount, so a rebuild that only bumps a module ref is
quicker again. The images come out at ~550MB (worldserver) and ~1.1GB (db-import, which carries the SQL trees).

**Do not build on a cluster node.** Ninja at `-j16` against a full AzerothCore tree will not fit alongside the
realm on a 15Gi box.

Useful overrides, all read from the environment (make passes command-line variables through):

```bash
make wow-image WOW_IMAGE_PUSH=0                       # build only, no publish
make wow-image WOW_IMAGE_TAG=test                     # override the dated tag
make wow-image WOW_AC_REF=<sha>                       # pin the core to a commit
make wow-image WOW_MOD_TRANSMOG_REF=<sha>             # pin a module to a commit
```

The per-module refs are `WOW_MOD_TRANSMOG_REF`, `WOW_MOD_AUTOBALANCE_REF` and `WOW_MOD_AOE_LOOT_REF`, all
defaulting to `master`.

The Dockerfile clones its own sources rather than taking a checkout as build context, so the context is one
file and every input is pinned in one place.

## Deploying

1. Build and push; note the two digests the script prints.
2. Edit `k8s/apps/wow/worldserver.yaml`: the `db-import` init container and the `worldserver` container. Pin
   `repo:tag@sha256:...` the same way the upstream images are pinned.
3. Commit, push, let Flux reconcile. Never `kubectl apply` it.

The pod does a `Recreate` rollout. db-import applies the module SQL on the way up; first boot after the swap
takes longer than usual because of it.

No `imagePullSecret` is needed. Forgejo answers an unauthenticated `/v2/` request with a `401` and a Bearer
challenge, which looks like a private package but is just the standard registry token flow — fetch an anonymous
token from `/v2/token` and the manifest returns `200`. containerd does this on its own. The nodes resolve
`forgejo.item.fyi` to the internal Traefik address (`192.168.20.90`) via split-horizon DNS, so the pull never
leaves the LAN.

Should that ever change to a genuine `401` after the token exchange, the fix is an `imagePullSecret` in the
`wow` namespace: a SOPS-encrypted `dockerconfigjson`, wired like any other secret here.

## Wiping the realm

**This destroys every character, account and item on the realm. There is no undo.** Only do it deliberately —
for example to start clean before real players arrive.

Worth knowing: a wipe is also the better-tested way to install a new module. A fresh import is the path
verified end to end (empty schemas, db-import from scratch, worldserver booting with every module config
loaded); the incremental path has only been checked statically against the applied-updates table.

Drop the schemas — do **not** delete the `wow-database` PVC. The db-import image sets `AC_FORCE_CREATE_DB=1` and
recreates all three databases from nothing, whereas deleting the volume also destroys the MySQL root user that
the `wow` secret's password belongs to, which just makes extra work.

```bash
# 1. drop the three databases
kubectl -n wow exec deploy/wow-database -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "
  DROP DATABASE IF EXISTS acore_world;
  DROP DATABASE IF EXISTS acore_characters;
  DROP DATABASE IF EXISTS acore_auth;"'

# 2. db-import only runs as an init container, so the pod has to restart for it
#    to repopulate. dropping the schemas alone leaves a worldserver talking to
#    databases that no longer exist.
kubectl -n wow delete pod -l app.kubernetes.io/name=wow-worldserver

# 3. the authserver holds connections to acore_auth and will not recover on its own
kubectl -n wow delete pod -l app.kubernetes.io/name=wow-authserver

# 4. watch the import — roughly 3000 files, several minutes
kubectl -n wow logs -f deploy/wow-worldserver -c db-import
```

What survives, and what does not:

- **The `client-data` volume is untouched**, so there is no 2.7GB re-download. Only the database is rebuilt.
- **The realmlist row heals itself.** db-import recreates it and the `realmlist` init container patches in
  `wow.minch.zone`, `192.168.20.93` and the realm name on every boot.
- **Accounts do not survive.** Recreate them afterwards (`scripts/wow-accounts.py`).

## Verifying

The line to look for is the one that reads `> Not found modules config files` on the upstream image. On a
correct build it becomes:

```
Loading Modules Configuration...

Using modules configuration:
> mod_aoe_loot.conf
> AutoBalance.conf
> transmog.conf
```

```bash
# module configs found, rather than "Not found modules config files"
kubectl -n wow logs deploy/wow-worldserver -c worldserver | grep -A4 "Loading Modules Configuration"

# transmog schema applied
kubectl -n wow exec deploy/wow-database -- \
  mysql -uroot -p"$PASS" -e "SHOW TABLES FROM acore_characters LIKE 'custom_transmogrification%'"
```

In game: `.reload config` re-reads module config without a restart. AutoBalance also has `.autobalance`
commands for inspecting what it is scaling.

## Configuring the server and modules

Every setting lives as an `AC_*` environment variable on the worldserver container in
`k8s/apps/wow/worldserver.yaml`. Never edit the `.conf` files: they sit on the Longhorn volume where nothing
reviews them, and environment beats file in `ConfigMgr::GetValueDefault`, so a value set here cannot be
shadowed by a stale copy an earlier image left behind.

The image does ship the upstream `.conf` defaults — the runtime stage promotes each module's `.conf.dist` to a
live `.conf`, because `LoadModulesConfigs` reads only `.conf` and would otherwise warn once per option at every
boot. Treat those files as inert.

### Deriving the variable name

Uppercase; `.`, `-` and space become `_`; a lower-to-upper transition or a letter/digit boundary also inserts
`_`. That is `IniKeyToEnvVarKey` in `Config.cpp`. The rule has sharp edges — `AutoBalance.Enable.5M` becomes
`AC_AUTO_BALANCE_ENABLE_5_M`, while `AOELoot.Enable` becomes `AC_AOELOOT_ENABLE` with no split at all, because
the run is already uppercase and the rule only fires on lower-to-upper.

Guessing is unsafe, because **a wrong name fails silently**: `OverrideWithEnvVariablesIfAny` walks the loaded
config and quietly ignores anything that does not match. There is no warning and no error.

Two things that look like verification but are not:

- The `Found config value 'X' from environment variable 'AC_Y'` log only prints when the environment value
  _differs_ from the file. Anything restating a default is silent.
- `worldserver --dry-run` loads module configs but never reads module options, so a bad value passes.

The reliable check is to compile the real function and diff it against the manifest:

```bash
# extract IniKeyToEnvVarKey from Config.cpp into a main(), then:
./mangle AutoBalance.MinPlayers.RaidHeroic AOELoot.Range MinPetitionSigns
# AC_AUTO_BALANCE_MIN_PLAYERS_RAID_HEROIC
# AC_AOELOOT_RANGE
# AC_MIN_PETITION_SIGNS
```

All 25 variables currently in the manifest were verified this way, and nine of them additionally by booting the
image against a throwaway database with deliberately invalid values and confirming each reported
`Bad value defined for name '...'`.

### What is set, and why

Most of it restates upstream defaults so the knobs are visible in git. The non-defaults:

| Setting                                         | Value | Stock | Why                                                                  |
| ----------------------------------------------- | ----- | ----- | -------------------------------------------------------------------- |
| `AC_MIN_PETITION_SIGNS`                         | 0     | 9     | 9 signatures means every other player on a ten-person realm          |
| `AC_QUESTS_IGNORE_RAID`                         | 1     | 0     | raid groups otherwise stop ordinary quests completing                |
| `AC_INSTANCE_IGNORE_RAID`                       | 1     | 0     | enter raid instances without forming a raid group                    |
| `AC_ALLOW_TWO_SIDE_INTERACTION_GROUP`           | 1     | 0     | a small realm cannot afford a faction split                          |
| `AC_ALLOW_TWO_SIDE_INTERACTION_CHAT`            | 1     | 0     | as above. `Channel`/`Guild`/`Auction`/`Calendar` stay stock          |
| `AC_MAP_UPDATE_THREADS`                         | 4     | 1     | an idle realm measured ~840m CPU on the single stock thread          |
| `AC_AUTO_BALANCE_REWARD_SCALING_XP`             | 0     | 1     | scaled-down rewards compound with the party split, see below         |
| `AC_AUTO_BALANCE_REWARD_SCALING_MONEY`          | 0     | 1     | as above                                                             |
| `AC_TRANSMOGRIFICATION_ALLOW_LOWER_TIERS`       | 1     | 0     | stock blocks lower-tier appearances, most of the point of the module |
| `AC_TRANSMOGRIFICATION_ALLOW_MIXED_ARMOR_TYPES` | 1     | 0     | plate wearers can take cloth appearances                             |

`AC_MAP_UPDATE_THREADS` is paired with a CPU request of 2 (raised from 1) on the same container. Change them
together — the request is what the scheduler sizes the pod by.

**`AutoBalance.MinPlayers` is a floor, not a target.** The config is explicit: "if fewer than this number of
players enter the instance, scale to the specified value." The stock 1 means no artificial floor, so content
tracks whoever is actually present. Raise it only to stop a small group trivialising an instance.

Scaling runs in **both** directions: take more than five players into a 5-man and it scales up.
`AC_AUTO_BALANCE_PLAYER_CHANGE_NOTIFY=1` announces each rescale in chat.

**Both XP and money are exempt from reward scaling.** Under `RewardScaling.Method="dynamic"` a reward is cut in
proportion to how far the creature was scaled down, on top of the party split the core already applies. That
compounds badly — a solo player in a 5-man collects roughly 12% of a mob's XP, so easier fights buy themselves
a levelling rate far under blizzlike even with `Rate.XP.*` at 1, and gold income takes the same cut. Setting
`AC_AUTO_BALANCE_REWARD_SCALING_XP=0` and `..._MONEY=0` skips the module's reward hooks outright, leaving the
core's group split as the only divisor. The matching `..._MODIFIER` values are inert while the toggles are 0 —
the module skips the hook rather than multiplying by them — and are kept only so the knobs stay visible.

The trade is that scaled content now pays full freight: a duo clearing a 5-man scaled to two players still
earns what two players would earn from an unscaled mob. That is the intended bargain for a small realm, but it
does mean AutoBalance is no longer self-balancing on rewards. If levelling or gold ends up too fast, these two
toggles are the first thing to put back.

`RewardScaling.Method="fixed"` reads like the middle ground and is not one. It pays every player a flat
full-group share regardless of headcount — `amount * XPModifier * (currentPlayerCount / maxPlayerCount)`, so a
lone player in a 5-man gets 20% of a mob's XP and a full group gets the same 20% each. That is worse than
exempting the reward at every group size, and better than `dynamic` only at one or two players. `METHOD` is
global — it governs XP and money together — which is why the per-reward toggles are the right lever and
`METHOD` stays at its stock `dynamic` even though nothing now consumes it.

All of this is config-only: an env edit and a pod restart, no rebuild. `.reload config` picks changes up live.

## Keeping it current

This is the maintenance cost the modules bought:

- **Renovate can no longer see the worldserver or db-import images.** It still tracks `ac-wotlk-authserver`,
  `ac-wotlk-client-data` and `mysql`. Rebuilding is a manual, deliberate act.
- **Rebuild worldserver and db-import together, always.** They share a database version check. Publishing one
  without the other is the failure mode this two-image setup exists to prevent.
- **Watch for drift against `client-data` and `authserver`** when Renovate bumps them. In practice AzerothCore
  migrations are additive and this is quiet, but a worldserver pinned months behind a bumped authserver is
  worth a rebuild.
- The Dockerfile restates upstream's cmake invocation and apt dependency list. If a build fails on a missing
  header or an unknown cmake option, diff `docker/azerothcore/Dockerfile` against upstream
  `apps/docker/Dockerfile` before reaching for anything cleverer.

## Sources

- [AzerothCore wiki — installing a module](https://www.azerothcore.org/wiki/installing-a-module)
- [AzerothCore wiki — install with docker](https://www.azerothcore.org/wiki/install-with-docker)
- [upstream apps/docker/Dockerfile](https://github.com/azerothcore/azerothcore-wotlk/blob/master/apps/docker/Dockerfile)
- [mod-transmog](https://github.com/azerothcore/mod-transmog)
- [mod-autobalance](https://github.com/azerothcore/mod-autobalance)
