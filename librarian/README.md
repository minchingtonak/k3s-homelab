# librarian

Periodic per-album metadata pipeline for the music library: ReplayGain 2.0
(rsgain), BPM (aubio), and musical key (keyfinder-cli), with a uniform
check → act → report skip model. Implements `docs/librarian-spec.md`.

## Usage

```bash
bin/librarian [library-root]        # root defaults to $LIBRARIAN_LIBRARY_DIR or /data/music
bin/librarian -v /tmp/librarian-test-run/clean/
```

Requires Python ≥ 3.11 with `mutagen` (`uv pip install mutagen` or
`pip install mutagen`). The three analysis binaries must be resolvable
(see Tool provisioning).

Never run against the master `librarian-test-bank/` — always a disposable copy
(the tool writes tags in place).

## Configuration (environment)

| Variable                                              | Default             | Meaning                                      |
| ----------------------------------------------------- | ------------------- | -------------------------------------------- |
| `LIBRARIAN_LIBRARY_DIR`                               | `/data/music`       | library root (positional arg overrides)      |
| `LIBRARIAN_SCAN_TIMEOUT`                              | `43200` (12h)       | pipeline window, seconds, after the walk     |
| `LIBRARIAN_SUBPROCESS_TIMEOUT`                        | `1800`              | per-binary wall clock, seconds               |
| `LIBRARIAN_HEARTBEAT_INTERVAL`                        | `60`                | heartbeat cadence, seconds                   |
| `LIBRARIAN_RG_FINGERPRINT`                            | `rsgain-3.8/tp/-18` | settings fingerprint = required RGTOOL value |
| `PUSHOVER_TOKEN` + `PUSHOVER_USER_KEY`                | unset               | unset ⇒ pushes are logged, not sent          |
| `LIBRARIAN_HEADLAMP_URL`, `POD_NAME`, `POD_NAMESPACE` | unset               | enable the pod-logs deep link                |
| `LIBRARIAN_<TOOL>_BIN`                                | unset               | explicit binary path override (local runs)   |

## Tests

```bash
uv venv .venv && uv pip install --python .venv mutagen pytest
.venv/bin/python -m pytest tests/        # 101 unit tests, synthetic inputs only
```

`tests/fixtures/` holds 0.3s silence files per tag family (regenerate with
ffmpeg if ever needed: `ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 0.3`).

Acceptance runs against `librarian-test-bank/` copies exercise the real binaries
(rsgain 3.8, aubio 0.4.9, keyfinder-cli 1.2.0) — see the scratch Dockerfile
pattern used during development (ubuntu:24.04 + `aubio-tools` + libKeyFinder
2.2.2 + keyfinder-cli 1.2.0 built from source).

## Tool provisioning and the image

Everything ships in one image — `docker/librarian/Dockerfile` (build with
`scripts/build-librarian-image.sh`, which follows the azerothcore conventions:
dated tag, manifests pin by digest):

```bash
LIBRARIAN_IMAGE_PUSH=0 bash scripts/build-librarian-image.sh   # local build+test
bash scripts/build-librarian-image.sh                      # build + push
```

Contents: multi-stage ubuntu:24.04 (same digest pin as docker/azerothcore) —

- **rsgain 3.8** downloaded from the Forgejo generic-package registry at build
  time, checksum-pinned to the upstream release
  (`api/packages/akmin/generic/rsgain/3.8/rsgain-3.8-Linux.tar.xz`, sha256
  `4939de3b…`; that registry entry remains the canonical source).
