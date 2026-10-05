"""Run orchestration: walk → pipeline → report, with the notification contract.

The outcome decision is a pure function of three booleans (unit-tested):

    infra_error  window_expired  work_done  -> push                exit
    ---------------------------------------------------------------------
    yes          *               *          failure               1
    no           yes             no         failure               1
    no           yes             yes        progress              0
    no           no              yes        stats (+skipped note) 0
    no           no              no         silence               0
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from time import sleep

from .analyze import ToolError, resolve_all_tools
from .config import Config
from .notify import (
    Notifier,
    format_failure_message,
    format_progress_message,
    format_stats_message,
    format_skipped_notice,
)
from .stats import LibraryStats, snapshot_to_stats
from .steps import StepContext, StepResult, default_steps
from .walk import Album, Heartbeat, WalkResult, walk_library

log = logging.getLogger("librarian")


class AbortRun(Exception):
    """SIGTERM/SIGINT seen; stop promptly and notify best-effort."""


class InfraError(Exception):
    """Infrastructure failure (tool provisioning, unwalkable root)."""


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    notification: str  # "failure" | "progress" | "stats" | "silence"


def decide_outcome(work_done: bool, window_expired: bool, infra_error: bool) -> Outcome:
    if infra_error:
        return Outcome(1, "failure")
    if window_expired and not work_done:
        return Outcome(1, "failure")
    if window_expired:
        return Outcome(0, "progress")
    if work_done:
        return Outcome(0, "stats")
    return Outcome(0, "silence")


class AbortFlag:
    def __init__(self) -> None:
        self.signaled = False
        self.signal_name: str | None = None

    def request_abort(self, name: str) -> None:
        self.signaled = True
        self.signal_name = name

    def check(self) -> None:
        if self.signaled:
            raise AbortRun(self.signal_name or "abort")


class AbortableHeartbeat(Heartbeat):
    """Heartbeat whose beat() also honors an abort flag (subprocess polls)."""

    def __init__(self, flag: AbortFlag, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.flag = flag

    def beat(self, detail: str) -> None:
        self.flag.check()
        super().beat(detail)


def aggregate(results: list[StepResult]) -> dict[str, StepResult]:
    merged: dict[str, StepResult] = {}
    for res in results:
        cur = merged.setdefault(
            res.name,
            StepResult(name=res.name),
        )
        cur.scanned += res.scanned
        cur.tagged += res.tagged
        cur.markers_written += res.markers_written
        cur.already_done += res.already_done
        cur.skip_tagged.extend(res.skip_tagged)
        cur.skip_tag_seen += res.skip_tag_seen
        cur.unreadable += res.unreadable
        cur.skipped_other.extend(res.skipped_other)
        cur.write_failures.extend(res.write_failures)
        cur.binary_failures.extend(res.binary_failures)
        cur.albums_scanned += res.albums_scanned
    return merged


def step_summary(name: str, r: StepResult) -> str:
    if name == "replaygain":
        bits = [
            f"{r.albums_scanned} albums scanned ({r.scanned} files)",
            f"{r.markers_written} markers written",
        ]
        if r.binary_failures:
            bits.append(f"{len(r.binary_failures)} files un-analyzable")
        return "ReplayGain: " + ", ".join(bits)
    bits = [f"{r.tagged} tagged"]
    if r.binary_failures:
        bits.append(f"{len(r.binary_failures)} un-analyzable")
    if r.unreadable:
        bits.append(f"{r.unreadable} unreadable")
    label = "BPM" if name == "bpm" else "Key"
    return f"{label}: " + ", ".join(bits)


def run_pipeline(
    albums: list[Album],
    ctx: StepContext,
    deadline: float,
    clock=time.monotonic,
) -> tuple[dict[str, StepResult], bool]:
    """Apply every step to every album until the scan window expires.

    Returns (per-step aggregate, window_expired).
    """
    results: list[StepResult] = []
    window_expired = False
    for idx, album in enumerate(albums):
        if clock() >= deadline:
            window_expired = True
            log.warning("scan window expired before album %s", album.path.name)
            break
        ctx.check_abort()

        log.info("processing album [%s/%s]", idx + 1, len(albums))
        for step in ctx.steps:
            try:
                res = step.run_album(album, ctx)
            except AbortRun:
                raise
            except Exception:
                # A step crashing must never block the others or fail the run.
                log.exception("step %s crashed on %s", step.name, album.path)
                res = StepResult(name=step.name, write_failures=[album.path])
            results.append(res)
    return aggregate(results), window_expired


def library_stats(walk: WalkResult, target_lufs: float = -18.0) -> LibraryStats:
    snaps = [snap for album in walk.albums for snap in album.files]
    stats = snapshot_to_stats(snaps, target_lufs)
    stats.albums = len(walk.albums)
    return stats


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="librarian",
        description="Walk the music library and apply the metadata pipeline "
        "(ReplayGain, BPM, key) per album.",
    )
    parser.add_argument(
        "library",
        nargs="?",
        default=None,
        help="library root (default: $LIBRARIAN_LIBRARY_DIR or /data/music)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    started = time.monotonic()
    config = Config.from_env()
    if args.library:
        config = replace(config, library_dir=Path(args.library))

    notifier = Notifier(config=config)
    abort = AbortFlag()

    def on_signal(signum, _frame):
        abort.request_abort(signal.Signals(signum).name)
        log.warning("received %s, aborting", signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    exit_code = 0
    try:
        exit_code = _run(config, notifier, abort, started)
    except AbortRun as exc:
        log.error("aborted on %s", exc)
        notifier.push(
            "Librarian: terminated",
            format_failure_message(
                f"terminated by {exc}", time.monotonic() - started, notifier.logs_link()
            ),
            priority=1,
        )
        return 143
    except InfraError as exc:
        log.error("infrastructure failure: %s", exc)
        notifier.push(
            "Librarian: FAILED",
            format_failure_message(str(exc), time.monotonic() - started, notifier.logs_link()),
            priority=1,
        )
        return 1
    return exit_code


def _run(config: Config, notifier: Notifier, abort: AbortFlag, started: float) -> int:
    if not config.library_dir.is_dir():
        raise InfraError(f"library root {config.library_dir} is not a directory")

    log.info("librarian starting: library=%s", config.library_dir)
    try:
        tools = resolve_all_tools(config)
    except ToolError as exc:
        raise InfraError(f"tool provisioning failed: {exc}") from exc
    for name, path in tools.items():
        log.info("tool %s -> %s", name, path)

    heartbeat = AbortableHeartbeat(
        abort, interval=config.heartbeat_interval, label="walk"
    )
    log.info("walk: beginning library walk")
    walk = walk_library(config.library_dir, heartbeat=heartbeat, abort_check=abort.check)
    log.info(
        "walk: %d albums / %d files in %s",
        len(walk.albums),
        walk.files_total,
        time.monotonic() - started,
    )

    pipeline_heartbeat = AbortableHeartbeat(
        abort, interval=config.heartbeat_interval, label="pipeline"
    )
    steps = default_steps()
    ctx = StepContext(
        config=config, tools=tools, steps=steps, heartbeat=pipeline_heartbeat, abort=abort
    )

    deadline = time.monotonic() + config.scan_timeout
    log.info("pipeline: beginning pipeline run")
    results, window_expired = run_pipeline(walk.albums, ctx, deadline)

    # Steps refreshed snapshots in place, so recompute library facts after work.
    refreshed = WalkResult(
        root=walk.root,
        albums=walk.albums,
        files_total=walk.files_total,
        unreadable=walk.unreadable,
        skipped=walk.skipped,
        non_audio_seen=walk.non_audio_seen,
        elapsed=walk.elapsed,
    )
    stats = library_stats(refreshed, target_lufs=-18.0)

    step_results = list(results.values())
    work_done = any(r.work_done for r in step_results)
    elapsed = time.monotonic() - started

    skipped = format_skipped_notice(
        sorted({str(p) for r in step_results for p in r.skip_tagged}),
        sorted({str(p) for r in step_results for p in r.write_failures}),
        sorted({str(p) for r in step_results for p in r.skipped_other})
    )

    outcome = decide_outcome(
        work_done=work_done, window_expired=window_expired, infra_error=False
    )
    if outcome.notification == "failure":
        reason = (
            "scan window expired with zero work completed"
            if window_expired
            else "unknown"
        )
        notifier.push(
            "Librarian: FAILED",
            format_failure_message(reason, elapsed, notifier.logs_link()),
            priority=1,
        )
    elif outcome.notification == "progress":
        processed = sum(r.scanned for r in step_results)
        notifier.push(
            "Librarian: window ended",
            format_progress_message(processed, elapsed),
        )
    elif outcome.notification == "stats":
        summaries = [step_summary(name, r) for name, r in results.items()]
        message = format_stats_message(stats, summaries, elapsed, notifier.logs_link())
        notifier.push("Librarian: run complete", message)
        if skipped:
            sleep(0.5)
            notifier.push("Librarian: skipped files", skipped)

    log.info(
        "done in %.1fs: work_done=%s window_expired=%s exit=%d",
        elapsed,
        work_done,
        window_expired,
        outcome.exit_code,
    )
    return outcome.exit_code
