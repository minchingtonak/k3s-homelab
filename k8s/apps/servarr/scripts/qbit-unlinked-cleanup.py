#!/usr/bin/env python3
"""Delete qBittorrent torrents tagged as unlinked by cleanuparr, then
notify via Pushover. Also notifies when nothing is tagged (weekly
heartbeat), and best-effort notifies on failure.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

QBIT_URL = os.environ.get(
    "QBIT_URL", "http://qbittorrent.servarr.svc.cluster.local:8081"
).rstrip("/")
CLEANUP_TAG = os.environ.get("CLEANUP_TAG", "cleanuparr-unlinked")
DELETE_FILES = os.environ.get("DELETE_FILES", "true").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
USER_AGENT = "qbit-unlinked-cleanup/1.0"


def env_int(name, default):
    value = os.environ.get(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"invalid {name}={value!r}, expected an integer")


MAX_NAMES = env_int("MAX_NAMES", 15)


def log(msg):
    print(msg, flush=True)


def human_size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def urlencode(form):
    # quote_via=quote keeps '|' readable; qBittorrent accepts both forms.
    return urllib.parse.urlencode(
        form, quote_via=urllib.parse.quote
    ).encode()


def qbit(method, path, form=None, cookie=None):
    """One qBittorrent WebUI API call. Returns (body, headers)."""
    headers = {"User-Agent": USER_AGENT, "Referer": QBIT_URL + "/"}
    data = None
    if form is not None:
        data = urlencode(form)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(
        QBIT_URL + path, data=data, method=method, headers=headers
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode(), resp.headers

def open_session():
    """Return a session cookie, or None when already authorized.

    In-cluster callers are authorized by the WebUI auth-subnet
    whitelist, so no login is needed. On 401 (whitelist removed,
    calling from outside the cluster, ...) fall back to password
    login if credentials were provided.
    """
    try:
        qbit("GET", "/api/v2/app/version")
        return None
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
    user = os.environ.get("QBIT_USERNAME", "")
    password = os.environ.get("QBIT_PASSWORD", "")
    if not user or not password:
        raise RuntimeError(
            "qBittorrent returned 401 and no QBIT_USERNAME /"
            " QBIT_PASSWORD are set for fallback login"
        )
    # login returns HTTP 200 with body "Fails." on bad credentials,
    # so the body must be checked, not just the status code.
    body, resp_headers = qbit(
        "POST",
        "/api/v2/auth/login",
        form={"username": user, "password": password},
    )
    if body.strip() != "Ok.":
        raise RuntimeError(f"qBittorrent login failed: {body.strip()!r}")
    return resp_headers["Set-Cookie"].split(";", 1)[0]


def pushover(title, message, priority=0):
    token = os.environ.get("PUSHOVER_TOKEN", "")
    user = os.environ.get("PUSHOVER_USER_KEY", "")
    if not token or not user:
        log("PUSHOVER_TOKEN / PUSHOVER_USER_KEY not set; skipping")
        return
    data = urlencode(
        {
            "token": token,
            "user": user,
            "title": title,
            "message": message,
            "priority": priority,
        }
    )
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


def main():
    cookie = open_session()

    path = "/api/v2/torrents/info?tag=" + urllib.parse.quote(CLEANUP_TAG)
    body, _ = qbit("GET", path, cookie=cookie)
    try:
        torrents = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"non-JSON torrent list from qBittorrent: {body[:200]!r}"
        )
    if not torrents:
        log(f"no torrents tagged {CLEANUP_TAG!r}; nothing to do")
        pushover(
            "qBittorrent cleanup: nothing to do",
            f"No torrents tagged {CLEANUP_TAG!r};"
            " nothing to clean up this week.",
        )
        return

    total = sum(t["size"] for t in torrents)
    verb = "would delete" if DRY_RUN else "deleting"
    log(f"{verb} {len(torrents)} torrent(s), {human_size(total)}:")
    for t in torrents:
        log(f"  - {t['name']} ({human_size(t['size'])})")

    hashes = "|".join(t["hash"] for t in torrents)
    form = {"hashes": hashes, "deleteFiles": str(DELETE_FILES).lower()}
    if DRY_RUN:
        preview = hashes if len(hashes) <= 64 else hashes[:61] + "..."
        log(f"dry run: would POST /api/v2/torrents/delete hashes="
            f"{preview} deleteFiles={form['deleteFiles']}")
        return
    qbit("POST", "/api/v2/torrents/delete", form=form, cookie=cookie)
    log("delete request accepted by qBittorrent")

    names = [t["name"] for t in torrents]
    lines = [f"Deleted {len(names)} unlinked torrent(s)"
             f", {human_size(total)} freed:"]
    lines.extend("- " + n for n in names[:MAX_NAMES])
    if len(names) > MAX_NAMES:
        lines.append(f"... and {len(names) - MAX_NAMES} more")
    title = f"qBittorrent cleanup: {len(names)} unlinked deleted"
    pushover(title, "\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - report anything, fail job
        log(f"ERROR: {exc!r}")
        try:
            pushover(
                "qBittorrent cleanup FAILED",
                f"error: {exc!r}",
                priority=1,
            )
        except Exception as notify_exc:  # noqa: BLE001
            log(f"failed to send failure notification: {notify_exc!r}")
        sys.exit(1)
