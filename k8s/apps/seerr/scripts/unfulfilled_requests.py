#!/usr/bin/env python3
"""Report Seerr media requests that have not been fulfilled yet.

Queries filter=unavailable (requests whose media is pending, processing,
or only partially available), then trims requests that are unfulfilled
*by design*:

- content that has not been released/aired yet (movie releaseDate or
  requested-season airDate in the future, or absent from TMDB)
- TV media where every requested season is already available and only
  unrequested seasons keep the media "partially available"
- TV seasons where every aired episode is already downloaded in Sonarr
  and only unaired episodes remain (checked via Sonarr episode data;
  when Sonarr cannot be consulted the request is kept, not skipped)

If any genuinely unfulfilled requests remain, sends a Pushover
notification listing them; an empty week only logs a message. Failures
send a best-effort Pushover and exit 1.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

SEERR_URL = os.environ.get(
    "SEERR_URL", "http://seerr.seerr.svc.cluster.local"
).rstrip("/")
SONARR_URL = os.environ.get(
    "SONARR_URL", "http://sonarr.servarr.svc.cluster.local:8989"
).rstrip("/")
USER_AGENT = "seerr-unfulfilled-requests/1.0"
TODAY = date.today().isoformat()

MEDIA_STATUS = {
    1: "unknown",
    2: "pending",
    3: "processing",
    4: "partially available",
    5: "available",
}


def env_int(name, default):
    value = os.environ.get(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"invalid {name}={value!r}, expected an integer")


MAX_ITEMS = env_int("MAX_ITEMS", 15)
TAKE = env_int("TAKE", 100)


def log(msg):
    print(msg, flush=True)


def api(base, key, path):
    """One REST API call with an X-Api-Key header (seerr or sonarr)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if key:
        headers["X-Api-Key"] = key
    req = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"non-JSON response from {path}: {raw[:200]!r}")


def seerr(path):
    return api(
        SEERR_URL, os.environ.get("SEERR_API_KEY", ""), path
    )


def sonarr(path):
    return api(
        SONARR_URL, os.environ.get("SONARR_API_KEY", ""), path
    )


