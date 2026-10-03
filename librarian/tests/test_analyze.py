"""Parsers for analysis-binary output + subprocess polling behavior."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from librarian.analyze import parse_bpm, parse_key, rsgain_command, run_polling


class TestParseBpm:
    def test_plain(self):
        assert parse_bpm("107.55 bpm\n") == 107.55

    def test_last_match_wins(self):
        # aubio can print interim estimates; the final one is authoritative
        assert parse_bpm("90.00 bpm\n95.00 bpm\n124.30 bpm\n") == 124.30

    def test_noisy(self):
        assert parse_bpm("[mp3float @ 0x1] warnings\n101.66 bpm\n") == 101.66

    def test_unparseable(self):
        assert parse_bpm("no numbers here") is None
        assert parse_bpm("") is None

    def test_degenerate_rejected(self):
        assert parse_bpm("0 bpm") is None
        assert parse_bpm("5000 bpm") is None

    def test_case_insensitive_unit(self):
        assert parse_bpm("128 BPM") == 128


class TestParseKey:
    def test_plain(self):
        assert parse_key("Gbm\n") == "Gbm"

    def test_major_no_suffix(self):
        assert parse_key("D\n") == "D"

    def test_all_tonics(self):
        for key in ["A", "Am", "Bb", "Bbm", "Ab", "Abm", "Gb", "Gbm"]:
            assert parse_key(f"{key}\n") == key

    def test_invalid(self):
        assert parse_key("???") is None
        assert parse_key("") is None
        assert parse_key("not a key") is None


class TestRsgainCommand:
    def test_flags(self):
        cmd = rsgain_command("rsgain")
        assert cmd[0] == "custom"
        for flag in ("-a", "-t", "-l", "-18", "-c", "p", "-s", "i"):
            assert flag in cmd


class TestRunPolling:
    def test_success(self):
        res = run_polling([sys.executable, "-c", "print('hi')"], timeout=10)
        assert res.returncode == 0
        assert res.stdout.strip() == "hi"
        assert not res.timed_out

    def test_nonzero_exit(self):
        res = run_polling([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=10)
        assert res.returncode == 3

    def test_timeout(self):
        res = run_polling([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
        assert res.timed_out

    def test_missing_binary(self):
        import pytest

        from librarian.analyze import ToolError

        with pytest.raises(ToolError):
            run_polling(["/nonexistent/binary-xyz"], timeout=5)

    def test_unicode_args_pass_through(self, tmp_path):
        # A unicode filename as argv must round-trip into the child untouched.
        script = tmp_path / "echoarg.py"
        script.write_text("import sys; print(sys.argv[1])")
        name = "神前暁_「囮物語」劇伴音楽集&あとがたり_01_千石撫子、十四歳。.flac"
        res = run_polling([sys.executable, str(script), name], timeout=10)
        assert res.stdout.strip() == name

    def test_heartbeat_fires(self):
        beats = []

        class FakeHeartbeat:
            def beat(self, detail):
                beats.append(detail)

        run_polling(
            [sys.executable, "-c", "import time; time.sleep(1.2)"],
            timeout=10,
            heartbeat=FakeHeartbeat(),
            poll_interval=0.3,
        )
        assert len(beats) >= 2  # polled with heartbeats during the wait
