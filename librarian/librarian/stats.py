"""Library statistics computed from the walk's tag snapshots.

Pure functions throughout: everything here is unit-testable with synthetic
snapshot lists, no audio needed.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field

from .tags import TagSnapshot

# Spotify normalizes to -14 LUFS; the library target is -18 LUFS (RG 2.0).
SPOTIFY_LUFS = -14.0


@dataclass
class LibraryStats:
    albums: int = 0
    files: int = 0
    artists: set[str] = field(default_factory=set)
    unreadable: int = 0
    loudness_values: list[float] = field(default_factory=list)
    gain_peak_pairs: list[tuple[float, float]] = field(default_factory=list)
    bpm_values: list[float] = field(default_factory=list)
    key_counts: Counter[str] = field(default_factory=Counter)
    rg_tagged: int = 0
    bpm_tagged: int = 0
    key_tagged: int = 0

    @property
    def avg_loudness(self) -> float | None:
        if not self.loudness_values:
            return None
        return statistics.fmean(self.loudness_values)

    @property
    def median_bpm(self) -> float | None:
        if not self.bpm_values:
            return None
        return statistics.median(self.bpm_values)

    @property
    def clip_adjustments(self) -> int:
        """Tracks whose written gain sits at the clipping-protection cap.

        With clip mode 'p', rsgain caps positive gains at -20·log10(peak) so
        the boosted peak just touches full scale. A written gain at exactly
        that cap means protection actually reduced the gain.
        """
        return sum(1 for gain, peak in self.gain_peak_pairs if is_clip_adjusted(gain, peak))


def is_clip_adjusted(gain_db: float | None, peak: float | None) -> bool:
    """True when the written gain sits at the clipping-protection cap.

    With clip mode 'p', rsgain caps gains at -20·log10(peak) so the boosted
    peak just touches full scale. A written gain at exactly that cap means
    protection actually determined the value (positive gains capped down,
    or >1.0 true peaks forcing a negative cap).
    """
    if gain_db is None or peak is None or peak <= 0:
        return False
    cap = -20.0 * math.log10(peak)
    return abs(gain_db - cap) <= 0.01


def loudness_from_gain(gain_db: float | None, target_lufs: float = -18.0) -> float | None:
    """Measured loudness implied by an RG2.0 gain: loudness = target - gain."""
    if gain_db is None:
        return None
    return target_lufs - gain_db


def spotify_delta(avg_loudness: float | None) -> float | None:
    """Positive = library is quieter than Spotify's -14 LUFS normalization."""
    if avg_loudness is None:
        return None
    return SPOTIFY_LUFS - avg_loudness


def _parse_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.strip().lower().removesuffix("db"))
    except ValueError:
        return None


def snapshot_to_stats(snaps: list[TagSnapshot], target_lufs: float = -18.0) -> LibraryStats:
    stats = LibraryStats(files=len(snaps))
    for snap in snaps:
        if not snap.readable:
            stats.unreadable += 1
            continue
        if snap.artist:
            stats.artists.add(snap.artist)
        gain = snap.track_gain
        peak = snap.track_peak
        if gain is not None:
            stats.rg_tagged += 1
            stats.gain_peak_pairs.append((gain, peak if peak is not None else 0.0))
            loud = loudness_from_gain(gain, target_lufs)
            if loud is not None:
                stats.loudness_values.append(loud)
        bpm = _parse_float(snap.get("BPM"))
        if bpm is not None:
            stats.bpm_tagged += 1
            stats.bpm_values.append(bpm)
        key = snap.get("KEY")
        if key:
            stats.key_tagged += 1
            stats.key_counts[key] += 1
    return stats


def format_loudness_line(stats: LibraryStats) -> str:
    avg = stats.avg_loudness
    if avg is None:
        return "Average loudness: no ReplayGain tags found"
    delta = spotify_delta(avg)
    if delta is None:
        return f"Average loudness: {avg:.1f} LUFS ({stats.rg_tagged} files RG-tagged)"
    if delta > 0:
        direction = f"{delta:.1f} dB quieter than"
    elif delta < 0:
        direction = f"{-delta:.1f} dB louder than"
    else:
        direction = "identical to"
    return (
        f"Average loudness: {avg:.1f} LUFS "
        f"({stats.rg_tagged} files RG-tagged; Spotify -14: {direction} Spotify)"
    )


def format_key_distribution(stats: LibraryStats, top: int = 5) -> str:
    if not stats.key_counts:
        return "Keys: none tagged"
    parts = [f"{name} ({count})" for name, count in stats.key_counts.most_common(top)]
    return f"Keys ({stats.key_tagged} tagged): " + ", ".join(parts)
