#!/usr/bin/env python3
"""Trigger the Paperless-ngx sanity check via the REST API, wait for
it to finish, and report the outcome via Pushover.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get(
    "PAPERLESS_BASE_URL",
    "http://keda-add-ons-http-interceptor-proxy.keda.svc.cluster.local:8080",
)
HOST = os.environ.get("PAPERLESS_HOST", "paperless.item.fyi")
TASK_TYPE = os.environ.get("SANITY_TASK_TYPE", "sanity_check")
USER_AGENT = "paperless-sanity-check/1.0"


def env_int(name, default):
    value = os.environ.get(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"invalid {name}={value!r}, expected an integer")


POLL_INTERVAL = env_int("POLL_INTERVAL", 15)
MAX_WAIT = env_int("MAX_WAIT", 1200)
COLD_START_MAX_WAIT = env_int("COLD_START_MAX_WAIT", 180)
RETRYABLE_HTTP = (502, 503, 504)


def log(msg):
    print(msg, flush=True)


def retry_delay(attempt, headers):
    """Seconds to wait before a retry: honour Retry-After, else back off."""
    retry_after = (headers or {}).get("Retry-After", "")
    try:
        return min(float(retry_after), 60.0)
    except ValueError:
        return min(5 * 2 ** (attempt - 1), 30)


def api(method, path, json_body=None):
    """One Paperless REST API call through the KEDA interceptor."""
    headers = {
        "User-Agent": USER_AGENT,
        "Host": HOST,
        "Accept": "application/json",
    }
    token = os.environ.get("PAPERLESS_TOKEN", "")
    if token:
        headers["Authorization"] = "Token " + token
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method, headers=headers
    )

    deadline = time.monotonic() + COLD_START_MAX_WAIT
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read().decode()
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP:
                raise
            delay = retry_delay(attempt, exc.headers)
            if time.monotonic() + delay > deadline:
                raise
            log(
                f"HTTP {exc.code} on {method} {path}; "
                f"retrying in {delay:.0f}s (cold start?)"
            )
        except OSError as exc:
            # connection refused/reset while endpoints flap during scale-up
            delay = retry_delay(attempt, None)
            if time.monotonic() + delay > deadline:
                raise
            log(
                f"{exc!r} on {method} {path}; "
                f"retrying in {delay:.0f}s (cold start?)"
            )
        time.sleep(delay)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # e.g. the interceptor returning an HTML error page
        raise RuntimeError(f"non-JSON response from {HOST}{path}: {raw[:200]!r}") from exc


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
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"non-JSON response from pushover: {raw[:200]!r}") from exc
        if body.get("status") != 1:
            raise RuntimeError(f"pushover rejected notification: {body}")
    log(f"pushover notification sent ({len(chunks)} part(s))")


def run_sanity_check():
    log(f"requesting {TASK_TYPE!r} task on {HOST} ...")
    resp = api("POST", "/api/tasks/run/", {"task_type": TASK_TYPE})
    task_id = resp.get("task_id")
    if not task_id:
        raise RuntimeError(f"no task_id in response: {resp!r}")
    log(f"task queued: {task_id}")

    deadline = time.monotonic() + MAX_WAIT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        path = "/api/tasks/?task_id=" + urllib.parse.quote(task_id)
        results = api("GET", path).get("results", [])
        if not results:
            log("task not visible yet; waiting")
            continue
        task = results[0]
        status = task.get("status")
        log(f"status: {status}")
        if status in ("success", "failure", "revoked"):
            return task
    raise TimeoutError(f"sanity check did not finish within {MAX_WAIT}s")


def main():
    task = run_sanity_check()
    status = task["status"]
    duration = task.get("duration_seconds")
    result = json.dumps(task.get("result_data"))
    message = ""
    if duration:
        message = f"Completed in {duration:.0f}s."
    if task.get("result_data"):
        message += " Result: " + result
    if status == "success":
        pushover("Paperless sanity check passed", message or "Completed.")
        return
    pushover(
        f"Paperless sanity check {status.upper()}",
        f"Status: {status}. " + message,
        priority=1,
    )
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - report anything, fail job
        log(f"ERROR: {exc!r}")
        try:
            pushover(
                "Paperless sanity check FAILED",
                f"error: {exc!r}",
                priority=1,
            )
        except Exception as notify_exc:  # noqa: BLE001
            log(f"failed to send failure notification: {notify_exc!r}")
        sys.exit(1)
