# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
#
# AzerothCore worldserver + db-import images with server-side modules compiled in.
#
# Why this exists: AzerothCore modules are C++ statically linked into the core, so
# the upstream `acore/ac-wotlk-*` images cannot load one at runtime. The only way
# to add mod-transmog, mod-autobalance, mod-aoe-loot or mod-ah-bot is to rebuild.
# See docs/wow-modules.md.
#
# Why *two* images and not just the worldserver: mod-transmog, mod-aoe-loot and
# mod-ah-bot ship SQL under `data/sql/`, and the update fetcher looks for it at
# `<source>/modules/<module>/data/sql/` (UpdateFetcher.cpp). The upstream
# db-import image has an empty modules dir, so it would silently never apply
# their schema. Building db-import from the same tree also guarantees the two
# binaries agree on the database version, which they would not if one tracked
# upstream `master` and the other a commit we pinned weeks earlier.
#
# The stage layout deliberately mirrors upstream `apps/docker/Dockerfile` so the
# results stay drop-in compatible with k8s/apps/wow: same uid/gid 1000, same
# entrypoint, same paths, same env defaults. Keep it that way — the manifests
# assume all of it.
#
# Unlike upstream this clones the source itself rather than expecting an
# azerothcore-wotlk checkout as the build context. That keeps every input pinned
# in this one file and the build context empty, at the cost of having to restate
# upstream's cmake invocation below.

ARG UBUNTU_VERSION=26.04@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b

# Source refs. Defaults track master because AzerothCore cuts no releases; pass
# explicit commit SHAs to make a build reproducible:
#   docker build --build-arg AC_REF=<sha> --build-arg MOD_TRANSMOG_REF=<sha> ...
ARG AC_REPO=https://github.com/azerothcore/azerothcore-wotlk.git
ARG AC_REF=master
ARG MOD_TRANSMOG_REPO=https://github.com/azerothcore/mod-transmog.git
ARG MOD_TRANSMOG_REF=master
ARG MOD_AUTOBALANCE_REPO=https://github.com/azerothcore/mod-autobalance.git
ARG MOD_AUTOBALANCE_REF=master
ARG MOD_AOE_LOOT_REPO=https://github.com/azerothcore/mod-aoe-loot.git
ARG MOD_AOE_LOOT_REF=master
ARG MOD_AH_BOT_REPO=https://github.com/azerothcore/mod-ah-bot.git
ARG MOD_AH_BOT_REF=master

############
# Skeleton #
############

# Directory layout and timezone shared by every stage below. Not useful alone.
FROM ubuntu:${UBUNTU_VERSION} AS skeleton

ARG TZ=Etc/UTC
ARG DEBIAN_FRONTEND=noninteractive

ENV AC_FORCE_CREATE_DB=1

RUN mkdir -pv \
    /azerothcore/bin \
    /azerothcore/data \
    /azerothcore/deps \
    /azerothcore/env/dist/bin \
    /azerothcore/env/dist/data/Cameras \
    /azerothcore/env/dist/data/dbc \
    /azerothcore/env/dist/data/maps \
    /azerothcore/env/dist/data/mmaps \
    /azerothcore/env/dist/data/vmaps \
    /azerothcore/env/dist/logs \
    /azerothcore/env/dist/temp \
    /azerothcore/env/dist/etc \
    /azerothcore/modules \
    /azerothcore/src \
    /azerothcore/build

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
    && echo "$TZ" > /etc/timezone \
    && dpkg-reconfigure --frontend noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /azerothcore

#########
# Build #
#########

FROM skeleton AS build

ARG DEBIAN_FRONTEND=noninteractive

