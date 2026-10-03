"""Pushover notifications and message formatting.

The notification contract (spec):

  Situation                              Pushover                        Exit
  ---------------------------------------------------------------------- -----
  Any work done, no errors               combined stats push             0
  Work done + some files skipped         stats push, then notice listing 0
  Nothing to do                          silence                         0
  Scan window expired with work done     progress push                   0
  Scan window expired with zero work     failure push                    1
  Infrastructure failure                 failure push                    1

Pushover delivery itself is best-effort: a failed POST is logged as an error
but never changes the run's exit code (the job's outcome is the file tags,
not the pager). Locally, with no PUSHOVER_TOKEN/USER configured, pushes are
logged instead of sent — which also keeps local test runs quiet.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import Config
from .stats import LibraryStats, format_key_distribution, format_loudness_line

log = logging.getLogger("librarian.notify")

PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"
MAX_MESSAGE_CHARS = 4000  # keep under Pushover's 10k limit with headroom
LIST_CAP = 40  # max files listed in the skipped-files notice


@dataclass
class Push:
    title: str
    message: str
    priority: int = 0  # 0 normal, 1 high (failures)


@dataclass
class Notifier:
    config: Config
    sent: list[Push] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.config.pushover_token and self.config.pushover_user)

    def push(self, title: str, message: str, priority: int = 0) -> None:
        push = Push(title=title, message=message, priority=priority)
        self.sent.append(push)
        if not self.configured:
            log.info("pushover (not configured, logging only) [%s] %s", title, message)
            return
        payload = build_payload(self.config, push)
        try:
            post_form(payload)
            log.info("pushover sent: %s", title)
        except Exception as exc:
            log.error("pushover delivery failed (%s): %s", title, exc)

    def logs_link(self) -> str | None:
        """Headlamp deep link to this run's pod logs, if the env knows how."""
        cfg = self.config
        if not (cfg.headlamp_url and cfg.pod_name and cfg.pod_namespace):
            return None
        return headlamp_logs_url(cfg.headlamp_url, cfg.pod_namespace, cfg.pod_name)


def headlamp_logs_url(base: str, namespace: str, pod: str) -> str:
    base = base.rstrip("/")
    return f"{base}/#/namespace/{namespace}/pod/{pod}/logs?container=librarian"


def build_payload(config: Config, push: Push) -> dict[str, str]:
    return {
        "token": config.pushover_token or "",
        "user": config.pushover_user or "",
        "title": push.title,
        "message": push.message,
        "priority": str(push.priority),
    }


def post_form(payload: dict[str, str], endpoint: str = PUSHOVER_ENDPOINT) -> bytes:
    """POST the payload; endpoint defaults to the Pushover API (a literal)."""
    data = urllib.parse.urlencode(payload).encode()
    opener = urllib.request.build_opener()
    with opener.open(endpoint, data=data, timeout=30) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Message formatting (pure functions, unit-testable)
# ---------------------------------------------------------------------------


def format_stats_message(
    stats: LibraryStats,
    step_summaries: list[str],
    elapsed_s: float,
    logs_link: str | None = None,
) -> str:
    lines = [
        f"Librarian: {stats.albums} albums, {stats.files} files walked"
        + (f", {stats.unreadable} unreadable" if stats.unreadable else ""),
        *step_summaries,
        format_loudness_line(stats),
        f"Clip adjustments: {stats.clip_adjustments} tracks gain-limited",
    ]
    if stats.bpm_values:
        lines.append(f"Median BPM: {stats.median_bpm:.0f}")
    if stats.key_counts:
        lines.append(format_key_distribution(stats))
    lines.append(f"Elapsed: {format_duration(elapsed_s)}")
    if logs_link:
        lines.append(f"Pod logs: {logs_link}")
    return "\n".join(lines)


def format_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def format_skipped_notice(
    skip_tagged: list[str], write_failures: list[str]
) -> str | None:
    """The normal-priority follow-up listing un-analyzable/failed files."""
    entries: list[str] = []
    for path in skip_tagged:
        entries.append(f"un-analyzable: {path}")
    for path in write_failures:
        entries.append(f"write failed: {path}")
    if not entries:
        return None
    shown = entries[:LIST_CAP]
    hidden = len(entries) - len(shown)
    body = "\n".join(shown)
    if hidden > 0:
        body += f"\n… and {hidden} more"
    return body


def format_progress_message(processed: int, elapsed_s: float) -> str:
    return (
        f"Scan window ended after {format_duration(elapsed_s)}: "
        f"{processed} files processed this run; next run continues from the "
        f"top (tags are the state)."
    )


def format_failure_message(reason: str, elapsed_s: float, logs_link: str | None) -> str:
    lines = [f"Librarian FAILED after {format_duration(elapsed_s)}: {reason}"]
    if logs_link:
        lines.append(f"Pod logs: {logs_link}")
    return "\n".join(lines)


def truncate(message: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 20] + "\n… (truncated)"


def dumps(push: Push) -> str:
    """JSON rendering of a push, for logs and tests."""
    return json.dumps(
        {"title": push.title, "message": push.message, "priority": push.priority}
    )
