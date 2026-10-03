"""The uniform three-condition skip model and per-step behaviors."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from librarian.analyze import ProcResult
from librarian.config import Config
from librarian.steps import (
    BpmStep,
    KeyStep,
    ReplayGainStep,
    StepContext,
    default_steps,
)
from librarian.tags import (
    T_BPM,
    T_BPMSKIP,
    T_KEY,
    T_KEYSKIP,
    T_RGSKIP,
    T_RGTOOL,
    read_snapshot,
    write_tag,
)
from librarian.walk import Album

FP = "rsgain-3.8/tp/-18"


def make_ctx(tmp_path, tools=None):
    config = Config(library_dir=tmp_path, rg_fingerprint=FP)
    return StepContext(
        config=config,
        tools=tools or {"rsgain": "rsgain", "aubio": "aubio", "keyfinder-cli": "keyfinder-cli"},
        steps=[],
    )


def album_with(path: Path) -> Album:
    snap = read_snapshot(path)
    return Album(path=path.parent, files=[snap])


def fake_proc(stdout="", returncode=0, argv=None, timed_out=False):
    return ProcResult(
        argv=argv or ["x"], returncode=returncode, stdout=stdout, stderr="", duration=0.1,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------- BPM step


def test_bpm_writes_tag_on_success(mp3, monkeypatch):
    monkeypatch.setattr(
        "librarian.steps.run_aubio", lambda *a, **k: fake_proc("107.55 bpm\n")
    )
    ctx = make_ctx(mp3.parent.parent)
    res = BpmStep().run_album(album_with(mp3), ctx)
    assert res.tagged == 1
    assert read_snapshot(mp3).get(T_BPM) == "108"  # rounded


def test_bpm_skips_when_tag_present(mp3, monkeypatch):
    write_tag(mp3, T_BPM, "120")
    called = []
    monkeypatch.setattr(
        "librarian.steps.run_aubio",
        lambda *a, **k: called.append(1) or fake_proc("100 bpm"),
    )
    res = BpmStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert res.already_done == 1 and not called


def test_bpm_skips_when_skip_tag_present(mp3, monkeypatch):
    write_tag(mp3, T_BPMSKIP, "aubio exit 1")
    called = []
    monkeypatch.setattr(
        "librarian.steps.run_aubio",
        lambda *a, **k: called.append(1) or fake_proc("100 bpm"),
    )
    res = BpmStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert res.skip_tag_seen == 1 and not called


def test_bpm_nonzero_exit_writes_skip_tag(mp3, monkeypatch):
    monkeypatch.setattr(
        "librarian.steps.run_aubio",
        lambda *a, **k: fake_proc("garbage", returncode=1),
    )
    res = BpmStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert res.skip_tagged == [mp3]
    snap = read_snapshot(mp3)
    assert snap.get(T_BPMSKIP) == "aubio exit 1"
    assert snap.get(T_BPM) is None


def test_bpm_unparseable_output_writes_skip_tag(mp3, monkeypatch):
    monkeypatch.setattr(
        "librarian.steps.run_aubio", lambda *a, **k: fake_proc("", returncode=0)
    )
    res = BpmStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert res.skip_tagged == [mp3]
    reason = read_snapshot(mp3).get(T_BPMSKIP)
    assert reason is not None and "exit 0" in reason  # rc=0, output unparseable


def test_bpm_timeout_writes_skip_tag(mp3, monkeypatch):
    monkeypatch.setattr(
        "librarian.steps.run_aubio",
        lambda *a, **k: fake_proc("", returncode=0, timed_out=True),
    )
    res = BpmStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert res.skip_tagged == [mp3]
    reason = read_snapshot(mp3).get(T_BPMSKIP)
    assert reason is not None and "timeout" in reason


def test_bpm_skips_unreadable_file(tmp_path, monkeypatch):
    bad = tmp_path / "broken.mp3"
    bad.write_bytes(b"\x00" * 2048)
    called = []
    monkeypatch.setattr(
        "librarian.steps.run_aubio",
        lambda *a, **k: called.append(1) or fake_proc("100 bpm"),
    )
    res = BpmStep().run_album(album_with(bad), make_ctx(tmp_path))
    assert res.unreadable == 1 and not called


# ---------------------------------------------------------------- Key step


def test_key_writes_tag_on_success(mp3, monkeypatch):
    monkeypatch.setattr(
        "librarian.steps.run_keyfinder", lambda *a, **k: fake_proc("Gbm\n")
    )
    res = KeyStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert res.tagged == 1
    assert read_snapshot(mp3).get(T_KEY) == "Gbm"


def test_key_invalid_output_writes_skip_tag(mp3, monkeypatch):
    monkeypatch.setattr(
        "librarian.steps.run_keyfinder", lambda *a, **k: fake_proc("???\n")
    )
    res = KeyStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert res.skip_tagged == [mp3]
    assert read_snapshot(mp3).get(T_KEYSKIP) is not None


# ---------------------------------------------------------- ReplayGain step


def test_rg_skips_when_marker_matches(mp3, monkeypatch):
    write_tag(mp3, T_RGTOOL, FP)
    called = []
    monkeypatch.setattr(
        "librarian.steps.run_rsgain_album",
        lambda *a, **k: called.append(1) or fake_proc("done"),
    )
    album = album_with(mp3)
    res = ReplayGainStep().run_album(album, make_ctx(mp3.parent.parent))
    assert res.already_done == 1 and not called


def test_rg_runs_when_marker_missing_and_stamps_it(mp3, monkeypatch):
    seen = {}

    def fake_rsgain(_bin, files, _cfg, heartbeat=None):
        seen["files"] = list(files)
        # rsgain writes RGTOOL itself on success
        for f in files:
            write_tag(Path(f), T_RGTOOL, FP)
        return fake_proc("ok")

    monkeypatch.setattr("librarian.steps.run_rsgain_album", fake_rsgain)
    album = album_with(mp3)
    res = ReplayGainStep().run_album(album, make_ctx(mp3.parent.parent))
    assert seen["files"] == [mp3]
    assert res.scanned == 1
    assert read_snapshot(mp3).get(T_RGTOOL) == FP


def test_rg_patches_marker_when_rsgain_did_not_write_it(mp3, monkeypatch):
    monkeypatch.setattr(
        "librarian.steps.run_rsgain_album", lambda *a, **k: fake_proc("ok")
    )
    res = ReplayGainStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert read_snapshot(mp3).get(T_RGTOOL) == FP
    assert res.markers_written == 1


def test_rg_failure_writes_skip_tags(mp3, monkeypatch):
    monkeypatch.setattr(
        "librarian.steps.run_rsgain_album",
        lambda *a, **k: fake_proc("boom", returncode=1),
    )
    res = ReplayGainStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert res.skip_tagged == [mp3]
    assert read_snapshot(mp3).get(T_RGSKIP) == "rsgain exit 1"


def test_rg_fingerprint_mismatch_queues_rescan(mp3, monkeypatch):
    write_tag(mp3, T_RGTOOL, "rsgain-3.5/sp/-18")  # stale settings
    called = []
    monkeypatch.setattr(
        "librarian.steps.run_rsgain_album",
        lambda *a, **k: called.append(1) or fake_proc("ok"),
    )
    ReplayGainStep().run_album(album_with(mp3), make_ctx(mp3.parent.parent))
    assert called, "stale fingerprint must trigger a re-scan"


def test_rg_unreadable_files_still_scanned_by_rsgain(tmp_path, monkeypatch):
    bad = tmp_path / "broken.ogg"
    bad.write_bytes(b"\x00" * 2048)
    seen = {}

    def fake_rsgain(_bin, files, _cfg, heartbeat=None):
        seen["files"] = list(files)
        return fake_proc("ok")

    monkeypatch.setattr("librarian.steps.run_rsgain_album", fake_rsgain)
    album = album_with(bad)
    res = ReplayGainStep().run_album(album, make_ctx(tmp_path))
    assert seen["files"] == [bad]  # handed to rsgain even though mutagen can't read it
    # marker write will fail on an unreadable file: reported, not fatal
    assert res.write_failures == [bad]


def test_rg_album_level_check_triggers_on_one_stale_file(tmp_path, monkeypatch):
    import shutil

    src = Path(__file__).parent / "fixtures" / "silence.ogg"
    a, b = tmp_path / "a.ogg", tmp_path / "b.ogg"
    shutil.copy2(src, a)
    shutil.copy2(src, b)
    write_tag(a, T_RGTOOL, FP)
    # b has no marker: the whole album must be scanned
    scanned = []

    def fake_rsgain(_bin, files, _cfg, heartbeat=None):
        scanned.extend(files)
        for f in files:
            write_tag(Path(f), T_RGTOOL, FP)
        return fake_proc("ok")

    monkeypatch.setattr("librarian.steps.run_rsgain_album", fake_rsgain)
    from librarian.walk import walk_library

    walk = walk_library(tmp_path)
    ReplayGainStep().run_album(walk.albums[0], make_ctx(tmp_path))
    assert sorted(scanned) == sorted([a, b])


# ------------------------------------------------------------- step model


def test_default_step_order():
    names = [s.name for s in default_steps()]
    assert names == ["replaygain", "bpm", "key"]


def test_step_independence_rg_survives_bpm_crash(mp3, monkeypatch):
    """RG output persists even when the BPM binary crashes on the same file."""
    write_tag(mp3, T_RGTOOL, FP)
    monkeypatch.setattr(
        "librarian.steps.run_aubio",
        lambda *a, **k: fake_proc("x", returncode=2),
    )
    ctx = make_ctx(mp3.parent.parent)
    album = album_with(mp3)
    rg = ReplayGainStep().run_album(album, ctx)
    bpm = BpmStep().run_album(album, ctx)
    assert rg.already_done == 1  # untouched
    assert bpm.skip_tagged == [mp3]  # failed independently
    assert read_snapshot(mp3).get(T_RGTOOL) == FP  # persists
