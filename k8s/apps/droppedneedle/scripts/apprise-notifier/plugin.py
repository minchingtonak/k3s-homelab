"""Apprise Notifier - forwards DroppedNeedle events to an Apprise API endpoint.

Subscriber plugin: receives PluginEvent structs from the host and POSTs a
compact notification to an Apprise stateful notify URL (/notify/<config-id>),
which fans out to whatever channels Apprise has configured. Uses the host's
HTTP client and logger; never raises into the host event flow.
"""

DEFAULT_URL = "http://apprise.notification.svc.cluster.local:8000/notify/droppedneedle"
DEFAULT_EVENTS = ("download_failed", "import_finished", "request_fulfilled")

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

# Field harvest order per payload struct; anything absent is skipped.
_FIELDS = (
    "title", "body",  # plugin_notice
    "artist", "track", "album",  # scrobble / playback
    "outcome", "source", "track_count",  # download / import
    "release_group_mbid", "task_id", "request_id", "user_id",
)


class AppriseNotifier:
    def __init__(self, context):
        self.ctx = context

    def _wanted(self, kind: str) -> bool:
        raw = (self.ctx.settings.get("events") or "").strip()
        kinds = [k.strip() for k in raw.split(",") if k.strip()] if raw else list(DEFAULT_EVENTS)
        return kind in kinds

    async def on_event(self, event) -> None:
        kind = event.kind
        if not self._wanted(kind):
            return
        url = (self.ctx.settings.get("apprise_url") or "").strip() or DEFAULT_URL
        payload = getattr(event, "payload", None)
        lines = []
        for field in _FIELDS:
            value = getattr(payload, field, None)
            if value not in (None, ""):
                lines.append(f"{field}={value}")
        notification = {
            "title": f"DroppedNeedle: {_TITLES.get(kind, kind)}",
            "body": "\n".join(lines) if lines else kind,
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
