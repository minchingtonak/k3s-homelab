"""Unified tag access across the three tag families the library uses.

Read/write mapping (name -> per-format location):

  =========  ==========================  ======================  ===========================
  tag        ID3v2 (MP3/WAV/AIFF)        Vorbis (FLAC/OGG/Opus)  MP4 atoms (M4A)
  =========  ==========================  ======================  ===========================
  RGTOOL     TXXX:RGTOOL                 RGTOOL                  ----:com.apple.iTunes:RGTOOL
  BPM        TBPM                        BPM                     tmpo (integer)
  KEY        TKEY                        KEY                     ----:com.apple.iTunes:KEY
  RGSKIP     TXXX:RGSKIP                 RGSKIP                  ----:com.apple.iTunes:RGSKIP
  BPMSKIP    TXXX:BPMSKIP                BPMSKIP                 ----:com.apple.iTunes:BPMSKIP
  KEYSKIP    TXXX:KEYSKIP                KEYSKIP                 ----:com.apple.iTunes:KEYSKIP
  =========  ==========================  ======================  ===========================

All lookups are case-insensitive where the underlying format allows it
(Vorbis field names are case-insensitive; MP4 freeform atom names are matched
case-insensitively on the name segment because the library contains both
``com.apple.iTunes`` and ``com.apple.itunes`` means).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from mutagen.id3 import ID3, TBPM, TKEY, TXXX
from mutagen.mp4 import AtomDataType, MP4FreeForm, MP4Tags

log = logging.getLogger("librarian.tags")

# Canonical tag names used throughout the librarian pipeline.
T_RGTOOL = "RGTOOL"
T_BPM = "BPM"
T_KEY = "KEY"
T_RGSKIP = "RGSKIP"
T_BPMSKIP = "BPMSKIP"
T_KEYSKIP = "KEYSKIP"

MP4_FREEFORM_MEAN = "com.apple.iTunes"

MAXIMUM_AUDIO_DURATION_SECONDS = 3600


class TagWriteError(Exception):
    """A tag write failed; the step's work stays incomplete and retries next run."""


@dataclass
class TagSnapshot:
    """The tags of one file as read once during the walk, shared by all steps.

    ``readable=False`` means mutagen could not parse the container at all —
    the file is skipped by every per-file check but still participates in
    album-level ReplayGain (rsgain decodes it independently of mutagen).
    """

    path: Path
    readable: bool = True
    skipped: str | None = None
    values: dict[str, str | None] = field(default_factory=dict)
    # audio facts used for stats
    duration: float | None = None
    channels: int | None = None
    artist: str | None = None
    album: str | None = None
    track_gain: float | None = None
    track_peak: float | None = None

    def get(self, name: str) -> str | None:
        return self.values.get(name)


def _first_str(value) -> str | None:
    """Extract a single string from any mutagen value shape."""
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        inner = value[0]
        if isinstance(inner, MP4FreeForm):
            return inner.decode("utf-8", "replace").strip() or None
        return str(inner).strip() or None
    return str(value).strip() or None


def _parse_gain(text: str | None) -> float | None:
    """'-13.36 dB' -> -13.36"""
    if not text:
        return None
    try:
        return float(text.lower().removesuffix("db").strip())
    except ValueError:
        return None


# Logical tag name -> per-format storage key. Anything not listed is a
# custom tag stored as TXXX:<name> (ID3) / <name> (Vorbis) / freeform (MP4).
_ID3_MAP = {
    T_BPM: "TBPM",
    T_KEY: "TKEY",
    "ARTIST": "TPE1",
    "ALBUMARTIST": "TPE2",
    "ALBUM": "TALB",
    "REPLAYGAIN_TRACK_GAIN": "TXXX:REPLAYGAIN_TRACK_GAIN",
    "REPLAYGAIN_TRACK_PEAK": "TXXX:REPLAYGAIN_TRACK_PEAK",
}
_MP4_MAP = {
    T_BPM: "tmpo",
    "ARTIST": "©ART",
    "ALBUMARTIST": "aART",
    "ALBUM": "©alb",
}


def _storage_key(kind: str, logical: str) -> str:
    if kind == "id3":
        return _ID3_MAP.get(logical, f"TXXX:{logical}")
    if kind == "mp4":
        return _MP4_MAP.get(logical, _mp4_freeform_key(logical))
    return logical.upper()


def _tag_kind(tags) -> str:
    if isinstance(tags, ID3):
        return "id3"
    if isinstance(tags, MP4Tags):
        return "mp4"
    return "vorbis"


