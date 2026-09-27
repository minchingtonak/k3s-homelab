#!/usr/bin/env python3
"""Weekly audit of the Forgejo GitHub mirror fleet.

Two checks:
1. Reconciliation - every repo owned by the GitHub account must exist
   as a mirror on Forgejo (forks and archived repos included; they are
   flagged in the report, not excluded).
2. Sync freshness - every Forgejo mirror must have synced within
   STALE_HOURS (mirrors that silently stopped syncing are the failure
   mode gatus cannot see; the web UI stays up either way).

Always pushes a Pushover summary (clean weeks confirm the mirror
count); runtime errors push a high-priority failure notice and exit 1.

The job runs from the forgejo-mirror-audit-script ConfigMap baked by
the configMapGenerator in this kustomization (dagu pattern).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

FORGEJO_URL = os.environ.get("FORGEJO_URL", "http://forgejo.forgejo.svc.cluster.local")
GITHUB_URL = os.environ.get("GITHUB_URL", "https://api.github.com")
try:
    STALE_HOURS = int(os.environ.get("STALE_HOURS", "24"))
except ValueError:
    STALE_HOURS = 24
MAX_ITEMS = 15
USER_AGENT = "k3s-homelab-mirror-audit/1.0"


def log(msg):
    print(msg, flush=True)


def api(base, path, token, bearer=False):
    auth = f"Bearer {token}" if bearer else f"token {token}"
    req = urllib.request.Request(
        f"{base}{path}",
        headers={
            "Authorization": auth,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"non-JSON response from {base}: {raw[:200]!r}")


def forgejo_repos(token):
    """All repos visible to the token, following limit/page pagination."""
    repos, page = [], 1
    while True:
        batch = api(
            FORGEJO_URL,
            f"/api/v1/user/repos?limit=50&page={page}",
            token,
        )
        repos.extend(batch)
        if len(batch) < 50:
            return repos
        page += 1


def github_repos(token):
    """All repos owned by the token's account (affiliation=owner)."""
    repos, page = [], 1
    while True:
        batch = api(
            GITHUB_URL,
            f"/user/repos?affiliation=owner&per_page=100&page={page}",
            token,
            bearer=True,
        )
        repos.extend(batch)
        if len(batch) < 100:
            return repos
        page += 1


def parse_interval_hours(text):
    """'8h30m0s' -> 8.5 (best-effort; None when unparsable)."""
    if not text:
        return None
    try:
        hours = 0.0
        for mult, unit in ((1, "h"), (1 / 60, "m"), (1 / 3600, "s")):
            part, _, text = text.partition(unit)
            if part.isdigit():
                hours += int(part) * mult
        return hours or None
    except ValueError:
        return None


def chunk_message(message, limit=1000):
    """Split at newline boundaries to stay under Pushover's cap."""
    chunks = []
    while message:
        if len(message) <= limit:
            chunks.append(message)
            break
        cut = message.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(message[:cut].rstrip())
        message = message[cut:].lstrip("\n")
    return chunks


def pushover(title, message, priority=0):
    token = os.environ.get("PUSHOVER_TOKEN", "")
    user = os.environ.get("PUSHOVER_USER_KEY", "")
    if not token or not user:
        log("PUSHOVER_TOKEN / PUSHOVER_USER_KEY not set; skipping")
        return
    chunks = chunk_message(message)
    for index, chunk in enumerate(chunks, 1):
        if index > 1:
            time.sleep(1)  # gaps keep multi-part notifications in order
        suffix = "" if len(chunks) == 1 else f" ({index}/{len(chunks)})"
        data = urllib.parse.urlencode(
            {
                "token": token,
                "user": user,
                "title": title + suffix,
                "message": chunk,
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
            raise RuntimeError(f"non-JSON response from pushover: {raw[:200]!r}")
        if body.get("status") != 1:
            raise RuntimeError(f"pushover rejected notification: {body}")
    log(f"pushover notification sent ({len(chunks)} part(s))")


def main():
    forgejo_token = os.environ.get("FORGEJO_TOKEN", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not forgejo_token or not github_token:
        raise RuntimeError("FORGEJO_TOKEN / GITHUB_TOKEN not set")

    mirrors = [r for r in forgejo_repos(forgejo_token) if r.get("mirror")]
    mirror_names = {r["name"].lower() for r in mirrors}
    log(f"{len(mirrors)} mirror(s) on Forgejo")

    now = datetime.now(timezone.utc)
    stale = []
    for repo in mirrors:
        stamp = repo.get("mirror_updated") or ""
        if not stamp:
            stale.append(f"{repo['full_name']}: never synced")
            continue
        age = now - datetime.fromisoformat(stamp)
        hours = age.total_seconds() / 3600
        if hours > STALE_HOURS:
            interval = parse_interval_hours(repo.get("mirror_interval"))
            expected = f", interval {interval:g}h" if interval else ""
            stale.append(f"{repo['full_name']}: last synced {hours:.0f}h ago{expected}")

    missing = []
    for repo in github_repos(github_token):
        if repo["name"].lower() in mirror_names:
            continue
        entry = repo["name"]
        if repo.get("archived"):
            entry += " (archived)"
        if repo.get("fork"):
            entry += " (fork)"
        missing.append(entry)

    log(f"{len(missing)} GitHub repo(s) not mirrored, {len(stale)} stale sync(s)")

    lines = []
    for label, entries in (("Missing on Forgejo", missing), ("Stale syncs", stale)):
        if not entries:
            continue
        lines.append(f"{label} ({len(entries)}):")
        lines.extend(f"- {e}" for e in entries[:MAX_ITEMS])
        if len(entries) > MAX_ITEMS:
            lines.append(f"... and {len(entries) - MAX_ITEMS} more")
        lines.append("")
    if lines:
        lines.append(f"OK: {len(mirrors) - len(stale)} mirror(s) fresh")
    else:
        lines.append(
            f"All good: {len(mirrors)} mirror(s) fresh, nothing missing from GitHub"
        )
    message = "\n".join(lines)

    if missing or stale:
        title = f"Forgejo mirrors: {len(missing)} missing, {len(stale)} stale"
        # missing mirrors need action; stale-only stays at normal priority
        priority = 1 if missing else 0
    else:
        title = f"Forgejo mirrors: {len(mirrors)} ok"
        priority = 0
    pushover(title, message, priority=priority)
    log(f"title: {title} (priority {priority})")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc!r}")
        try:
            pushover(
                "Forgejo mirror audit FAILED",
                f"error: {exc!r}",
                priority=1,
            )
        except Exception as notify_exc:  # noqa: BLE001
            log(f"failed to send failure notification: {notify_exc!r}")
        sys.exit(1)
