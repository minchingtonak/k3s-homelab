"""Pipeline steps: check → act → report, independently failing.

Every step shares the same three-condition skip model (spec):

    1. Output tag present        → done, skip
    2. Step skip tag present     → permanently failed, skip
    3. Neither present           → process

ReplayGain is the one asymmetry: its output tag (RGTOOL) only counts when it
matches the current settings fingerprint, and it runs per album (rsgain
computes album gain+peak over the album's files as one unit).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .analyze import (
    ProcResult,
    ToolError,
    parse_bpm,
    parse_key,
    run_aubio,
    run_keyfinder,
    run_rsgain_album,
)
from .config import Config
from .tags import (
    T_BPM,
    T_BPMSKIP,
    T_KEY,
    T_KEYSKIP,
    T_RGSKIP,
    T_RGTOOL,
    TagSnapshot,
    TagWriteError,
    read_snapshot,
    write_tag,
)
from .walk import Album, Heartbeat

log = logging.getLogger("librarian.steps")


@dataclass
class StepResult:
    """What one step did on one album (or one file, for per-file steps)."""

    name: str
    # "processed" = the step's analysis/tagging action was performed
    scanned: int = 0  # files handed to the analysis binary
    tagged: int = 0  # output tags written by us
    markers_written: int = 0  # fingerprint markers stamped (RG only)
    already_done: int = 0  # skipped: output tag present
    skip_tagged: list[Path] = field(default_factory=list)  # got a skip tag this run
    skip_tag_seen: int = 0  # skipped: skip tag already present
    unreadable: int = 0  # skipped: tags unreadable (per-file steps only)
    skipped_other: list[Path] = field(default_factory=list) # skipped for another reason (too long, etc)
    write_failures: list[Path] = field(default_factory=list)
    binary_failures: list[Path] = field(default_factory=list)  # non-zero exits
    albums_scanned: int = 0

    @property
    def work_done(self) -> bool:
        return bool(self.scanned or self.tagged or self.markers_written or self.skip_tagged)


class AbortChecker(Protocol):
    """Anything that raises when the run should stop (SIGTERM handling)."""

    def check(self) -> None: ...


@dataclass
class StepContext:
    config: Config
    tools: dict[str, str]
    steps: list[Step] = field(default_factory=list)
    heartbeat: Heartbeat | None = None
    abort: AbortChecker | None = None

    def check_abort(self) -> None:
        if self.abort is not None:
            self.abort.check()


def _refresh(snap: TagSnapshot) -> None:
    """Re-read a file's tags in place after subprocesses/writes touched it."""
    fresh = read_snapshot(snap.path)
    snap.readable = fresh.readable
    snap.values = fresh.values
    snap.artist = fresh.artist
    snap.album = fresh.album
    snap.track_gain = fresh.track_gain
    snap.track_peak = fresh.track_peak
    snap.duration = fresh.duration
    snap.channels = fresh.channels


def _try_write(snap: TagSnapshot, name: str, value: str, result: StepResult) -> None:
    try:
        changed = write_tag(snap.path, name, value)
    except TagWriteError as exc:
        log.warning("tag write failed (%s) on %s: %s", name, snap.path, exc)
        result.write_failures.append(snap.path)
        return
    if changed:
        _refresh(snap)


class Step:
    """Base class tying the shared skip-model bookkeeping together."""

    name = "step"
    output_tag: str = ""
    skip_tag: str = ""

    def run_album(self, album: Album, ctx: StepContext) -> StepResult:
        raise NotImplementedError


