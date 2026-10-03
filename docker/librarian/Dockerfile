# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
#
# librarian CronJob image: the library metadata pipeline and its three analysis
# binaries (rsgain, aubio, keyfinder-cli) in one self-contained image.
#
# Why baked instead of runtime-fetched (docs/librarian-spec.md originally
# sketched fetching pinned artifacts into an emptyDir): the aubio/keyfinder
# artifacts are glibc/libpython-locked to the base anyway, the librarian code needs
# an image regardless, kubelet caches digest-pinned images (no per-run
# download), and one @sha256: digest pins tools + libs + code together.
# rsgain still comes from the Forgejo generic-package registry at build time —
# that entry remains the canonical source, checksum-verified here.
#
# Build context is librarian/ (see scripts/build-librarian-image.sh); the
# base pin matches docker/azerothcore so the cluster patches one ubuntu.

ARG UBUNTU_VERSION=24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

# --- stage 1: keyfinder-cli builder ----------------------------------------
# Built from source because nobody packages it: libKeyFinder + the CLI wrapper,
# plus an IMPORTED-target shim (upstream ships only pkg-config). The library's
# own tests cannot compile on modern GCC (SIGSTKSZ), so only the `keyfinder`
# target is built.
FROM ubuntu:${UBUNTU_VERSION} AS keyfinder-builder

ARG LIBKEYFINDER_REF=v2.2.2
ARG KEYFINDER_CLI_REF=v1.2.0

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        build-essential cmake pkg-config git ca-certificates \
        libfftw3-dev libavformat-dev libavcodec-dev libswresample-dev libavutil-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${LIBKEYFINDER_REF} \
        https://github.com/ibsh/libKeyFinder.git /tmp/libKeyFinder \
    && cmake -S /tmp/libKeyFinder -B /tmp/libKeyFinder/build \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
    && cmake --build /tmp/libKeyFinder/build --target keyfinder -j"$(nproc)" \
    && cmake --install /tmp/libKeyFinder/build --prefix /opt/keyfinder \
    && mkdir -p /opt/keyfinder/lib/cmake/KeyFinder \
    && printf '%s\n' \
        'add_library(KeyFinder::keyfinder UNKNOWN IMPORTED)' \
        'set_target_properties(KeyFinder::keyfinder PROPERTIES' \
        '  IMPORTED_LOCATION /opt/keyfinder/lib/libkeyfinder.so' \
        '  INTERFACE_INCLUDE_DIRECTORIES /opt/keyfinder/include)' \
        > /opt/keyfinder/lib/cmake/KeyFinder/KeyFinderConfig.cmake

RUN git clone --depth 1 --branch ${KEYFINDER_CLI_REF} \
        https://github.com/EvanPurkhiser/keyfinder-cli.git /tmp/keyfinder-cli \
    && cmake -S /tmp/keyfinder-cli -B /tmp/keyfinder-cli/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DKeyFinder_DIR=/opt/keyfinder/lib/cmake/KeyFinder \
    && cmake --build /tmp/keyfinder-cli/build -j"$(nproc)" \
    && cmake --install /tmp/keyfinder-cli/build --prefix /opt/keyfinder

# --- stage 2: runtime --------------------------------------------------------
FROM ubuntu:${UBUNTU_VERSION}

# rsgain 3.8 from the internal Forgejo generic-package registry, checksum-pinned
# to the upstream release (the registry copy is byte-identical).
ARG RSGAIN_URL=https://forgejo.item.fyi/api/packages/akmin/generic/rsgain/3.8/rsgain-3.8-Linux.tar.xz
ARG RSGAIN_SHA256=4939de3b2022a50edc0751343b10a791ea8c1c9ca6505012f21413bf8de365a0
# Pinned to the version the library was validated against.
ARG MUTAGEN_VERSION=1.48.1

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        python3 python3-pip ca-certificates curl xz-utils libfftw3-double3 \
        aubio-tools \
    && pip3 install --break-system-packages --no-cache-dir mutagen==${MUTAGEN_VERSION} \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSL -o /tmp/rsgain.tar.xz "${RSGAIN_URL}" \
    && echo "${RSGAIN_SHA256}  /tmp/rsgain.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/rsgain.tar.xz -C /tmp \
    && install -m 0755 /tmp/rsgain-3.8-Linux/rsgain /usr/local/bin/rsgain \
    && rm -rf /tmp/rsgain.tar.xz /tmp/rsgain-3.8-Linux

COPY --from=keyfinder-builder /opt/keyfinder /opt/keyfinder
ENV LD_LIBRARY_PATH=/opt/keyfinder/lib

# librarian itself: the python package plus a small entry shim on PATH.
COPY librarian /opt/librarian/librarian
COPY bin/librarian /usr/local/bin/librarian
COPY pyproject.toml README.md /opt/librarian/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/keyfinder/bin:$PATH
WORKDIR /opt/librarian

# Spec: runs as UID/GID 100000 (Lidarr's ownership on the NFS export).
USER 100000:100000

ENTRYPOINT ["librarian"]
