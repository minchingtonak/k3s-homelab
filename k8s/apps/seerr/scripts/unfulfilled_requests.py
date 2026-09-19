#!/usr/bin/env python3
"""Report Seerr media requests that have not been fulfilled yet.

Queries filter=unavailable (requests whose media is pending, processing,
or only partially available). If any exist, sends a Pushover notification
listing them; an empty week only logs a message. Failures send a
best-effort Pushover and exit 1.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get(
    "SEERR_URL", "http://seerr.seerr.svc.cluster.local"
).rstrip("/")
USER_AGENT = "seerr-unfulfilled-requests/1.0"


def env_int(name, default):
    value = os.environ.get(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"invalid {name}={value!r}, expected an integer")


MAX_ITEMS = env_int("MAX_ITEMS", 15)
TAKE = env_int("TAKE", 100)

MEDIA_STATUS = {
    1: "unknown",
    2: "pending",
    3: "processing",
    4: "partially available",
    5: "available",
}


def log(msg):
    print(msg, flush=True)


def api(path):
    """One Seerr REST API call."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    key = os.environ.get("SEERR_API_KEY", "")
    if key:
        headers["X-Api-Key"] = key
    req = urllib.request.Request(BASE_URL + path, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"non-JSON response from {path}: {raw[:200]!r}")


def pushover(title, message):
    token = os.environ.get("PUSHOVER_TOKEN", "")
    user = os.environ.get("PUSHOVER_USER_KEY", "")
    if not token or not user:
        log("PUSHOVER_TOKEN / PUSHOVER_USER_KEY not set; skipping")
        return
    data = urllib.parse.urlencode(
        {"token": token, "user": user, "title": title, "message": message}
    ).encode()
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=data,
        method="POST",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"non-JSON response from pushover: {raw[:200]!r}"
        )
    if body.get("status") != 1:
        raise RuntimeError(f"pushover rejected notification: {body}")
    log("pushover notification sent")


def media_title(media_type, tmdb_id, cache):
    """Look up a human title for the requested media; degrade gracefully."""
    key = (media_type, tmdb_id)
    if key in cache:
        return cache[key]
    path = f"/api/v1/{media_type}/{tmdb_id}"
    try:
        detail = api(path)
        title = detail.get("title") or detail.get("name")
        if not title:
            raise RuntimeError("no title field")
    except Exception as exc:  # noqa: BLE001 - a lookup miss is not fatal
        log(f"title lookup failed for {path}: {exc!r}")
        title = f"{media_type} #{tmdb_id}"
    cache[key] = title
    return title


def describe(req, cache):
    media = req.get("media") or {}
    media_type = media.get("mediaType", "movie")
    title = media_title(media_type, media.get("tmdbId"), cache)
    if media_type == "tv":
        seasons = sorted(
            s.get("seasonNumber") for s in req.get("seasons") or []
        )
        if seasons:
            nums = ",".join(f"S{n}" for n in seasons)
            title = f"{title} ({nums})"
    media_status = media.get("status") or 0
    status = MEDIA_STATUS.get(media_status, "unknown")
    return f"{title} [{status}]"


def main():
    query = urllib.parse.urlencode({"filter": "unavailable", "take": TAKE})
    data = api("/api/v1/request?" + query)
    reqs = data.get("results") or []
    page_info = data.get("pageInfo") or {}
    total = page_info.get("results") or len(reqs)

    if not total:
        log("ran successfully: no unfulfilled requests, nothing to report")
        return

    log(f"{total} unfulfilled request(s):")
    cache = {}
    lines = []
    for req in reqs[:MAX_ITEMS]:
        entry = describe(req, cache)
        lines.append("- " + entry)
        log(f"  - {entry}")
    if total > MAX_ITEMS:
        lines.append(f"... and {total - len(lines)} more")

    message = f"{total} unfulfilled request(s) in Seerr:\n" + "\n".join(
        lines
    )
    pushover(f"Seerr: {total} unfulfilled request(s)", message)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - report anything, fail job
        log(f"ERROR: {exc!r}")
        try:
            pushover("Seerr unfulfilled check FAILED", f"error: {exc!r}")
        except Exception as notify_exc:  # noqa: BLE001
            log(f"failed to send failure notification: {notify_exc!r}")
        sys.exit(1)
