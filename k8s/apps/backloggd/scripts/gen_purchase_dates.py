#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Generate purchase_dates.txt for the backloggd-steam-sync job.

Parses saved Steam "Purchase History" pages (account/history/, with enough
"Load more history" clicks to cover the years you care about) and maps each
transaction's game names to their purchase dates. Names are matched against
the live owned-games list (GetOwnedGames) with the same normalization the
sync job uses; only exact normalized matches are emitted — a wrong appid is
worse than a missing one, and anything unmatched can be added by hand.

Usage:
  STEAM_API_KEY=... STEAM_ID=... uv run gen_purchase_dates.py \
      "path/to/Account.html" [more.html ...] > purchase_dates.txt

Notes:
- "Purchase" rows are used; Gift Purchase/Refund/Market rows are skipped
  (gifted-away copies must not become your start dates).
- When a name appears in several purchases, the earliest date wins.
- The Steam history page does not include appids; F2P acquisitions have no
  purchase row at all — those games simply stay last-played-only in the
  report.
"""

import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

STEAM_API_KEY = os.environ["STEAM_API_KEY"]
STEAM_ID = os.environ["STEAM_ID"]

ROW = re.compile(
    r'<td class="wht_date">([A-Z][a-z]{2} \d{1,2}, \d{4})</td>\s*'
    r'<td[^>]*class="wht_items[^"]*"[^>]*>(.*?)</td>\s*'
    r'<td class="wht_type[^"]*">\s*<div>([^<]+)</div>',
    re.DOTALL,
)
ITEM_DIV = re.compile(r'<div style="clear: both">\s*(.*?)\s*</div>', re.DOTALL)
TAG = re.compile(r"<[^>]+>")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

USED_TYPES = ("Purchase",)  # Gift Purchase/Refund/Market excluded deliberately

# Token canonicalization shared with steam_sync.py (kept in sync
# deliberately): number words and multi-letter roman numerals to digits.
# Single-letter I/V/X stay untouched — mapping them would collide real
# titles ("Mega Man X" vs "Mega Man 10").
_NUMERALS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10",
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7",
    "viii": "8", "ix": "9", "xi": "11", "xii": "12", "xiii": "13",
    "xiv": "14", "xv": "15", "xvi": "16", "xvii": "17", "xviii": "18",
    "xix": "19", "xx": "20",
}


def normalize_title(value):
    """Same normalization as steam_sync.py (kept in sync deliberately)."""
    value = value.replace("™", "").replace("®", "").replace("©", "")
    value = re.sub(r"\s*\(\s*(?:19|20)\d{2}\s*\)\s*$", "", value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return " ".join(_NUMERALS.get(token, token) for token in value.split())


def owned_games():
    """Fetch the owned-games list (appid + name) from the Steam Web API."""
    url = (
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?"
        + urllib.parse.urlencode(
            {
                "key": STEAM_API_KEY,
                "steamid": STEAM_ID,
                "include_appinfo": "true",
                "format": "json",
            }
        )
    )
    if not url.startswith("https://"):
        raise SystemExit(f"refusing non-https URL: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "gen-purchase-dates/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"steam owned-games -> HTTP {exc.code}") from exc
    games = (body.get("response") or {}).get("games") or []
    if not games:
        raise SystemExit("owned-games list came back empty — check key/steamid/privacy")
    return games


def read_page(path):
    """Read one saved history page, with an actionable error when missing."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise SystemExit(f"cannot read {path!r}: {exc}") from exc


def parse_history(paths):
    """Yield (iso_date, normalized_item_name) from Purchase-type rows."""
    for path in paths:
        html = read_page(path)
        for date_raw, items_html, type_raw in ROW.findall(html):
            if type_raw.strip() not in USED_TYPES:
                continue
            month, day, year = date_raw.replace(",", "").split()
            iso = f"{year}-{MONTHS[month]:02d}-{int(day):02d}"
            for item in ITEM_DIV.findall(items_html):
                name = TAG.sub("", item).strip()
                if name:
                    yield iso, normalize_title(name)


def main():
    """Exact match first; bundle rows list long marketing names (e.g.
    "Resident Evil 0 / biohazard 0 HD REMASTER" vs owned "Resident Evil 0"),
    so fall back to word-aligned prefix matching in either direction —
    longest candidate wins so "Resident Evil 0" beats "Resident Evil".
    """
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    purchases = {}
    for iso, name in parse_history(sys.argv[1:]):
        if name not in purchases or iso < purchases[name]:
            purchases[name] = iso

    games = owned_games()
    names = {normalize_title(g["name"]): g["appid"] for g in games}

    def is_word_prefix(short, long_):
        a, b = short.split(), long_.split()
        return len(a) < len(b) and b[: len(a)] == a

    def resolve(item):
        """Owned name for a history item: exact, else longest prefix relation."""
        if item in names:
            return item
        candidates = [
            owned
            for owned in names
            if is_word_prefix(owned, item) or is_word_prefix(item, owned)
        ]
        return max(candidates, key=len) if candidates else None

    result = {}
    for item, iso in purchases.items():
        owned = resolve(item)
        if owned and (owned not in result or iso < result[owned][1]):
            result[owned] = (names[owned], iso)

    matched = len(result)
    lines = [
        f"{appid} {iso}" for appid, iso in sorted(result.values(), key=lambda x: x[0])
    ]
    print("\n".join(lines))
    print(f"# {matched}/{len(games)} owned games matched a purchase row", file=sys.stderr)


if __name__ == "__main__":
    main()
