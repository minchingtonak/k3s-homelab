# Music Librarian — Functional Spec

## Purpose

A single Kubernetes CronJob that periodically walks the music library and, for each album, applies a pipeline of metadata operations (ReplayGain, BPM, key, and future steps) so that every audio file carries complete, consistent tags. One job, one walk, pluggable steps.

## Core Behavior

Every 2 hours (cron: `"25 */2 * * *"`, timezone `America/New_York`):

1. **Walk** the music library (`/data/music` on NFS, ~28,000 audio files in ~5,700 album directories). Read each file's tag headers once — a single filesystem pass whose results are shared by all pipeline steps. Must handle ID3v2 (MP3/WAV/AIFF), Vorbis comments (OGG/FLAC/Opus), and MP4 atoms (M4A) for both reading and writing custom tags.

2. **Apply the pipeline** to each album directory. Steps run in order and are independent — a failure in one does not block others.

   ### Step 1: ReplayGain 2.0

   - **Check**: does any file in this album lack an `RGTOOL` marker matching the current settings fingerprint?
   - **Act**: if yes, invoke `rsgain` (a standalone C++ binary, run as a subprocess) on the album directory, then write the `RGTOOL` marker onto every file in that album.
   - **Skip**: if all files have matching markers.
   - **Settings**: true-peak measurement, -18 LUFS target, album + track tags, multithreaded.

   ### Step 2: BPM

   - **Check**: does the file lack a `BPM` tag and lack a `BPMSKIP` tag?
   - **Act**: invoke `aubio tempo <file>` as a subprocess, parse stdout for a BPM number, write the `BPM` tag.
   - **Skip**: if the file has a `BPM` tag (done) or a `BPMSKIP` tag (permanently failed).
   - **Errors**: non-zero exit or unparseable output = write `BPMSKIP` immediately. The job is stateless — there is no cross-run memory to distinguish transient from permanent failure, and subprocess failures are overwhelmingly permanent (corrupt data, unsupported format, surround audio). Delete the tag to retry a fixed file.

   ### Step 3: Musical Key

   - **Check**: does the file lack a `KEY` tag and lack a `KEYSKIP` tag?
   - **Act**: invoke `keyfinder-cli <file>` as a subprocess, parse stdout for a key, write the `KEY` tag.
   - **Skip**: if the file has a `KEY` tag (done) or a `KEYSKIP` tag (permanently failed).
   - **Errors**: non-zero exit or unparseable output = write `KEYSKIP` immediately, same rationale as step 2.

   ### Future steps (not implemented, but the architecture must support them)

   - Genre tagging, cover art embedding, any per-file or per-album metadata operation.
   - Each step: check → act → report. Steps are independent.

3. **Report** via Pushover (see Notification Contract).

## Step Skip Model (uniform across all steps)

Every step uses the same three-condition check, in order:

```
1. Output tag present       → done, skip
2. Step skip tag present    → permanently failed, skip
3. Neither present          → process this file
```

| Step       | Output tag (condition 1)                     | Skip tag (condition 2) |
| ---------- | -------------------------------------------- | ---------------------- |
| ReplayGain | `RGTOOL` marker matching current fingerprint | `RGSKIP`               |
| BPM        | `BPM` tag exists                             | `BPMSKIP`              |
| Key        | `KEY` tag exists                             | `KEYSKIP`              |

Skip tags exist solely to prevent infinite retries on permanently-unanalyzable files. They are written on **first failure** — the job is stateless, so there is no mechanism to distinguish a first failure from a consecutive one, and subprocess failures are overwhelmingly permanent. Delete a skip tag to retry that step on a fixed file.

The one asymmetry: ReplayGain's `RGTOOL` marker carries a settings fingerprint, so changing scan settings (target loudness, peak mode) automatically queues the whole library for one re-tagging pass. BPM and key are absolute facts, not reference-relative, so their output tags don't need fingerprints.

## Pipeline Step Independence

- If step 1 succeeds but step 2 crashes on the same file, the RG tags persist and only the BPM tag is missing. Next run retries only the missing step.
- Steps do not roll back or block each other.
- A file can have any combination of tags. Each step checks only its own output and skip tags.

## File-Level Edge Cases

| Condition                               | Behavior                                                        |
| --------------------------------------- | --------------------------------------------------------------- |
| Tags cannot be read (corrupt metadata)  | Skip entirely, count as "skipped", never fail the run           |
| Any tag write fails (including markers) | The step's work is incomplete; retry next run                   |
| Any analysis binary exits non-zero      | Write that step's skip tag immediately; report as un-analyzable |

## Notification Contract

