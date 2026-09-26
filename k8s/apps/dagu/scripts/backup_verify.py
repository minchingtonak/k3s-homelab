#!/usr/bin/env python3
"""Weekly backup verification.

Three checks, covering the two backup pipelines:
1. dagu nightly PBS backup DAG - the latest run must be recent and
   every step (including the local-PBS step that has
   continue_on: failure, so a dead local backup still "succeeds" the
   DAG) must have finished successfully.
2. Longhorn volume backups - every live volume whose recurring-job
   group includes a backup task must have a backup no older than its
   schedule (derived from the recurring job's cron) plus grace;
   volumes with no backup task scheduled are counted, not alarmed,
   and backup records of deleted volumes are ignored as orphans.
3. PVC name mapping so the report reads like the cluster.

Always pushes a Pushover summary; problems are priority 1, clean
weeks are the normal heartbeat. Runtime errors push a high-priority
failure notice and exit 1.

The job runs from the backup-verify-script ConfigMap baked by the
configMapGenerator in this kustomization (dagu pattern).
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DAGU_URL = os.environ.get("DAGU_URL", "http://dagu.dagu.svc.cluster.local")
# Overridable for local testing (kubectl proxy); defaults to the
# in-cluster ServiceAccount mount.
K8S_API = os.environ.get("KUBERNETES_API", "https://kubernetes.default.svc")
DAGU_MAX_AGE_HOURS = 32.0
STALE_GRACE_HOURS = 12.0
MAX_ITEMS = 15
USER_AGENT = "k3s-homelab-backup-verify/1.0"


def log(msg):
    print(msg, flush=True)


def fetch_json(url, headers=None, context=None):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=30, context=context) as resp:
        raw = resp.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"non-JSON response from {url}: {raw[:200]!r}")


def parse_ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def k8s_context():
    """Headers + ssl context for the Kubernetes API (in-cluster SA)."""
    base = "/var/run/secrets/kubernetes.io/serviceaccount"
    headers = {}
    context = None
    try:
        with open(f"{base}/token") as fh:
            headers["Authorization"] = f"Bearer {fh.read().strip()}"
        context = ssl.create_default_context(cafile=f"{base}/ca.crt")
    except OSError:
        log("no service account token found; assuming anonymous API")
    return headers, context


def k8s_list(path, headers, context):
    """One collection endpoint, following continue tokens."""
    items, params = [], {}
    while True:
        url = f"{K8S_API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        batch = fetch_json(url, headers, context)
        items.extend(batch.get("items") or [])
        token = batch.get("metadata", {}).get("continue")
        if not token:
            return items
        params = {"continue": token}


def check_dagu(problems):
    data = fetch_json(f"{DAGU_URL}/api/v1/dags/backup")
    run = data.get("latestDAGRun") or {}
    if not run:
        problems.append("dagu backup DAG: no runs recorded")
        return None, 0.0
    finished = parse_ts(run["finishedAt"]) if run.get("finishedAt") else None
    age = (
        (datetime.now(timezone.utc) - finished).total_seconds() / 3600
        if finished
        else None
    )
    if age is None or age > DAGU_MAX_AGE_HOURS:
        problems.append(
            f"dagu backup DAG: last run "
            f"{'never finished' if age is None else f'{age:.0f}h ago'}"
            f" (expected < {DAGU_MAX_AGE_HOURS:.0f}h)"
        )
    bad = [
        n["step"]["name"]
        for n in run.get("nodes") or []
        if n.get("statusLabel") not in ("succeeded", "skipped")
    ]
    for name in bad:
        problems.append(f"dagu backup DAG step {name!r} did not succeed")
    label = run.get("statusLabel")
    if label and label not in ("succeeded", "finished"):
        problems.append(f"dagu backup DAG run status: {label}")
    return finished, age


def check_longhorn(problems):
    headers, context = k8s_context()
    backups = k8s_list("/apis/longhorn.io/v1beta2/backups", headers, context)
    pvcs = k8s_list("/api/v1/persistentvolumeclaims", headers, context)
    volumes = k8s_list("/apis/longhorn.io/v1beta2/volumes", headers, context)
    jobs = k8s_list("/apis/longhorn.io/v1beta2/recurringjobs", headers, context)
    friendly = {
        (p["spec"].get("volumeName") or ""): (
            f"{p['metadata']['namespace']}/{p['metadata']['name']}"
        )
        for p in pvcs
    }

    # group -> most frequent backup-task interval (snapshots do not
    # leave the cluster, so only backup tasks count), from the cron
    # expression: daily = 24h, weekly = 168h, monthly = 31d.
    def cron_interval_hours(cron):
        fields = cron.split()
        if len(fields) != 5:
            return None
        _, _, dom, _, dow = fields
        if dom == "*" and dow == "*":
            return 24.0
        if dom == "*":
            return 168.0
        if dow == "*":
            return 31 * 24.0
        return None  # exotic cron: be conservative downstream

    group_hours = {}
    for job in jobs:
        spec = job.get("spec") or {}
        if spec.get("task") != "backup":
            continue
        hours = cron_interval_hours(spec.get("cron", ""))
        for group in spec.get("groups") or []:
            if hours is not None:
                prior = group_hours.get(group)
                if prior is None or hours < prior:
                    group_hours[group] = hours

    # volume -> groups enabled via recurring-job-group.longhorn.io/*
    live = {}
    for vol in volumes:
        name = vol["metadata"]["name"]
        groups = [
            key.split("/", 1)[1]
            for key in (vol["metadata"].get("labels") or {})
            if key.startswith("recurring-job-group.longhorn.io/")
        ]
        live[name] = max(
            (group_hours[g] for g in groups if g in group_hours),
            default=None,
        )

    latest = {}
    for b in backups:
        status = b.get("status") or {}
        vol, when = status.get("volumeName"), status.get("backupCreatedAt")
        if not vol or not when:
            continue
        if vol not in latest or when > latest[vol]:
            latest[vol] = when

    now = datetime.now(timezone.utc)
    fresh, stale, orphaned, unscheduled = 0, [], 0, 0
    for vol, limit in sorted(live.items()):
        name = friendly.get(vol) or vol
        when = latest.get(vol)
        if limit is None:
            # no backup task scheduled for this volume's groups
            unscheduled += 1
            continue
        threshold = limit + STALE_GRACE_HOURS
        if when is None:
            problems.append(f"longhorn: {name}: never backed up")
            continue
        age_h = (now - parse_ts(when)).total_seconds() / 3600
        if age_h <= threshold:
            fresh += 1
        else:
            stale.append(
                f"{name}: last backup {age_h:.0f}h ago "
                f"(schedule allows {threshold:.0f}h)"
            )
    for entry in stale:
        problems.append(f"longhorn: {entry}")
    orphaned = len([v for v in latest if v not in live])
    return fresh, stale, unscheduled, orphaned


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
    problems = []
    finished, dagu_age = check_dagu(problems)
    fresh, stale, unscheduled, orphans = check_longhorn(problems)
    log(
        f"dagu last run: {dagu_age:.0f}h ago"
        if dagu_age is not None
        else "dagu last run: none"
    )
    log(
        f"longhorn: {fresh} fresh, {len(stale)} stale, "
        f"{unscheduled} unscheduled, {orphans} orphaned"
    )
    for p in problems:
        log(f"  PROBLEM: {p}")

    lines = []
    if problems:
        lines.append("Problems:")
        lines.extend(f"- {p}" for p in problems[:MAX_ITEMS])
        if len(problems) > MAX_ITEMS:
            lines.append(f"... and {len(problems) - MAX_ITEMS} more")
        title = f"Backup check: {len(problems)} problem(s)"
        priority = 1
    else:
        lines.append(
            f"Backups OK: dagu nightly {dagu_age:.0f}h old, {fresh} volume(s) fresh"
        )
        title = "Backup check: OK"
        priority = 0
    if unscheduled:
        lines.append(f"({unscheduled} volume(s) without a backup schedule)")
    if orphans:
        lines.append(f"({orphans} orphaned backup record(s) ignored)")
    pushover(title, "\n".join(lines), priority=priority)
    log(f"title: {title} (priority {priority})")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc!r}")
        try:
            pushover(
                "Backup verify FAILED",
                f"error: {exc!r}",
                priority=1,
            )
        except Exception as notify_exc:  # noqa: BLE001
            log(f"failed to send failure notification: {notify_exc!r}")
        sys.exit(1)
