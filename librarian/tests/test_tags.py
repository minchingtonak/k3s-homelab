"""Tag read/write across the three families, via the real mutagen formats."""

from __future__ import annotations

from pathlib import Path

import pytest

from librarian.tags import (
    T_BPM,
    T_KEY,
    T_KEYSKIP,
    T_RGTOOL,
    TagWriteError,
    read_snapshot,
    write_tag,
)


def _make_unreadable(tmp_path: Path) -> Path:
    p = tmp_path / "broken.ogg"
    p.write_bytes(b"\x00" * 4096)
    return p


ALL = ["mp3", "ogg", "flac", "m4a"]


@pytest.mark.parametrize("fmt", ALL)
def test_write_and_read_custom_tags(all_formats, fmt):
    path = all_formats[fmt]
    assert write_tag(path, T_RGTOOL, "rsgain-3.8/tp/-18") is True
    assert write_tag(path, T_KEYSKIP, "keyfinder-cli exit 1") is True
    snap = read_snapshot(path)
    assert snap.readable
    assert snap.get(T_RGTOOL) == "rsgain-3.8/tp/-18"
    assert snap.get(T_KEYSKIP) == "keyfinder-cli exit 1"


@pytest.mark.parametrize("fmt", ALL)
def test_idempotent_write_returns_false(all_formats, fmt):
    path = all_formats[fmt]
    write_tag(path, T_RGTOOL, "fp-1")
    assert write_tag(path, T_RGTOOL, "fp-1") is False


@pytest.mark.parametrize("fmt", ALL)
def test_bpm_and_key_roundtrip(all_formats, fmt):
    path = all_formats[fmt]
    write_tag(path, T_BPM, "128")
    write_tag(path, T_KEY, "Gbm")
    snap = read_snapshot(path)
    assert snap.get(T_BPM) == "128"
    assert snap.get(T_KEY) == "Gbm"


def test_bpm_written_as_integer_normalization(m4a):
    # MP4's tmpo atom is an integer; fractional BPM must round.
    write_tag(m4a, T_BPM, "127.6")
    import mutagen

    mf = mutagen.File(str(m4a))
    assert mf.tags["tmpo"] == [128]
    snap = read_snapshot(m4a)
    assert snap.get(T_BPM) == "128"


def test_id3_uses_txxx_tbpm_tkey_frames(mp3):
    from mutagen.id3 import ID3

    write_tag(mp3, T_RGTOOL, "fp")
    write_tag(mp3, T_BPM, "99")
    write_tag(mp3, T_KEY, "Am")
    tags = ID3(str(mp3))
    assert tags.getall("TXXX:RGTOOL")[0].text == ["fp"]
    assert tags.getall("TBPM")[0].text == ["99"]
    assert tags.getall("TKEY")[0].text == ["Am"]


def test_vorbis_case_insensitive_read(ogg):
    # Pre-existing lowercase fields must still be found.
    import mutagen

    mf = mutagen.File(str(ogg))
    assert mf is not None and mf.tags is not None
    mf.tags["rgtool"] = ["rsgain-3.8/tp/-18"]
    mf.tags["bpm"] = ["140"]
    mf.tags["key"] = ["Am"]
    mf.save()
    snap = read_snapshot(ogg)
    assert snap.get(T_RGTOOL) == "rsgain-3.8/tp/-18"
    assert snap.get(T_BPM) == "140"
    assert snap.get(T_KEY) == "Am"


def test_mp4_freeform_with_different_mean_casing(m4a):
    import mutagen

    mf = mutagen.File(str(m4a))
    assert mf is not None and mf.tags is not None
    mf.tags["----:com.apple.itunes:RGTOOL"] = [
        mutagen.mp4.MP4FreeForm(b"fp-old", dataformat=mutagen.mp4.AtomDataType.UTF8)
    ]
    mf.save()
    snap = read_snapshot(m4a)
    assert snap.get(T_RGTOOL) == "fp-old"
    # and a fresh write lands under our canonical mean and is readable back
    assert write_tag(m4a, T_RGTOOL, "fp-new") is True
    assert read_snapshot(m4a).get(T_RGTOOL) == "fp-new"


def test_replaygain_and_artist_extraction(mp3):
    import mutagen

    mf = mutagen.File(str(mp3))
    assert mf is not None and mf.tags is not None
    mf.tags["TPE2"] = mutagen.id3.TPE2(encoding=1, text=["Album Artist"])
    mf.tags["TXXX:REPLAYGAIN_TRACK_GAIN"] = mutagen.id3.TXXX(
        encoding=0, desc="REPLAYGAIN_TRACK_GAIN", text=["-13.36 dB"]
    )
    mf.tags["TXXX:REPLAYGAIN_TRACK_PEAK"] = mutagen.id3.TXXX(
        encoding=0, desc="REPLAYGAIN_TRACK_PEAK", text=["1.050849"]
    )
    mf.save()
    snap = read_snapshot(mp3)
    assert snap.artist == "Album Artist"
    assert snap.track_gain == pytest.approx(-13.36)
    assert snap.track_peak == pytest.approx(1.050849)


def test_vorbis_replaygain_extraction(ogg):
    import mutagen

    mf = mutagen.File(str(ogg))
    assert mf is not None and mf.tags is not None
    mf.tags["replaygain_track_gain"] = ["-13.36 dB"]
    mf.tags["replaygain_track_peak"] = ["1.050849"]
    mf.tags["albumartist"] = ["SPICE"]
    mf.save()
    snap = read_snapshot(ogg)
    assert snap.track_gain == pytest.approx(-13.36)
    assert snap.track_peak == pytest.approx(1.050849)
    assert snap.artist == "SPICE"


def test_unreadable_file_is_not_an_error(tmp_path):
    path = _make_unreadable(tmp_path)
    snap = read_snapshot(path)
    assert snap.readable is False
    assert snap.get(T_RGTOOL) is None


def test_unreadable_file_write_raises(tmp_path):
    path = _make_unreadable(tmp_path)
    with pytest.raises(TagWriteError):
        write_tag(path, T_RGTOOL, "fp")


def test_fresh_flac_without_tags_gets_tags_on_write(flac):
    import mutagen

    mf = mutagen.File(str(flac))
    assert mf is not None
    mf.delete()
    mf = mutagen.File(str(flac))
    assert mf is not None
    assert mf.tags is None
    assert write_tag(flac, T_RGTOOL, "fp") is True
    assert read_snapshot(flac).get(T_RGTOOL) == "fp"