| Situation                                 | Pushover                                                              | Job exit  |
| ----------------------------------------- | --------------------------------------------------------------------- | --------- |
| Any work done, no errors                  | Combined stats push (per-step results + library facts)                | 0 (green) |
| Work done + some files skipped            | Stats push first, then a normal-priority notice listing them          | 0 (green) |
| Nothing to do                             | Silence                                                               | 0 (green) |
| Scan window expired with work done        | Progress push ("window ended, N files processed, next run continues") | 0 (green) |
| Scan window expired with zero work        | Failure push                                                          | 1 (red)   |
| Infrastructure failure (tool fetch, etc.) | Failure push                                                          | 1 (red)   |

Stats push contents (when work was done):

- Files processed per step
- Album/release/artist counts
- Average loudness (LUFS) with a Spotify -14 LUFS comparison
- Clip adjustments
- Median BPM and key distribution (when those steps ran)
- Elapsed time
- Link to this run's pod logs in Headlamp

## Concurrency

None needed — single job. `concurrencyPolicy: Forbid` prevents self-overlap.

## Runtime Dependencies

All fetched at runtime into ephemeral storage (emptyDir), verified by checksum, discarded with the pod. No in-process download retries — the Kubernetes job controller's backoff (limit 4) handles transient failures.

All binaries are packaged into the internal Forgejo registry (`forgejo.item.fyi`), sha256-pinned:

- **rsgain 3.8** (static binary) — ReplayGain 2.0 computation
- **aubio** (tempo CLI) — BPM detection
- **keyfinder-cli** (libKeyFinder wrapper) — musical key detection

Note that rsgain already resides in the registry, aubio and keyfinder-cli do not, they will need to be added.

## Container

- Any base image that provides the required runtime and glibc for the CLI binaries
- Runs as UID/GID 100000 (matching Lidarr's file ownership on the NFS export)
- readOnlyRootFilesystem, all capabilities dropped
- Memory: **2Gi** limit (tag I/O + subprocess output buffers; no in-process audio decoding)
- CPU: no limit
- activeDeadlineSeconds: **50400** (14h)

## Timeouts

- **Scan timeout** (12h, configurable via env): applies to pipeline execution (after the walk). With work done: progress push, exit 0. With zero work: failure push, exit 1.
- **Job deadline** (14h): the kubelet kills the pod. Must exceed scan timeout plus walk duration (~35-70 min) with margin, or the kubelet silently kills before the wrapper can notify.

## Observability

- Pod logs stream live (tool output, per-file results, heartbeats during silent stretches).
- Heartbeat every 60 seconds during the walk.
- SIGTERM handler: on kubelet kill, send a best-effort Pushover notification.
- Notifications carry a Headlamp deep link to this run's pod logs.
- Pushover credentials from the `librarian-secret` Kubernetes Secret.

## Testing Methodology

The implementation must be testable locally against `librarian-test-bank/` (124 real audio files, 2.5GB, in this repository) before any cluster deployment. No push-to-remote-and-observe testing.

**Preserving the master bank:** the librarian tool modifies files in-place (writes tags). Always run against a disposable copy, never the master:

```bash
# Before each test run — copy the master to a scratch directory
cp -r librarian-test-bank/ /tmp/librarian-test-run/

# Run the tool against the copy
bin/librarian /tmp/librarian-test-run/clean/
bin/librarian /tmp/librarian-test-run/binary-fails/
# etc.

# Discard when done — the master is untouched
rm -rf /tmp/librarian-test-run/
```

The master `librarian-test-bank/` directory is a reference fixture — its tag states are load-bearing for test repeatability. Never run the librarian tool directly against it.

**Local acceptance tests:** run the tool against each test-bank directory (the copy) and verify the expected behavior from `docs/librarian-test-catalog.md`:

```
librarian-test-bank/clean/                 → all steps skip, silent exit 0
librarian-test-bank/rg-only-no-bpmkey/     → steps 2+3 process, step 1 skips
librarian-test-bank/untagged/              → all steps process, tags written
librarian-test-bank/binary-fails/          → binaries exit non-zero, skip tags written, green exit
librarian-test-bank/metadata-unreadable/   → files skipped, counted, green exit
librarian-test-bank/long-audio/            → all steps process normally (no length cap)
librarian-test-bank/unicode-paths/         → subprocess arguments correctly quoted
```

**Unit tests** for the pure functions (tag read/write, skip-tag logic, notification formatting, timeout decisions) using synthetic inputs — no audio files needed.

**Pushover integration** tested with a sandbox token or by verifying the HTTP request payload (not requiring actual delivery).

**What this replaces:** the previous implementation was tested by pushing to the remote and observing cluster behavior. That pattern is slow (Flux reconciliation + NFS walk latency), non-deterministic (dependent on cluster state), and risks affecting the production library. All of it is replaceable by pointing the tool at a local directory.

## What This Spec Deliberately Does NOT Specify

- Programming language or runtime
- How the walk is implemented (sequential, parallel, io_uring, readdir — any approach)
- Which tag library (mutagen, TagLib, taglib-sharp, custom parser — any that handles the three families)
- Internal code structure, error handling patterns, or logging framework
- Container base image (any that provides the required runtime and glibc for the CLI binaries)
