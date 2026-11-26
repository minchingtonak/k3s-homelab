"""Apprise Notifier - forwards DroppedNeedle events to an Apprise API endpoint.

Subscriber plugin: receives PluginEvent structs from the host and POSTs a
compact notification to an Apprise stateful notify URL (/notify/<config-id>),
which fans out to whatever channels Apprise has configured. Uses the host's
HTTP client and logger; never raises into the host event flow.

Acquisition events carry only a release-group MBID, not the artist or album
title, so the MBID is resolved on MusicBrainz first. Lookups are cached and
best-effort: when MusicBrainz cannot answer, the raw MBID is sent instead.
"""

from __future__ import annotations

import asyncio
import time

DEFAULT_URL = "http://apprise.notification.svc.cluster.local:8000/notify/droppedneedle"
DEFAULT_EVENTS = ("download_failed", "import_finished", "request_fulfilled")

_MB_LOOKUP_URL = "https://musicbrainz.org/ws/2/release-group/{mbid}"
_MB_CONTACT = "https://github.com/minchingtonak/k3s-homelab"
_MB_TIMEOUT = 2.0
_MB_MIN_INTERVAL = 1.0  # MusicBrainz allows one request per second.
_MB_RETRY_AFTER = 60.0  # Pause lookups after a failure so notifications stay quick.

_TITLES = {
    "download_started": "Download started",
    "download_completed": "Download completed",
    "download_failed": "Download failed",
    "request_created": "New request",
    "request_fulfilled": "Request fulfilled",
    "import_finished": "Import finished",
    "playback_started": "Now playing",
    "scrobble": "Scrobble",
    "plugin_notice": "Plugin notice",
}

# Events whose payload identifies the album only by release-group MBID.
_RELEASE_KINDS = frozenset(
    {
        "download_started",
        "download_completed",
        "download_failed",
        "request_created",
        "request_fulfilled",
        "import_finished",
    }
)

# Payload field -> label, in the order the body shows them.
_DETAIL_FIELDS = (
    ("outcome", "outcome"),
    ("source", "source"),
    ("track_count", "tracks"),
    ("status", "status"),
    ("task_id", "task"),
    ("request_id", "request"),
    ("user_id", "user"),
)

_release_labels = {}  # type: dict[str, str]
_last_mb_request = 0.0
_mb_retry_after = 0.0


def _text(payload, field: str) -> str:
    value = getattr(payload, field, None)
    return str(value).strip() if value not in (None, "") else ""


def _artist_credit(release_group: dict) -> str:
    parts = []
    for credit in release_group.get("artist-credit") or []:
        name = (credit.get("name") or "").strip()
        if name:
            parts.append(name + (credit.get("joinphrase") or ""))
    return "".join(parts).strip()


class AppriseNotifier:
    def __init__(self, context):
        self.ctx = context

    def _wanted(self, kind: str) -> bool:
        raw = (self.ctx.settings.get("events") or "").strip()
        kinds = [k.strip() for k in raw.split(",") if k.strip()] if raw else list(DEFAULT_EVENTS)
        return kind in kinds

    async def _release_label(self, mbid: str) -> str:
        """Name a release group, or return "" when MusicBrainz cannot."""
        global _last_mb_request, _mb_retry_after
        if not mbid:
            return ""
        if mbid in _release_labels:
            return _release_labels[mbid]
        if time.monotonic() < _mb_retry_after:
            return ""
        wait = _MB_MIN_INTERVAL - (time.monotonic() - _last_mb_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_mb_request = time.monotonic()
        version = getattr(self.ctx, "version", "") or "2.1.0"
        try:
            response = await self.ctx.http.get(
                _MB_LOOKUP_URL.format(mbid=mbid),
                params={"fmt": "json", "inc": "artist-credits"},
                headers={
                    "User-Agent": f"apprise-notifier/{version} ({_MB_CONTACT})"
                },
                timeout=_MB_TIMEOUT,
            )
            if response.status_code != 200:
                self.ctx.logger.warning(
                    "musicbrainz returned HTTP %s for %s", response.status_code, mbid
                )
                if response.status_code >= 500:
                    _mb_retry_after = time.monotonic() + _MB_RETRY_AFTER
                return ""
            release_group = response.json() or {}
        except Exception as exc:  # noqa: BLE001 - names are best-effort
            self.ctx.logger.warning("musicbrainz lookup failed for %s: %s", mbid, exc)
            _mb_retry_after = time.monotonic() + _MB_RETRY_AFTER
            return ""
        _mb_retry_after = 0.0
        label = " - ".join(
            part
            for part in (
                _artist_credit(release_group),
                (release_group.get("title") or "").strip(),
            )
            if part
        )
        if label:
            _release_labels[mbid] = label
        return label

    async def _body(self, kind: str, payload) -> str:
        artist = _text(payload, "artist")
        track = _text(payload, "track")
        album = _text(payload, "album")
        if artist or track:
            lines = [" - ".join(part for part in (artist, track) if part)]
            if album:
                lines.append(f"album: {album}")
            user_id = _text(payload, "user_id")
            if user_id:
                lines.append(f"user: {user_id}")
            return "\n".join(lines)

        if kind == "plugin_notice":
            lines = [
                text
                for text in (_text(payload, "title"), _text(payload, "body"))
                if text
            ]
            source_plugin = _text(payload, "source_plugin")
            if source_plugin:
                lines.append(f"from: {source_plugin}")
            return "\n".join(lines) or kind

        lines = []
        release = ""
        mbid = _text(payload, "release_group_mbid")
        if kind in _RELEASE_KINDS and mbid:
            release = await self._release_label(mbid)
            if release:
                lines.append(release)
        for field, label in _DETAIL_FIELDS:
            value = _text(payload, field)
            if value:
                lines.append(f"{label}: {value}")
        if mbid and not release:
            lines.append(f"release_group_mbid: {mbid}")
        return "\n".join(lines) or kind

    async def on_event(self, event) -> None:
        kind = event.kind
        if not self._wanted(kind):
            return
        url = (self.ctx.settings.get("apprise_url") or "").strip() or DEFAULT_URL
        payload = getattr(event, "payload", None)
        notification = {
            "title": f"DroppedNeedle: {_TITLES.get(kind, kind)}",
            "body": await self._body(kind, payload) if payload is not None else kind,
            "type": "warning" if kind == "download_failed" else "info",
        }
        try:
            response = await self.ctx.http.post(url, json=notification)
            if response.status_code >= 400:
                self.ctx.logger.warning(
                    "apprise returned HTTP %s for %s", response.status_code, kind
                )
        except Exception as exc:  # noqa: BLE001 - notifications are best-effort
            self.ctx.logger.warning("apprise notify failed for %s: %s", kind, exc)