- **aubio 0.4.9** via `aubio-tools` (the CLI is a python3 console script; the
  image's python3 runtime satisfies it).
- **keyfinder-cli 1.2.0** compiled from libKeyFinder 2.2.2 + the CLI, with an
  IMPORTED-target CMake shim (upstream ships only pkg-config; the library's own
  tests can't compile on modern GCC, so only the `keyfinder` target is built) —
  staged into `/opt/keyfinder` with `libfftw3-double3` providing the runtime fftw.
- **librarian** at `/opt/librarian`, uid/gid 100000, `PYTHONDONTWRITEBYTECODE=1` for the
  read-only-rootfs pod, ENTRYPOINT `/opt/librarian/librarian`.

Tool resolution in-process is just PATH (with a `LIBRARIAN_*_BIN` env override for
local runs) — an earlier runtime-fetch design (tarballs from the generic
registry into emptyDir) was superseded by this image; the tarball build
recipes survive in `scratch/librarian/packaging/` as a fallback path. All CronJob
env for tools (`LIBRARIAN_TOOLS_DIR`, `LIBRARIAN_*_URL`, `LIBRARIAN_*_SHA256`) is gone.

The image is what the E2E validated: read-only rootfs, tmpfs /tmp, uid/gid
100000, no code mounts, fresh bank copy — full pipeline green, exit 0.

## Design decisions (where the spec left room)

- **rsgain `custom` mode, not `easy`.** Measured: easy mode exits 0 even when
  files fail to scan ("No files were scanned"), which would break the spec's
  non-zero-exit error contract; custom mode exits 1 on any file failure and
  takes an explicit file list, so album grouping equals the walk's grouping.
  Invocation: `rsgain custom -a -t -l -18 -c p -s i <files…>` — album gain,
  true peak, -18 LUFS, positive-gain clipping protection (easy-mode default),
  write tags. RGTOOL written by rsgain itself is exactly the fingerprint
  `rsgain-3.8/tp/-18`; the step additionally patches any file still missing it.
- **"Multithreaded"** is delivered at album granularity (per-album invocations
  back-to-back); custom mode has no `-m` flag. The fingerprint does not encode
  threading.
- **BPM value** is written as a rounded integer (matching the library's
  existing `TBPM`/`bpm` style). aubio prints interim estimates; the last
  `N.NN bpm` on stdout wins.
- **Key notation** is keyfinder-cli's default standard set (`Gbm`, `D`, `Am`).
  The library already contains mixed notations; presence of `KEY` is what the
  skip model checks.
- **Skip-tag values carry the reason** (`aubio exit 1`, `rsgain exit 1`,
  `aubio timeout`). Deleting the tag remains the retry mechanism.
- **MP4 BPM** is written to the standard `tmpo` atom (integer) _and_ mirrored
  to a `----:com.apple.iTunes:BPM` freeform tag.
- **Old `KTSKIP` tags** (keytempo-era, seen on some M4A) are deliberately not
  honored — the spec defines `BPMSKIP`/`KEYSKIP`/`RGSKIP` only.
- **Album** = any directory containing audio files directly (files grouped by
  immediate parent). Artist folders, art folders etc. are not albums.
- **Scan-window expiry** is checked between albums (and SIGTERM is honored
  inside subprocess polling); a huge album is not interrupted mid-scan.
- **Pushover delivery is best-effort**: a failed POST is logged but never
  changes the exit code. Missing credentials (local runs) ⇒ pushes are logged
  only.
- **Window-expiry "zero work"** means no step performed any action (scan,
  tag, or skip-tag write) during the run.
- **TBPM = 0 counts as present** (skip); the skip model is a presence check.

## Fixture drifts observed in `librarian-test-bank/` (2026-10-02 census)

- `untagged/skyrim-atmospheres` carries a valid `RGTOOL` marker and RG tags;
  only BPM/KEY process there. The bank contains **no readable album lacking
  RGTOOL**, so the RG processing path was acceptance-tested on a scratch copy
  with markers stripped.
- `rg-only-no-bpmkey/` files actually have BPM/KEY/RGTOOL — the category
  yields a no-op run despite its name.
- With aubio 0.4.9 / keyfinder-cli 1.2.0 the "surround audio will fail"
  claim does not hold (Interstella 5.1 FLAC and Seal 5.1 M4A analyze fine);
  the genuinely failing files are the corrupt OGGs and Migos MP3. The spec's
  behavior contract (non-zero exit ⇒ skip tag) is what governs, and it held.
