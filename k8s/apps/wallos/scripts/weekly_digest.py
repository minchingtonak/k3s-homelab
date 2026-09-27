#!/usr/bin/env python3
"""Weekly Wallos subscription digest.

Wallos's own notifications are per-subscription; its webhook channel
skips summaries entirely, and the public API has no trigger endpoint
(verified - see memory: wallos-period-summary-notification). This job
works around that by reading subscriptions read-only via the API and
pushing a digest of everything charging in the next 7 days, with a
per-currency total.

Always pushes (nothing-due weeks get a heartbeat); runtime errors
push a high-priority failure notice and exit 1.

The job runs from the wallos-weekly-digest-script ConfigMap baked by
the configMapGenerator in this kustomization (dagu pattern).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

WALLOS_URL = os.environ.get(
    "WALLOS_URL", "http://wallos.wallos.svc.cluster.local"
).rstrip("/")
try:
    WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
except ValueError:
    WINDOW_DAYS = 7
MAX_ITEMS = 15
USER_AGENT = "k3s-homelab-wallos-digest/1.0"


def log(msg):
    print(msg, flush=True)


def api(path, key):
    url = f"{WALLOS_URL}{path}"
    if "?" in url:
        url += "&" + urllib.parse.urlencode({"api_key": key})
    else:
        url += "?" + urllib.parse.urlencode({"api_key": key})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"non-JSON response from {url}: {raw[:200]!r}")


def currency_codes(key):
    try:
        data = api("/api/currencies/get_currencies.php", key)
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        log(f"currency lookup failed ({exc!r}); using ids")
        return {}
    entries = (data.get("currencies") if isinstance(data, dict) else data) or []
    return {
        str(e.get("id")): (e.get("code") or e.get("name") or "?")
        for e in entries
        if e.get("id") is not None
    }


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def money(value):
    try:
        text = f"{float(value):.2f}"
        return text.rstrip("0").rstrip(".") if "." in text else text
    except (TypeError, ValueError):
        return str(value)


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
    key = os.environ.get("WALLOS_API_KEY", "")
    if not key:
        raise RuntimeError("WALLOS_API_KEY not set")

    data = api("/api/subscriptions/get_subscriptions.php", key)
    subs = data.get("subscriptions") if isinstance(data, dict) else data
    if not isinstance(subs, list):
        raise RuntimeError(f"unexpected subscriptions payload: {data!r:.200}")
    codes = currency_codes(key)

    today = date.today()
    horizon = today + timedelta(days=WINDOW_DAYS)
    due = [
        s
        for s in subs
        if not s.get("inactive")
        and s.get("next_payment")
        and today.isoformat() <= s["next_payment"] <= horizon.isoformat()
    ]
    due.sort(key=lambda s: (s["next_payment"], s.get("name", "")))

    total_subs = len([s for s in subs if not s.get("inactive")])
    log(
        f"{total_subs} active subscription(s); "
        f"{len(due)} charging within {WINDOW_DAYS} day(s)"
    )
    for s in due:
        log(
            f"  {s['next_payment']}: {s.get('name')} "
            f"({s.get('price')} {codes.get(str(s.get('currency_id')), '?')})"
        )

    if not due:
        pushover(
            "Wallos: nothing due",
            f"No subscriptions charging in the next {WINDOW_DAYS} days.",
        )
        return

    lines = []
    totals = {}
    for s in due[:MAX_ITEMS]:
        code = codes.get(str(s.get("currency_id")), "?")
        price = to_float(s.get("price") or 0)
        totals[code] = totals.get(code, 0.0) + price
        lines.append(f"- {s['next_payment']}: {s.get('name')} ({money(price)} {code})")
    if len(due) > MAX_ITEMS:
        lines.append(f"... and {len(due) - MAX_ITEMS} more")
    summary = ", ".join(
        f"{money(total)} {code}" for code, total in sorted(totals.items())
    )
    lines.append("")
    lines.append(f"Total: {summary}")
    pushover(f"Wallos: {len(due)} charging this week", "\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc!r}")
        try:
            pushover(
                "Wallos digest FAILED",
                f"error: {exc!r}",
                priority=1,
            )
        except Exception as notify_exc:  # noqa: BLE001
            log(f"failed to send failure notification: {notify_exc!r}")
        sys.exit(1)
