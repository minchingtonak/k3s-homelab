"""Configuration for the library librarian tool.

Everything is driven by environment variables so the same script runs as a
Kubernetes CronJob container (secrets and downward-API values injected as env)
and as a plain local script against a scratch directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Audio extensions we walk, by tag family from the spec:
#   ID3v2        -> MP3, WAV, AIFF
#   Vorbis       -> OGG, FLAC, Opus
#   MP4 atoms    -> M4A
AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".aiff", ".aif", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4"}
)

DEFAULT_LIBRARY_DIR = "/data/music"
DEFAULT_SCAN_TIMEOUT = 12 * 3600  # 12h pipeline window, per spec
DEFAULT_SUBPROCESS_TIMEOUT = 1800  # 30 min per analysis subprocess (long mixes are legal)
DEFAULT_HEARTBEAT_INTERVAL = 60.0  # seconds, per spec observability section


def _env_int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as err:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from err


def _env_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as err:
        raise ValueError(f"{name} must be a number, got {raw!r}") from err


@dataclass(frozen=True)
class ToolSpec:
    """One analysis binary and how to locate it.

    Resolution order:
      1. explicit ``env_bin`` path (dev escape hatch: LIBRARIAN_<NAME>_BIN)
      2. ``PATH`` lookup by ``filename``

    In production the binaries live in the image (docker/librarian/Dockerfile);
    PATH is the normal case and the env override exists for local runs.
    """

    name: str  # lowercase human name, e.g. "rsgain"
    env_bin: str  # env var holding an explicit binary path
    filename: str  # executable name looked up on PATH


@dataclass(frozen=True)
class Config:
    library_dir: Path = Path(DEFAULT_LIBRARY_DIR)
    scan_timeout: float = DEFAULT_SCAN_TIMEOUT
    subprocess_timeout: float = DEFAULT_SUBPROCESS_TIMEOUT
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL

    # ReplayGain settings fingerprint. This is rsgain's own RGTOOL value for
    # the exact invocation we use (rsgain 3.8, true peak, -18 LUFS target).
    # Changing scan settings means changing this string, which automatically
    # queues the whole library for one re-tagging pass (see spec).
    rg_fingerprint: str = "rsgain-3.8/tp/-18"

    pushover_token: str | None = None
    pushover_user: str | None = None
    headlamp_url: str | None = None
    pod_name: str | None = None
    pod_namespace: str | None = None

    tools: tuple[ToolSpec, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)

        def get(name: str) -> str | None:
            v = env.get(name, "").strip()
            return v or None

        tools = (
            ToolSpec(name="rsgain", env_bin="LIBRARIAN_RSGAIN_BIN", filename="rsgain"),
            ToolSpec(name="aubio", env_bin="LIBRARIAN_AUBIO_BIN", filename="aubio"),
            ToolSpec(
                name="keyfinder-cli",
                env_bin="LIBRARIAN_KEYFINDER_BIN",
                filename="keyfinder-cli",
            ),
        )
        return cls(
            library_dir=Path(get("LIBRARIAN_LIBRARY_DIR") or DEFAULT_LIBRARY_DIR),
            scan_timeout=_env_float(env, "LIBRARIAN_SCAN_TIMEOUT", DEFAULT_SCAN_TIMEOUT),
            subprocess_timeout=_env_float(
                env, "LIBRARIAN_SUBPROCESS_TIMEOUT", DEFAULT_SUBPROCESS_TIMEOUT
            ),
            heartbeat_interval=_env_float(
                env, "LIBRARIAN_HEARTBEAT_INTERVAL", DEFAULT_HEARTBEAT_INTERVAL
            ),
            rg_fingerprint=get("LIBRARIAN_RG_FINGERPRINT") or "rsgain-3.8/tp/-18",
            pushover_token=get("PUSHOVER_TOKEN"),
            pushover_user=get("PUSHOVER_USER_KEY"),
            headlamp_url=get("LIBRARIAN_HEADLAMP_URL"),
            pod_name=get("POD_NAME"),
            pod_namespace=get("POD_NAMESPACE"),
            tools=tools,
        )