def pushover(title, message, priority=0):
    token = os.environ.get("PUSHOVER_TOKEN", "")
    user = os.environ.get("PUSHOVER_USER_KEY", "")
    if not token or not user:
        log("PUSHOVER_TOKEN / PUSHOVER_USER_KEY not set; skipping")
        return
    data = urllib.parse.urlencode(
        {
            "token": token,
            "user": user,
            "title": title,
            "message": message,
            "priority": priority,
        }
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


def media_detail(media_type, tmdb_id, cache):
    """Fetch (and cache) the seerr media detail for titles/air dates."""
    key = (media_type, tmdb_id)
    if key not in cache:
        path = f"/api/v1/{media_type}/{tmdb_id}"
        try:
            cache[key] = seerr(path)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log(f"detail lookup failed for {path}: {exc!r}")
            cache[key] = {}
    return cache[key]


def sonarr_missing_aired(series_id, season_number):
    """Count a season's aired-but-not-downloaded episodes in Sonarr.

    Returns (missing, unaired, ok); ok=False when Sonarr could not be
    consulted (API failure), with zeroed counts.
    """
    path = (
        f"/api/v3/episode?seriesId={series_id}"
        f"&seasonNumber={season_number}"
    )
    try:
        episodes = sonarr(path)
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        log(f"sonarr lookup failed for series {series_id}"
            f" season {season_number}: {exc!r}")
        return 0, 0, False
    missing = unaired = 0
    for ep in episodes:
        air = ep.get("airDate") or "9999-99-99"
        if air > TODAY:
            unaired += 1
        elif ep.get("monitored") and not ep.get("hasFile"):
            missing += 1
    return missing, unaired, True


def classify(req, cache):
    """Decide whether a request is genuinely unfulfilled.

    Returns (entry, reason, short): when the request should be
    reported, entry is its display line (reason and short are None);
    otherwise entry is None, reason names why it is fine by design,
    and short is a "Title (S1,S2)"-style name for the hidden list.
    """
    media = req.get("media") or {}
    media_type = media.get("mediaType", "movie")
    tmdb_id = media.get("tmdbId")
    detail = media_detail(media_type, tmdb_id, cache)
    title = detail.get("title") or detail.get("name")
    if not title:
        title = f"{media_type} #{tmdb_id}"
    label = MEDIA_STATUS.get(media.get("status") or 0, "unknown")
    season_nums = sorted(
        s.get("seasonNumber") for s in req.get("seasons") or []
    )
    short = title
    if media_type == "tv" and season_nums:
        nums = ",".join(f"S{n}" for n in season_nums)
        short = f"{title} ({nums})"

    if media_type != "tv":
        if (media.get("status") or 0) == 5:
            return None, "requested media already available", short
        release = detail.get("releaseDate") or "9999-99-99"
        if release > TODAY:
            return None, "not released yet", short
        return f"{title} [{label}]", None, None

    seasons = req.get("seasons") or []
    if not seasons:
        # whole-series request without season detail: fall back to the
        # media-level status and the show's first air date
        if (media.get("status") or 0) == 5:
            return None, "requested media already available", short
        first_air = detail.get("firstAirDate") or "9999-99-99"
        if first_air > TODAY:
            return None, "not released yet", short
        return f"{title} [{label}]", None, None

    unavailable = [s for s in seasons if (s.get("status") or 0) != 5]
    if not unavailable:
        # every requested season is fully available; only unrequested
        # seasons keep the media partially available - by design
        return None, "requested seasons all available", short

    air_dates = {
        s.get("seasonNumber"): s.get("airDate")
        for s in detail.get("seasons") or []
    }
    aired, unaired = [], []
    for season in unavailable:
        num = season.get("seasonNumber")
        air = air_dates.get(num) or "9999-99-99"
        (aired if air <= TODAY else unaired).append(num)
    if not aired:
        return None, "not released yet", short

    # every aired episode already downloaded? (currently-airing shows
    # waiting on future episodes are unfulfilled by design)
    series_id = media.get("externalServiceId")
    nums = ",".join(f"S{n}" for n in sorted(aired))
    entry = f"{title} ({nums}) [{label}]"
    if series_id:
        checks = [sonarr_missing_aired(series_id, n) for n in aired]
        if all(ok for _, _, ok in checks):
            total_missing = sum(m for m, _, _ in checks)
            if total_missing == 0:
                return None, "aired episodes all downloaded", short
            entry += f" - {total_missing} aired episode(s) missing"
    if unaired:
        extra = ",".join(f"S{n}" for n in sorted(unaired))
        entry += f" (+{extra} not yet aired)"
    return entry, None, None


def main():
    query = urllib.parse.urlencode({"filter": "unavailable", "take": TAKE})
    data = seerr("/api/v1/request?" + query)
    reqs = data.get("results") or []
    page_info = data.get("pageInfo") or {}
    total = page_info.get("results") or len(reqs)

    if not total:
        log("ran successfully: no unfulfilled requests, nothing to report")
        return

    cache = {}
    kept, hidden = [], []
    for req in reqs:
        entry, reason, short = classify(req, cache)
        if entry:
            kept.append(entry)
        elif reason:
            hidden.append((short, reason))

    log(f"{total} unavailable request(s) -> {len(kept)} to report")
    for entry in kept[:MAX_ITEMS]:
        log(f"  - {entry}")
    for short, reason in hidden:
        log(f"  hidden: {short}: {reason}")

    if not kept:
        log("nothing genuinely unfulfilled; not notifying")
        return

    lines = [f"- {e}" for e in kept[:MAX_ITEMS]]
    if len(kept) > MAX_ITEMS:
        lines.append(f"... and {len(kept) - MAX_ITEMS} more")
    if hidden:
        lines.append("")
        lines.append("Not counted:")
        grouped = {}
        for short, reason in hidden:
            grouped[reason] = grouped.get(reason, 0) + 1
        for reason in sorted(grouped):
            lines.append(f"- {reason}: {grouped[reason]}")
    message = f"{len(kept)} unfulfilled request(s) in Seerr:\n"
    message += "\n".join(lines)
    pushover(f"Seerr: {len(kept)} unfulfilled request(s)", message)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - report anything, fail job
        log(f"ERROR: {exc!r}")
        try:
            pushover(
                "Seerr unfulfilled check FAILED",
                f"error: {exc!r}",
                priority=1,
            )
        except Exception as notify_exc:  # noqa: BLE001
            log(f"failed to send failure notification: {notify_exc!r}")
        sys.exit(1)