def read_snapshot(path: Path) -> TagSnapshot:
    """Read a file's tags once. Never raises: unreadable -> readable=False."""
    snap = TagSnapshot(path=path)
    try:
        mf = mutagen.File(str(path))
    except Exception as exc:  # container-level corruption (spec: skip, never fail)
        log.debug("unreadable tags %s: %s: %s", path.name, type(exc).__name__, exc)
        snap.readable = False
        return snap
    if mf is None:
        snap.readable = False
        return snap

    tags = mf.tags
    kind = _tag_kind(tags) if tags is not None else "vorbis"

    def get_logical(logical: str) -> str | None:
        if tags is None:
            return None
        return _first_str(_get_any(tags, _storage_key(kind, logical)))

    values = {
        T_RGTOOL: get_logical(T_RGTOOL),
        T_BPM: get_logical(T_BPM),
        T_KEY: get_logical(T_KEY),
        T_RGSKIP: get_logical(T_RGSKIP),
        T_BPMSKIP: get_logical(T_BPMSKIP),
        T_KEYSKIP: get_logical(T_KEYSKIP),
    }

    info = mf.info
    snap.duration = getattr(info, "length", None)
    snap.channels = getattr(info, "channels", None)
    snap.values = values
    snap.artist = get_logical("ALBUMARTIST") or get_logical("ARTIST")
    snap.album = get_logical("ALBUM")
    snap.track_gain = _parse_gain(get_logical("REPLAYGAIN_TRACK_GAIN"))
    snap.track_peak = _parse_peak(get_logical("REPLAYGAIN_TRACK_PEAK"))

    if snap.duration and snap.duration > MAXIMUM_AUDIO_DURATION_SECONDS:
        snap.skipped = "length greater than 1hr"

    return snap


def _parse_peak(value) -> float | None:
    text = _first_str(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _get_any(tags, name: str):
    """Case/format-insensitive fetch of a custom tag's raw value."""
    if tags is None:
        return None
    upper = name.upper()
    # Fast path for the common exact-key layouts; formats with case-sensitive
    # storage fall through to the scan below.
    v = tags.get(name)
    if v is not None and v != []:
        return v
    keys = tags.keys()
    for key in keys:
        if isinstance(key, str) and _key_matches(key, upper):
            return tags[key]
    return None


def _key_matches(key: str, upper_name: str) -> bool:
    ku = key.upper()
    if ku == upper_name:  # Vorbis exact (case-insensitive) or ID3 frame
        return True
    if ku.startswith("TXXX:"):  # ID3 custom frame: TXXX:<desc>
        return ku[len("TXXX:") :] == upper_name
    if ku.startswith("----:"):  # MP4 freeform: ----:<mean>:<name>
        tail = ku.split(":", 2)
        return len(tail) == 3 and tail[2] == upper_name
    return False


def _mp4_freeform_key(name: str) -> str:
    return f"----:{MP4_FREEFORM_MEAN}:{name}"


def _mp4_freeform(value: str) -> MP4FreeForm:
    return MP4FreeForm(
        value.encode("utf-8"),
        dataformat=AtomDataType.UTF8,
    )


def _norm_bpm_value(name: str, value: str) -> str | int:
    """MP4's tmpo atom is an integer; everything else keeps strings."""
    if name == T_BPM:
        try:
            return int(round(float(value)))
        except ValueError:
            return value
    return value


def write_tag(path: Path, name: str, value: str) -> bool:
    """Write one custom tag onto a file.

    Returns True if the file was modified and saved, False if the tag already
    carried exactly this value (no write needed). Raises TagWriteError when
    the save itself fails — the caller reports and retries next run.
    """
    try:
        mf = mutagen.File(str(path))
    except Exception as exc:
        raise TagWriteError(f"{type(exc).__name__}: {exc}") from exc
    if mf is None:
        raise TagWriteError("mutagen could not detect the file type")
    if mf.tags is None:
        # Some formats (raw FLAC without tags) need an empty tag object added.
        try:
            mf.add_tags()
        except Exception as exc:
            raise TagWriteError(f"cannot add tags: {type(exc).__name__}: {exc}") from exc

    tags = mf.tags
    if tags is None:  # add_tags() above should have created them
        raise TagWriteError("file has no tag container")

    # Look up the current value through the storage-key mapping so existing
    # values in any spelling are recognized and writes stay idempotent.
    current = _first_str(_get_any(tags, _storage_key(_tag_kind(tags), name)))
    if current == value and not (isinstance(tags, MP4Tags) and name == T_BPM):
        return False

    if isinstance(tags, ID3):
        _write_id3(tags, name, value)
    elif isinstance(tags, MP4Tags):
        key = _mp4_freeform_key(name)
        if name == T_BPM:
            # tmpo is the standard MP4 BPM atom and players read it; the
            # freeform mirror covers tag readers that skip tmpo.
            tags["tmpo"] = [_norm_bpm_value(name, value)]
            tags[key] = [_mp4_freeform(value)]
        else:
            tags[key] = [_mp4_freeform(value)]
    else:
        # Vorbis comments and anything else key/value shaped: replace any
        # existing spelling of this field with the canonical uppercase one.
        existing_key = _existing_key(tags, name)
        if existing_key is not None and existing_key != name:
            del tags[existing_key]
        try:
            tags[name] = [value]
        except ValueError:
            tags[name] = value

    try:
        mf.save()
    except Exception as exc:
        raise TagWriteError(f"{type(exc).__name__}: {exc}") from exc
    return True


def _existing_key(tags, name: str) -> str | None:
    upper = name.upper()
    keys = tags.keys()
    for key in keys:
        if isinstance(key, str) and _key_matches(key, upper):
            return key
    return None


def _write_id3(tags: ID3, name: str, value: str) -> None:
    if name == T_BPM:
        try:
            bpm = str(int(round(float(value))))
        except ValueError:
            bpm = value
        tags.setall("TBPM", [TBPM(encoding=1, text=[bpm])])
    elif name == T_KEY:
        tags.setall("TKEY", [TKEY(encoding=1, text=[value])])
    else:
        tags.setall(f"TXXX:{name}", [TXXX(encoding=1, desc=name, text=[value])])
