"""Shared fixtures: tiny real audio files (0.3s silence) per tag family."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _copy(tmp_path: Path, name: str, new_name: str | None = None) -> Path:
    src = FIXTURES / name
    dest = tmp_path / (new_name or name)
    shutil.copy2(src, dest)
    return dest


@pytest.fixture
def mp3(tmp_path):
    return _copy(tmp_path, "silence.mp3", "track.mp3")


@pytest.fixture
def ogg(tmp_path):
    return _copy(tmp_path, "silence.ogg", "track.ogg")


@pytest.fixture
def flac(tmp_path):
    return _copy(tmp_path, "silence.flac", "track.flac")


@pytest.fixture
def m4a(tmp_path):
    return _copy(tmp_path, "silence.m4a", "track.m4a")


@pytest.fixture
def all_formats(mp3, ogg, flac, m4a):
    return {"mp3": mp3, "ogg": ogg, "flac": flac, "m4a": m4a}
