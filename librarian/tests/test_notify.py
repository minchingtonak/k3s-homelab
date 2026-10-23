"""Notification formatting, payloads, and the outcome decision matrix."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from librarian.cli import Outcome, decide_outcome
from librarian.config import Config
from librarian.notify import (
    MAX_MESSAGE_CHARS,
    MAX_TITLE_CHARS,
    Notifier,
    Push,
    build_payload,
    chunk_pushes,
    format_duration,
    format_progress_message,
    format_skipped_notice,
    format_stats_message,
    headlamp_logs_url,
)
from librarian.stats import LibraryStats, snapshot_to_stats
from librarian.tags import TagSnapshot


def snap(**kw):
    path = kw.pop("path", Path("/music/a/b.mp3"))
    values = kw.pop("values", {})
    return TagSnapshot(path=path, values=values, **kw)


class TestDecideOutcome:
    def test_matrix(self):
        assert decide_outcome(False, False, False) == Outcome(0, "silence")
        assert decide_outcome(True, False, False) == Outcome(0, "stats")
        assert decide_outcome(True, True, False) == Outcome(0, "progress")
        assert decide_outcome(False, True, False) == Outcome(1, "failure")
        assert decide_outcome(True, False, True) == Outcome(1, "failure")
        assert decide_outcome(False, False, True) == Outcome(1, "failure")
        assert decide_outcome(True, True, True) == Outcome(1, "failure")


class TestStatsFromSnapshots:
    def test_library_facts(self):
        snaps = [
            snap(track_gain=-10.0, track_peak=1.0, values={"BPM": "120", "KEY": "Am"}),
            snap(track_gain=-14.0, track_peak=0.5, values={"BPM": "140", "KEY": "C"}),
            snap(track_gain=-18.0, track_peak=0.9, values={"BPM": "132", "KEY": "Am"}),
            snap(readable=False),
        ]
        stats = snapshot_to_stats(snaps)
        assert stats.files == 4
        assert stats.unreadable == 1
        assert stats.rg_tagged == 3
        assert stats.avg_loudness == (-8.0 + -4.0 + 0.0) / 3  # -18 - gain
        assert stats.median_bpm == 132
        assert stats.key_counts == {"Am": 2, "C": 1}

    def test_empty(self):
        stats = snapshot_to_stats([])
        assert stats.avg_loudness is None
        assert stats.median_bpm is None


class TestLoudnessFormatting:
    def test_quieter_than_spotify(self):
        stats = LibraryStats(loudness_values=[-18.0, -18.0], rg_tagged=2)
        line = format_stats_message_stats_line(stats)
        assert "-18.0 LUFS" in line
        assert "quieter" in line

    def test_no_tags(self):
        from librarian.stats import format_loudness_line

        line = format_loudness_line(LibraryStats())
        assert "no ReplayGain" in line


def format_stats_message_stats_line(stats):
    from librarian.stats import format_loudness_line

    return format_loudness_line(stats)


class TestMessages:
    def test_stats_message_shape(self):
        stats = snapshot_to_stats(
            [snap(track_gain=-10.0, track_peak=1.0, values={"BPM": "120", "KEY": "Am"})]
        )
        stats.albums = 3
        msg = format_stats_message(
            stats,
            ["ReplayGain: 1 album scanned (1 file)", "BPM: 1 tagged", "Key: 1 tagged"],
            elapsed_s=3725,
            logs_link="http://h/#/n/x/pod/y/logs",
        )
        assert "3 albums" in msg
        assert "ReplayGain: 1 album scanned" in msg
        assert "Median BPM: 120" in msg
        assert "Elapsed: 1h 2m" in msg
        assert "Pod logs: http://h" in msg

    def test_skipped_notice(self):
        notice = format_skipped_notice(["/m/a.ogg"], ["/m/b.ogg"], ["/m/c.ogg"])
        assert notice is not None
        assert "un-analyzable: /m/a.ogg" in notice
        assert "write failed: /m/b.ogg" in notice
        assert "skipped: /m/c.ogg" in notice

    def test_skipped_notice_empty(self):
        assert format_skipped_notice([], [], []) is None

    def test_skipped_notice_cap(self):
        notice = format_skipped_notice(
            [f"/m/f{i}.mp3" for i in range(60)], [], [f"/m/s{i}.mp3" for i in range(20)]
        )
        assert notice is not None
        assert "and 30 more" in notice

    def test_progress(self):
        msg = format_progress_message(50, 43200)
        assert "50 files processed" in msg
        assert "12h 0m" in msg

class TestChunkPushes:
    def test_short_message_unchanged(self):
        pushes = chunk_pushes("title", "short", 0)
        assert pushes == [Push(title="title", message="short", priority=0)]

    def test_long_message_is_chunked(self):
        message = "x" * (MAX_MESSAGE_CHARS * 2 + 100)
        pushes = chunk_pushes("title", message, 1)
        assert len(pushes) == 3
        assert [p.title for p in pushes] == ["title[1/3]", "title[2/3]", "title[3/3]"]
        assert all(len(p.message) <= MAX_MESSAGE_CHARS for p in pushes)
        assert "".join(p.message for p in pushes) == message
        assert all(p.priority == 1 for p in pushes)

    def test_chunk_titles_never_exceed_max(self):
        title = "t" * (MAX_TITLE_CHARS - 2)
        message = "x" * (MAX_MESSAGE_CHARS * 2 + 100)
        pushes = chunk_pushes(title, message, 0)
        assert len(pushes) == 3
        for idx, p in enumerate(pushes):
            assert len(p.title) <= MAX_TITLE_CHARS
            assert p.title.endswith(f"[{idx + 1}/3]")


class TestDuration:
    def test_formats(self):
        assert format_duration(45) == "45s"
        assert format_duration(125) == "2m 5s"
        assert format_duration(3725) == "1h 2m"


class TestHeadlampLink:
    def test_shape(self):
        url = headlamp_logs_url("https://headlamp.example.com/", "media", "pod-123")
        assert url == (
            "https://headlamp.example.com/#/namespace/media/pod/pod-123/logs?container=librarian"
        )


def configured_notifier(env_extra=None):
    env = {"PUSHOVER_TOKEN": "tok", "PUSHOVER_USER_KEY": "usr"}
    if env_extra:
        env.update(env_extra)
    return Notifier(config=Config.from_env(env))


class TestNotifier:
    def test_silent_when_unconfigured(self):
        cfg = Config.from_env({})
        n = Notifier(config=cfg)
        n.push("t", "m")
        assert len(n.sent) == 1
        assert n.sent[0].priority == 0

    def test_long_message_recorded_as_chunks(self):
        n = Notifier(config=Config.from_env({}))
        n.push("t", "x" * (MAX_MESSAGE_CHARS * 2 + 100))
        assert len(n.sent) == 3

    def test_payload(self):
        n = configured_notifier()
        n.push("title", "msg", priority=1)
        payload = build_payload(n.config, n.sent[0])
        assert payload == {
            "token": "tok",
            "user": "usr",
            "title": "title",
            "message": "msg",
            "priority": "1",
        }

    def test_send_failure_does_not_raise(self, monkeypatch):
        n = configured_notifier()

        def boom(payload):
            raise OSError("network down")

        monkeypatch.setattr("librarian.notify.post_form", boom)
        n.push("t", "m")  # must not raise

    def test_http_request_verified(self, monkeypatch):
        """Spec: verify the HTTP request payload without requiring delivery."""
        n = configured_notifier()
        captured = {}
        import urllib.parse

        def fake_post(payload):
            captured["form"] = urllib.parse.urlencode(payload)
            return b"{}"

        monkeypatch.setattr("librarian.notify.post_form", fake_post)
        n.push("Librarian: run complete", "line1\nline2", priority=0)
        assert "token=tok" in captured["form"]
        assert "priority=0" in captured["form"]
        assert "title=Librarian%3A+run+complete" in captured["form"]