# Toolchain first so a source ref bump does not invalidate the apt layer. This
# list is copied from upstream apps/docker/Dockerfile; if a build ever fails on
# a missing header, diff it against upstream before doing anything cleverer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential ccache libtool cmake-data ninja-build cmake clang \
      git lsb-base curl unzip default-mysql-client openssl \
      default-libmysqlclient-dev libboost-all-dev libssl-dev libmysql++-dev \
      libreadline-dev zlib1g-dev libbz2-dev libncurses5-dev liblzma-dev \
    && rm -rf /var/lib/apt/lists/*

ARG AC_REPO
ARG AC_REF

# `git init` + fetch rather than `git clone`, because /azerothcore already holds
# the skeleton directories and clone refuses a non-empty target. Fetching a ref
# by name also accepts a raw commit SHA, which `clone --branch` does not.
#
# Depth 1 is enough. cmake's genrev.cmake runs `git describe --match 0.1 --always`
# for the revision string; shallow history has neither the tag nor the commits
# between it and HEAD, so describe falls through to `--always` and the server
# reports a bare commit hash. Cosmetic, and the only thing a deeper fetch buys.
RUN git init -q /azerothcore \
    && git -C /azerothcore remote add origin "$AC_REPO" \
    && git -C /azerothcore fetch -q --depth 1 origin "$AC_REF" \
    && git -C /azerothcore checkout -q FETCH_HEAD \
    && git config --global --add safe.directory /azerothcore \
    && echo "azerothcore-wotlk $(git -C /azerothcore rev-parse HEAD)"

ARG MOD_TRANSMOG_REPO
ARG MOD_TRANSMOG_REF
ARG MOD_AUTOBALANCE_REPO
ARG MOD_AUTOBALANCE_REF
ARG MOD_AOE_LOOT_REPO
ARG MOD_AOE_LOOT_REF
ARG MOD_AH_BOT_REPO
ARG MOD_AH_BOT_REF

# Module directory names are load-bearing twice over: cmake derives the compiled
# module list from them, and the update fetcher builds the SQL path from that
# list. This is the "remove the -master suffix" warning in the AzerothCore wiki,
# avoided by naming the clone target explicitly.
RUN set -eu; \
    for spec in \
      "mod-transmog|$MOD_TRANSMOG_REPO|$MOD_TRANSMOG_REF" \
      "mod-autobalance|$MOD_AUTOBALANCE_REPO|$MOD_AUTOBALANCE_REF" \
      "mod-aoe-loot|$MOD_AOE_LOOT_REPO|$MOD_AOE_LOOT_REF" \
      "mod-ah-bot|$MOD_AH_BOT_REPO|$MOD_AH_BOT_REF" \
    ; do \
      name="${spec%%|*}"; rest="${spec#*|}"; repo="${rest%%|*}"; ref="${rest#*|}"; \
      dir="/azerothcore/modules/$name"; \
      git init -q "$dir"; \
      git -C "$dir" remote add origin "$repo"; \
      git -C "$dir" fetch -q --depth 1 origin "$ref"; \
      git -C "$dir" checkout -q FETCH_HEAD; \
      git config --global --add safe.directory "$dir"; \
      echo "$name $(git -C "$dir" rev-parse HEAD)"; \
    done

ARG CTYPE="RelWithDebInfo"
ARG CSCRIPTS="static"
ARG CMODULES="static"
# `dbimport` is registered as a tool, not an app, so TOOLS_BUILD=none would drop
# it. db-only builds it and skips the map/vmap/mmap extractors, which this
# deployment never uses — the client-data init container ships pre-extracted
# data. Worth roughly a third of the build.
ARG CTOOLS_BUILD="db-only"
# Build-log noise only. Upstream defaults this ON; a full AzerothCore build with
# warnings on buries the errors that matter.
ARG CWITH_WARNINGS="OFF"
ARG CMAKE_EXTRA_OPTIONS=""

WORKDIR /azerothcore/build

# ccache is mounted rather than baked so it survives across builds without
# landing in the image. First build is cold (~20 min on 16 cores); a rebuild
# that only bumps a module ref reuses nearly all of it.
RUN --mount=type=cache,target=/ccache,sharing=locked \
    CCACHE_DIR=/ccache \
    CCACHE_MAXSIZE=2G \
    CCACHE_COMPILERCHECK=content \
    CCACHE_SLOPPINESS=pch_defines,time_macros,include_file_mtime \
    cmake /azerothcore \
      -G Ninja \
      -DCMAKE_INSTALL_PREFIX="/azerothcore/env/dist" \
      -DAPPS_BUILD="all" \
      -DTOOLS_BUILD="$CTOOLS_BUILD" \
      -DSCRIPTS="$CSCRIPTS" \
      -DMODULES="$CMODULES" \
      -DWITH_WARNINGS="$CWITH_WARNINGS" \
      -DCMAKE_BUILD_TYPE="$CTYPE" \
      -DCMAKE_CXX_COMPILER="clang++" \
      -DCMAKE_C_COMPILER="clang" \
      -DCMAKE_CXX_COMPILER_LAUNCHER="ccache" \
      -DCMAKE_C_COMPILER_LAUNCHER="ccache" \
      -DBoost_USE_STATIC_LIBS="ON" \
      $CMAKE_EXTRA_OPTIONS \
    && cmake --build . --config "$CTYPE" -j "$(nproc)" \
    && cmake --install . --config "$CTYPE"

###########
# Runtime #
###########

FROM skeleton AS runtime

ARG USER_ID=1000
ARG GROUP_ID=1000
ARG DOCKER_USER=acore
ARG DEBIAN_FRONTEND=noninteractive

ENV ACORE_COMPONENT=undefined

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      libmysqlclient21 libreadline8 libicu74 libncurses5-dev \
      gettext-base default-mysql-client \
      adduser \
    && rm -rf /var/lib/apt/lists/*

# env/ref/etc is the entrypoint's source for `cp -rn` into the real config dir.
# In this cluster that config dir is a Longhorn volume, so anything not present
# here on the pod's *first* boot never appears at all.
COPY --from=build /azerothcore/env/dist/etc/ /azerothcore/env/ref/etc

# cmake installs module configs as `.conf.dist`, and — unlike the component
# config — nothing promotes them to a live `.conf`. LoadModulesConfigs reads
# only `.conf`, so without this every module option falls back to its compiled
# default and logs a warning per option at startup (~100 lines a boot).
#
# These are upstream defaults, not our settings. Real tuning belongs in
# k8s/apps/wow/worldserver.yaml as AC_* env vars, which override file values
# (ConfigMgr::GetValueDefault) and so cannot be shadowed by a stale copy of this
# file left on the volume by an earlier image.
RUN for f in /azerothcore/env/ref/etc/modules/*.conf.dist; do \
      [ -e "$f" ] || continue; \
      cp -n "$f" "${f%.dist}"; \
    done

VOLUME /azerothcore/env/dist/etc

ENV PATH="/azerothcore/env/dist/bin:$PATH"

# ubuntu >= 23.04 ships a stock user at uid 1000; it has to go before the acore
# user can claim that uid. https://bugs.launchpad.net/cloud-images/+bug/2005129
RUN userdel --remove ubuntu \
    && addgroup --gid "$GROUP_ID" "$DOCKER_USER" \
    && adduser --disabled-password --gecos '' --uid "$USER_ID" --gid "$GROUP_ID" "$DOCKER_USER" \
    && passwd -d "$DOCKER_USER" \
    && chown -R "$DOCKER_USER:$DOCKER_USER" /azerothcore

COPY --from=build --chown=$USER_ID:$GROUP_ID --chmod=755 \
     /azerothcore/apps/docker/entrypoint.sh /azerothcore/entrypoint.sh

USER $DOCKER_USER

ENTRYPOINT ["/usr/bin/env", "bash", "/azerothcore/entrypoint.sh"]

################
# World Server #
################

FROM runtime AS worldserver

LABEL description="AzerothCore World Server (mod-transmog, mod-autobalance)"

ENV ACORE_COMPONENT=worldserver
# Migrations are db-import's job; two components racing on the same schema is
# how you get a half-applied update.
ENV AC_UPDATES_ENABLE_DATABASES=0
ENV AC_DISABLE_INTERACTIVE=1
ENV AC_CLOSE_IDLE_CONNECTIONS=0

COPY --from=build --chown=$DOCKER_USER:$DOCKER_USER \
     /azerothcore/env/dist/bin/worldserver /azerothcore/env/dist/bin/worldserver

VOLUME /azerothcore/env/dist/etc

CMD ["worldserver"]

#############
# DB Import #
#############

FROM runtime AS db-import

LABEL description="AzerothCore Database Import tool (mod-transmog, mod-autobalance)"

ENV ACORE_COMPONENT=dbimport

# Both directories are read at runtime, not build time: `data` holds the base
# schema and `modules` the per-module SQL the update fetcher walks. Dropping
# either leaves the corresponding updates silently unapplied.
COPY --from=build --chown=$DOCKER_USER:$DOCKER_USER /azerothcore/data /azerothcore/data
COPY --from=build --chown=$DOCKER_USER:$DOCKER_USER /azerothcore/modules /azerothcore/modules

COPY --from=build --chown=$DOCKER_USER:$DOCKER_USER \
     /azerothcore/env/dist/bin/dbimport /azerothcore/env/dist/bin/dbimport

CMD ["/azerothcore/env/dist/bin/dbimport"]
