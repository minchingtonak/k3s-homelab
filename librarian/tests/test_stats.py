"""Pure stats functions: loudness, clipping, durations, distributions."""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from librarian.stats import (
    format_key_distribution,
    format_loudness_line,
    is_clip_adjusted,
    loudness_from_gain,
    snapshot_to_stats,
    spotify_delta,
)
from librarian.tags import TagSnapshot


def test_loudness_from_gain():
    assert loudness_from_gain(-13.36) == -18.0 + 13.36  # -4.64 LUFS
    assert loudness_from_gain(0.0) == -18.0
    assert loudness_from_gain(None) is None


def test_spotify_delta():
    assert spotify_delta(-18.0) == 4.0  # library 4 dB quieter than Spotify
    assert spotify_delta(-14.0) == 0.0
    assert spotify_delta(-10.0) == -4.0
    assert spotify_delta(None) is None


def test_is_clip_adjusted():
    # gain == -20log10(peak) exactly: protection cap applied
    peak = 1.5
    cap = -20 * math.log10(peak)
    assert is_clip_adjusted(cap, peak) is True
    # positive gain well below the cap: no adjustment
    assert is_clip_adjusted(2.0, peak) is False
    # negative gain never clip-adjusted
    assert is_clip_adjusted(-1.0, 0.5) is False
    # nonsense inputs
    assert is_clip_adjusted(None, 0.5) is False
    assert is_clip_adjusted(1.0, None) is False
    assert is_clip_adjusted(1.0, 0.0) is False


def test_format_loudness_quieter():
    stats = snapshot_to_stats(
        [TagSnapshot(path=Path("a"), values={}, track_gain=0.0)]
    )
    line = format_loudness_line(stats)
    assert "-18.0 LUFS" in line and "quieter" in line


def test_format_loudness_louder():
    stats = snapshot_to_stats(
        [TagSnapshot(path=Path("a"), values={}, track_gain=-9.0)]
    )
    line = format_loudness_line(stats)
    assert "-9.0 LUFS" in line and "louder" in line


def test_format_key_distribution():
    stats = snapshot_to_stats(
        [
            TagSnapshot(path=Path("a"), values={"KEY": "Am"}),
            TagSnapshot(path=Path("b"), values={"KEY": "Am"}),
            TagSnapshot(path=Path("c"), values={"KEY": "C"}),
        ]
    )
    line = format_key_distribution(stats)
    assert "Am (2)" in line and "C (1)" in line
    assert format_key_distribution(snapshot_to_stats([])) == "Keys: none tagged"


def test_clip_adjustment_counted_in_stats():
    # Two tracks: one at the protection cap, one plain negative gain.
    cap = -20 * math.log10(1.4)
    stats = snapshot_to_stats(
        [
            TagSnapshot(path=Path("a"), values={}, track_gain=cap, track_peak=1.4),
            TagSnapshot(path=Path("b"), values={}, track_gain=-9.0, track_peak=0.9),
        ]
    )
    assert stats.clip_adjustments == 1