class ReplayGainStep(Step):
    name = "replaygain"
    output_tag = T_RGTOOL
    skip_tag = T_RGSKIP

    def needs_scan(self, album: Album, ctx: StepContext) -> list[TagSnapshot]:
        """Files whose RGTOOL marker is missing, unknown, or stale.

        Unreadable files count as needing a scan: their markers cannot be
        verified, and rsgain decodes independently of mutagen, so it may well
        tag them (the marker write afterwards is attempted and may fail —
        reported, retried next run).
        """
        fp = ctx.config.rg_fingerprint
        return [
            snap
            for snap in album.files
            if snap.skipped is None and (not snap.readable or snap.get(T_RGTOOL) != fp)
        ]

    def run_album(self, album: Album, ctx: StepContext) -> StepResult:
        result = StepResult(name=self.name)
        result.skipped_other.extend(
            list(map(lambda f: f.path, filter(lambda f: f.skipped is not None, album.files)))
        )

        candidates = self.needs_scan(album, ctx)
        if not candidates:
            result.already_done = len(album.files)
            return result

        files = sorted(album.audio_paths())
        result.albums_scanned = 1
        result.scanned = len(files)
        log.info("rsgain: scanning album %s (%d files)", album.path.name, len(files))
        try:
            proc = run_rsgain_album(
                ctx.tools["rsgain"], files, ctx.config, heartbeat=ctx.heartbeat
            )
        except ToolError as exc:
            log.error("rsgain failed to start: %s", exc)
            result.binary_failures.extend(files)
            return result

        # rsgain wrote tags directly; refresh every file's view before deciding
        # what still needs a marker or a skip tag.
        for snap in album.files:
            _refresh(snap)

        fp = ctx.config.rg_fingerprint
        if proc.timed_out:
            log.warning("rsgain timed out on %s", album.path)
            result.binary_failures.extend(files)
        elif proc.returncode != 0:
            log.warning(
                "rsgain exited %d on %s", proc.returncode, album.path.name
            )
            result.binary_failures.extend(files)

        if proc.timed_out or proc.returncode != 0:
            for snap in candidates:
                _try_write(snap, T_RGSKIP, f"rsgain exit {proc.returncode}", result)
                if snap.path not in result.skip_tagged and snap.get(T_RGSKIP):
                    result.skip_tagged.append(snap.path)
            return result

        # Success: ensure every file carries the fingerprint marker. rsgain
        # writes RGTOOL itself for everything it could tag; this pass patches
        # the remainder (and is the spec's explicit "write the marker" act).
        for snap in candidates:
            if snap.readable and snap.get(T_RGTOOL) == fp:
                continue
            _try_write(snap, T_RGTOOL, fp, result)
            if snap.path not in result.skip_tagged and snap.get(T_RGTOOL) == fp:
                result.markers_written += 1
        return result


class _PerFileStep(Step):
    """Shared machinery for BPM/key: uniform three-condition check per file."""

    binary_key: str = ""

    def analyze_file(self, path: Path, ctx: StepContext):
        raise NotImplementedError

    def value_from(self, proc: ProcResult) -> str | None:
        raise NotImplementedError

    def failure_reason(self, proc: ProcResult) -> str:
        if proc.timed_out:
            return f"{self.binary_key} timeout"
        return f"{self.binary_key} exit {proc.returncode}"

    def run_album(self, album: Album, ctx: StepContext) -> StepResult:
        result = StepResult(name=self.name)
        for snap in album.files:
            if not snap.readable:
                result.unreadable += 1
                continue
            if snap.skipped is not None:
                result.skipped_other.append(snap.path)
                continue
            if snap.get(self.output_tag):
                result.already_done += 1
                continue
            if snap.get(self.skip_tag):
                result.skip_tag_seen += 1
                continue
            result.scanned += 1
            try:
                proc = self.analyze_file(snap.path, ctx)
            except ToolError as exc:
                log.error("%s failed to start: %s", self.binary_key, exc)
                result.binary_failures.append(snap.path)
                continue
            value = self.value_from(proc) if proc.returncode == 0 and not proc.timed_out else None
            if value is not None:
                log.info("%s: %s = %s", self.name, snap.path.name[:50], value)
                _try_write(snap, self.output_tag, value, result)
                if snap.get(self.output_tag) == value:
                    result.tagged += 1
            else:
                log.warning(
                    "%s failed on %s (%s)",
                    self.name,
                    snap.path.name[:50],
                    self.failure_reason(proc),
                )
                result.binary_failures.append(snap.path)
                _try_write(snap, self.skip_tag, self.failure_reason(proc), result)
                if snap.path not in result.skip_tagged and snap.get(self.skip_tag):
                    result.skip_tagged.append(snap.path)
        return result


class BpmStep(_PerFileStep):
    name = "bpm"
    output_tag = T_BPM
    skip_tag = T_BPMSKIP
    binary_key = "aubio"

    def analyze_file(self, path: Path, ctx: StepContext):
        return run_aubio(ctx.tools["aubio"], path, ctx.config, heartbeat=ctx.heartbeat)

    def value_from(self, proc: ProcResult) -> str | None:
        bpm = parse_bpm(proc.stdout)
        if bpm is None:
            return None
        return str(int(round(bpm)))


class KeyStep(_PerFileStep):
    name = "key"
    output_tag = T_KEY
    skip_tag = T_KEYSKIP
    binary_key = "keyfinder-cli"

    def analyze_file(self, path: Path, ctx: StepContext):
        return run_keyfinder(
            ctx.tools["keyfinder-cli"], path, ctx.config, heartbeat=ctx.heartbeat
        )

    def value_from(self, proc: ProcResult) -> str | None:
        return parse_key(proc.stdout)


def default_steps() -> list[Step]:
    return [ReplayGainStep(), BpmStep(), KeyStep()]
