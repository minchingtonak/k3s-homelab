"""The library walk: one filesystem pass, tags read once, albums grouped.

An *album* is any directory that contains audio files directly (the library
is laid out as Artist/Album/track.ext, so the file's immediate parent is the
album). Directories with no direct audio files (artist folders, art folders)
are not albums.

The walk emits heartbeat log lines every ``heartbeat_interval`` seconds while
it runs, per the spec's observability requirements.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import AUDIO_EXTENSIONS
from .tags import TagSnapshot, read_snapshot

log = logging.getLogger("librarian.walk")


@dataclass
class Album:
    path: Path
    files: list[TagSnapshot] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    def audio_paths(self) -> list[Path]:
        return [snap.path for snap in self.files]


@dataclass
class WalkResult:
    root: Path
    albums: list[Album]
    files_total: int
    unreadable: list[Path]
    non_audio_seen: int = 0
    elapsed: float = 0.0


class Heartbeat:
    """Log a progress line at most once per interval during silent stretches."""

    def __init__(self, interval: float, clock=time.monotonic, label="working"):
        self.interval = max(interval, 0.001)
        self.clock = clock
        self.label = label
        self._last = clock()
        self._beats = 0

    def beat(self, detail: str) -> None:
        now = self.clock()
        if now - self._last >= self.interval:
            self._last = now
            self._beats += 1
            log.info("heartbeat[%d] %s: %s", self._beats, self.label, detail)

    @property
    def beats(self) -> int:
        return self._beats


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def walk_library(
    root: Path,
    heartbeat: Heartbeat | None = None,
    abort_check=None,
) -> WalkResult:
    """Recursively scan ``root`` and read every audio file's tags once.

    ``abort_check`` is an optional callable polled between files; raising from
    it aborts the walk (used for SIGTERM responsiveness).
    """
    started = time.monotonic()
    albums_by_dir: dict[Path, Album] = {}
    unreadable: list[Path] = []
    non_audio = 0
    files_total = 0

    def on_error(err: OSError) -> None:
        log.warning("walk error at %s: %s", getattr(err, "filename", "?"), err)

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=False):
        here = Path(dirpath)
        dirnames.sort()
        audio_files = sorted(
            f for f in filenames if Path(f).suffix.lower() in AUDIO_EXTENSIONS
        )
        non_audio += len(filenames) - len(audio_files)
        if not audio_files:
            continue
        album = Album(path=here)
        albums_by_dir[here] = album
        for fname in audio_files:
            if abort_check is not None:
                abort_check()
            path = here / fname
            files_total += 1
            if heartbeat is not None:
                heartbeat.beat(
                    f"{files_total} files in {len(albums_by_dir)} albums"
                    f" (at {path.name[:40]!r})"
                )
            snap = read_snapshot(path)
            if not snap.readable:
                unreadable.append(path)
                log.warning("unreadable tags, skipping file: %s", path)
            album.files.append(snap)

    albums = [albums_by_dir[d] for d in sorted(albums_by_dir)]
    elapsed = time.monotonic() - started
    log.info(
        "walk complete: %d albums, %d audio files, %d unreadable, %d non-audio files, %.1fs",
        len(albums),
        files_total,
        len(unreadable),
        non_audio,
        elapsed,
    )
    return WalkResult(
        root=root,
        albums=albums,
        files_total=files_total,
        unreadable=unreadable,
        non_audio_seen=non_audio,
        elapsed=elapsed,
    )
