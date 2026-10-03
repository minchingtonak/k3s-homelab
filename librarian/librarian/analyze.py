"""Analysis subprocess wrappers: rsgain, aubio, keyfinder-cli.

All three run as subprocesses with list argv (never a shell string), so
unicode and special-character paths are passed through verbatim by the OS —
the quoting the spec worries about is a non-issue with exec-style spawning.

Subprocesses are polled instead of blocking so that heartbeats keep flowing
and SIGTERM stays responsive during long analyses (a 66-minute FLAC takes
minutes to decode), and so a per-file wall-clock timeout can be enforced.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ToolSpec

log = logging.getLogger("librarian.tools")


class ToolError(Exception):
    """An analysis binary could not be provisioned (infrastructure failure)."""


class SubprocessTimeout(Exception):
    """The analysis subprocess exceeded its wall-clock budget."""


@dataclass(frozen=True)
class ProcResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False


def run_polling(
    argv: list[str],
    timeout: float,
    heartbeat=None,
    poll_interval: float = 0.5,
    clock=time.monotonic,
) -> ProcResult:
    """Run a subprocess to completion, polling so heartbeats can fire.

    Output is drained by pump threads (a blocking read on the pipe would
    otherwise stall the poll loop until process exit, defeating both the
    heartbeat and the timeout).
    """
    started = clock()
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as err:
        raise ToolError(f"binary not found: {argv[0]}") from err

    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    def pump(stream, chunks: list[bytes]) -> None:
        try:
            while True:
                block = stream.read()
                if not block:
                    return
                chunks.append(block)
        except (OSError, ValueError):
            return

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, out_chunks), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err_chunks), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    while True:
        proc.poll()
        elapsed = clock() - started
        if proc.returncode is not None:
            break
        if elapsed >= timeout:
            timed_out = True
            proc.kill()
            proc.wait()
            break
        if heartbeat is not None:
            heartbeat.beat(f"running {Path(argv[0]).name} ({elapsed:.0f}s elapsed)")
        time.sleep(min(poll_interval, max(timeout - elapsed, 0.01)))
    for reader in readers:
        reader.join(timeout=5)
    stdout = b"".join(out_chunks).decode("utf-8", "replace")
    stderr = b"".join(err_chunks).decode("utf-8", "replace")
    return ProcResult(
        argv=argv,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        duration=clock() - started,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# rsgain (ReplayGain 2.0, album level)
# ---------------------------------------------------------------------------


def rsgain_command(rsgain_bin: str) -> list[str]:
    """The exact rsgain invocation whose RGTOOL marker equals the fingerprint.

    ``custom`` mode is used (not ``easy``) for two measured reasons:
      * easy mode exits 0 even when files fail to scan ("No files were
        scanned"), which would defeat the non-zero-exit error contract;
      * custom mode takes an explicit file list, so the album grouping is
        exactly the walk's grouping.
    Flags: album gain+peak, true peak, -18 LUFS target, clipping protection
    on positive gains (rsgain's easy-mode default), write tags.
    """
    return ["custom", "-a", "-t", "-l", "-18", "-c", "p", "-s", "i"]


def run_rsgain_album(
    rsgain_bin: str, files: list[Path], config: Config, heartbeat=None
) -> ProcResult:
    argv = [rsgain_bin, *rsgain_command(rsgain_bin), *[str(f) for f in files]]
    return run_polling(argv, config.subprocess_timeout, heartbeat=heartbeat)


# ---------------------------------------------------------------------------
# aubio (BPM, per file)
# ---------------------------------------------------------------------------

_BPM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*bpm", re.IGNORECASE)


def parse_bpm(stdout: str) -> float | None:
    """Extract the BPM from aubio tempo output.

    aubio prints tempo estimates as they firm up and the final value last,
    so the last match wins. Returns None when nothing parseable was printed.
    """
    matches = _BPM_RE.findall(stdout)
    if not matches:
        return None
    try:
        bpm = float(matches[-1])
    except ValueError:
        return None
    if bpm <= 0 or bpm > 1000:  # degenerate estimates are failures
        return None
    return bpm


def run_aubio(aubio_bin: str, path: Path, config: Config, heartbeat=None) -> ProcResult:
    return run_polling(
        [aubio_bin, "tempo", str(path)], config.subprocess_timeout, heartbeat=heartbeat
    )


# ---------------------------------------------------------------------------
# keyfinder-cli (musical key, per file)
# ---------------------------------------------------------------------------

# keyfinder-cli's standard notation (its default). Anything else on stdout is
# treated as unparseable output and handled by the error contract.
_KEYS = {
    "A", "Am", "Bb", "Bbm", "B", "Bm", "C", "Cm", "Db", "Dbm",
    "D", "Dm", "Eb", "Ebm", "E", "Em", "F", "Fm", "Gb", "Gbm",
    "G", "Gm", "Ab", "Abm",
}


def parse_key(stdout: str) -> str | None:
    line = stdout.strip()
    if not line:
        return None
    # Tolerate trailing status lines by scanning for a key token.
    for token in reversed(line.split()):
        if token in _KEYS:
            return token
    return None


def run_keyfinder(
    keyfinder_bin: str, path: Path, config: Config, heartbeat=None
) -> ProcResult:
    return run_polling(
        [keyfinder_bin, str(path)], config.subprocess_timeout, heartbeat=heartbeat
    )


# ---------------------------------------------------------------------------
# Tool provisioning
# ---------------------------------------------------------------------------


def resolve_tool(spec: ToolSpec, config: Config, env: Mapping[str, str] | None = None) -> str:
    """Return a usable path to the analysis binary for ``spec``.

    Order: explicit env override (LIBRARIAN_<TOOL>_BIN), then PATH lookup.
    The binaries live in the CronJob image (docker/librarian/Dockerfile); PATH is
    the production case and the env override serves local runs. Raises
    ToolError (infrastructure failure — a broken image) when neither works.
    """
    env = os.environ if env is None else env

    explicit = env.get(spec.env_bin, "").strip()
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ToolError(f"{spec.env_bin}={explicit} does not exist")
        return str(p)

    found = shutil.which(spec.filename)
    if found:
        return found
    raise ToolError(
        f"could not provision {spec.name}: no {spec.env_bin} and not on PATH"
    )


def resolve_all_tools(
    config: Config, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    return {spec.name: resolve_tool(spec, config, env) for spec in config.tools}
