"""The walk: album grouping, extension filtering, unreadable handling, heartbeat."""

from __future__ import annotations

import contextlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from librarian.walk import Heartbeat, is_audio, walk_library

FIXTURES = Path(__file__).parent / "fixtures"


class TestIsAudio:
    def test_supported(self):
        for ext in ["mp3", "flac", "ogg", "opus", "m4a", "wav", "aiff", "oga", "aif", "mp4"]:
            assert is_audio(Path(f"x.{ext}")), ext

    def test_unsupported(self):
        for ext in ["lrc", "jpg", "txt", "song_ids", "log", "pdf", "MP3.bak"]:
            assert not is_audio(Path(f"x.{ext}")), ext

    def test_case_insensitive(self):
        assert is_audio(Path("X.MP3"))
        assert is_audio(Path("X.FlAc"))


class TestWalk:
    def test_album_grouping_and_order(self, tmp_path):
        # nested: artist/album/file and a standalone album dir
        a1 = tmp_path / "Artist" / "Album One"
        a1.mkdir(parents=True)
        a2 = tmp_path / "solo-album"
        a2.mkdir()
        empty = tmp_path / "Artist" / "Empty"
        empty.mkdir()
        shutil.copy2(FIXTURES / "silence.mp3", a1 / "t1.mp3")
        shutil.copy2(FIXTURES / "silence.ogg", a1 / "t2.ogg")
        shutil.copy2(FIXTURES / "silence.flac", a2 / "t3.flac")
        (a1 / "cover.jpg").write_bytes(b"x")
        (a1 / "lyrics.lrc").write_text("...")
        (a1 / ".song_ids").write_text("1")

        result = walk_library(tmp_path)
        assert [album.path.name for album in result.albums] == ["Album One", "solo-album"]
        album_one = result.albums[0]
        assert [f.name for f in album_one.audio_paths()] == ["t1.mp3", "t2.ogg"]
        assert result.files_total == 3
        assert result.non_audio_seen == 3  # cover.jpg, lyrics.lrc, .song_ids

    def test_unreadable_counted_not_raised(self, tmp_path):
        album = tmp_path / "broken-album"
        album.mkdir()
        (album / "bad.ogg").write_bytes(b"\x00" * 4096)
        result = walk_library(album)
        assert result.files_total == 1
        assert len(result.unreadable) == 1
        assert result.albums[0].files[0].readable is False

    def test_empty_library(self, tmp_path):
        result = walk_library(tmp_path)
        assert result.albums == []
        assert result.files_total == 0

    def test_missing_root_yields_empty(self, tmp_path):
        # os.walk with onerror logs the error instead of raising; either way
        # a missing root produces an empty walk rather than a crash.
        with contextlib.suppress(OSError):
            walk_library(tmp_path / "nope")
        result = walk_library(tmp_path / "nope")
        assert result.albums == []


class TestHeartbeat:
    def test_beats_at_interval(self):
        now = {"t": 0.0}

        def clock():
            return now["t"]

        hb = Heartbeat(interval=60.0, clock=clock, label="test")
        hb.beat("a")  # t=0: interval not elapsed yet
        assert hb.beats == 0
        now["t"] = 30.0
        hb.beat("b")  # still too soon
        assert hb.beats == 0
        now["t"] = 61.0
        hb.beat("c")
        assert hb.beats == 1
        now["t"] = 100.0  # 39s since the last beat
        hb.beat("d")
        assert hb.beats == 1
        now["t"] = 122.0
        hb.beat("e")
        assert hb.beats == 2

    def test_beats_immediately_when_interval_elapsed(self):
        now = {"t": 100.0}

        def clock():
            return now["t"]

        hb = Heartbeat(interval=60.0, clock=clock, label="walk")
        hb.beat("first")
        assert hb.beats == 0
        now["t"] = 160.0
        hb.beat("second")
        assert hb.beats == 1
