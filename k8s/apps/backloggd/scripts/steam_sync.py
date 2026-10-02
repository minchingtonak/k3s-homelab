#!/usr/bin/env python3
"""Weekly Steam -> Backloggd tracking nag.

Steam records no session history, but it does expose, per owned game, total
playtime (``playtime_forever``), the unix time of the last play session
(``rtime_last_played``), and per-game achievement unlock timestamps. This job
combines the three into the weekly "log your games" reminder:

1. ``IPlayerService/GetOwnedGames`` — the whole library in one call. Games
   whose last play falls on/after BACKFILL_FROM and that clear
   MIN_PLAYTIME_MINUTES are candidates; anything older is invisible until the
   window is ratcheted back.
2. Backloggd — no public API, a small Patreon-funded site, so this mirrors the
   posture of the community backloggd-mcp project: act only as the signed-in
   user on their own data, with an honest User-Agent, strictly serialized
   requests spaced by BACKLOGGD_MIN_INTERVAL, a hard page cap, and an
   immediate abort on any 429 (a weekly run costs about a dozen requests).
   Auth is a standard Rails/Devise form login that self-renews each run.
3. ``ISteamUserStats/GetPlayerAchievements`` — for candidates Backloggd does
   not know yet, the first achievement unlock approximates the start date
   Steam does not record; last play remains the authoritative end date.
   An optional user-provided purchase-date table (purchase_dates.txt, a
   plain ConfigMap — purchase dates are not exposed by any public API)
   shows as "bought <date>" on every candidate and stands in for the start
   only when no achievement signal exists.

Matching a Steam name to the Backloggd library is normalized (case,
diacritics, punctuation, trademark glyphs) then exact, then fuzzy at 0.90 —
the threshold is deliberately high because a false "already tracked" silently
hides a nag, while a false nag merely costs a glance. Fuzzy matches are
logged. Untracked candidates nag every week until they appear in Backloggd,
and tracked candidates nag while they sit on the playing shelf — the entry
exists but awaits a completion (or retired/shelved) date. The list is
self-healing and the job is stateless.

BACKFILL_FROM is the ratchet: edit it in the manifest (a plain env var, so
the change is a reviewable git commit) to widen the window back through
history once recent years are logged. Dates in the report use REPORT_TZ day
granularity — Steam only gives unix seconds, and the timezone matters when
an evening unlock lands on the next UTC day.
"""

import contextlib
import http.cookiejar
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser

# Honest and identifiable: this is an automated personal tool, not a browser.
USER_AGENT = "backloggd-steam-sync/1.0 (personal weekly tracker nag)"
FUZZY_THRESHOLD = 0.90


def log(msg):
    """Print with flush so job logs stream in real time."""
    print(msg, flush=True)


def env_str(name, default=None, required=False):
    """Env var reader; required ones raise, optional default to empty."""
    value = os.environ.get(name) or default
    if not value:
        if required:
            raise RuntimeError(f"{name} is not set (expected from the sync secret)")
        return ""
    return value


def env_int(name, default):
    """Read an integer env var with a default; fatal on garbage."""
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        raise RuntimeError(
            f"invalid {name}={os.environ.get(name)!r}, expected an integer"
        ) from None


def env_float(name, default):
    """Read a float env var with a default; fatal on garbage."""
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        raise RuntimeError(
            f"invalid {name}={os.environ.get(name)!r}, expected a number"
        ) from None


STEAM_API_KEY = env_str("STEAM_API_KEY", required=True)
STEAM_ID = env_str("STEAM_ID", required=True)
if not STEAM_ID.isdigit():
    raise RuntimeError(f"STEAM_ID must be a steamid64 (digits), got {STEAM_ID!r}")
STEAM_API_URL = env_str("STEAM_API_URL", "https://api.steampowered.com").rstrip("/")
STEAM_MIN_INTERVAL = env_float("STEAM_MIN_INTERVAL", 0.4)

BACKLOGGD_URL = env_str("BACKLOGGD_URL", "https://backloggd.com").rstrip("/")
BACKLOGGD_USERNAME = env_str("BACKLOGGD_USERNAME", required=True)
BACKLOGGD_PASSWORD = env_str("BACKLOGGD_PASSWORD", required=True)
BACKLOGGD_MIN_INTERVAL = env_float("BACKLOGGD_MIN_INTERVAL", 5.0)
BACKLOGGD_MAX_PAGES = env_int("BACKLOGGD_MAX_PAGES", 50)

BACKFILL_FROM = env_str("BACKFILL_FROM", "2025-01-01")
REPORT_TZ = env_str("REPORT_TZ", "UTC")
PURCHASE_DATES_FILE = env_str("PURCHASE_DATES_FILE", "/data/purchase_dates.txt")
try:
    CUTOFF_EPOCH = int(
        datetime.strptime(BACKFILL_FROM, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
except ValueError:
    raise RuntimeError(
        f"invalid BACKFILL_FROM={BACKFILL_FROM!r}, expected YYYY-MM-DD"
    ) from None

MIN_PLAYTIME_MINUTES = env_int("MIN_PLAYTIME_MINUTES", 30)
MAX_REPORT = env_int("MAX_REPORT", 25)
ACHIEVEMENT_FETCH_LIMIT = env_int("ACHIEVEMENT_FETCH_LIMIT", 60)
# Apps with no Backloggd database entry (open betas, benchmark tools) can
# never be tracked and would nag forever.
IGNORE_APPIDS = {
    int(token)
    for token in env_str("IGNORE_APPIDS", "").replace(",", " ").split()
    if token.isdigit()
}

# Canonical form for small number words: IGDB titles differ from Steam here
# ("Resident Evil Zero" vs "Resident Evil 0").
_NUMERALS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10",
}

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


def https_url(url):
    """Guard every outbound request to the https scheme explicitly."""
    if not url.startswith("https://"):
        raise RuntimeError(f"refusing non-https URL: {url!r}")
    return url


def load_purchase_dates():
    """Read the optional appid -> purchase-date table (user-provided;
    produced from Steam's Export Steam Data or saved history pages)."""
    dates = {}
    try:
        with open(PURCHASE_DATES_FILE, encoding="utf-8") as handle:
            for line in handle:
                parts = line.split("#", 1)[0].split()
                if len(parts) == 2 and parts[0].isdigit():
                    dates[int(parts[0])] = parts[1]
    except FileNotFoundError:
        pass
    return dates


def fmt_date(epoch):
    """Epoch seconds -> report-timezone YYYY-MM-DD (UTC if zone unknown)."""
    try:
        tz = zoneinfo.ZoneInfo(REPORT_TZ)
    except zoneinfo.ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime.fromtimestamp(epoch, tz=tz).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Steam Web API (quota 100k/day; this job uses a handful per week)


def steam_request(path, params):
    """GET a Steam Web API path as JSON, retrying transient errors."""
    url = https_url(f"{STEAM_API_URL}{path}?{urllib.parse.urlencode(params)}")
    for attempt in range(3):
        try:
            with _OUT_OPENER.open(url, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"steam {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"steam {path} unreachable: {exc.reason}") from exc
    raise RuntimeError(f"steam {path}: retries exhausted")


def fetch_steam_library():
    """Return the entire owned-games list in a single call."""
    data = steam_request(
        "/IPlayerService/GetOwnedGames/v1/",
        {
            "key": STEAM_API_KEY,
            "steamid": STEAM_ID,
            "include_appinfo": "true",
            "include_played_free_games": "true",
            "format": "json",
        },
    )
    response = data.get("response") or {}
    games = response.get("games") or []
    if not games:
        raise RuntimeError(
            "Steam returned an empty library — check that the profile's "
            "'Game details' privacy is Public and the API key matches the account"
        )
    return games


def achievement_window(appid):
    """First/last achievement unlock time and completion ratio, or None."""
    try:
        data = steam_request(
            "/ISteamUserStats/GetPlayerAchievements/v1/",
            {"key": STEAM_API_KEY, "steamid": STEAM_ID, "appid": appid, "l": "english"},
        )
    except RuntimeError as exc:
        # Most 400/403s here simply mean the game has no achievement schema.
        log(f"  achievements unavailable for appid {appid}: {exc}")
        return None
    achievements = (data.get("playerstats") or {}).get("achievements") or []
    if not achievements:
        return None
    unlocked = sorted(
        a["unlocktime"]
        for a in achievements
        if a.get("achieved") and a.get("unlocktime", 0) > 0
    )
    return {
        "first": unlocked[0] if unlocked else None,
        "last": unlocked[-1] if unlocked else None,
        "ratio": len(unlocked) / len(achievements),
    }


# --------------------------------------------------------------------------
# Backloggd (no public API: own-data reads as the signed-in user, spaced and
# capped; 429 aborts all Backloggd traffic for this run)

class _DefaultHeaders(urllib.request.BaseHandler):
    """Attach default headers to every request an opener makes."""

    def __init__(self, headers):
        self._headers = headers

    def _add(self, request):
        for name, value in self._headers.items():
            if not request.has_header(name):
                request.add_unredirected_header(name, value)
        return request

    def http_request(self, request):
        """Decorate plain-http requests with the defaults."""
        return self._add(request)

    def https_request(self, request):
        """Decorate https requests with the defaults."""
        return self._add(request)


_JAR = http.cookiejar.CookieJar()
_BACKLOGGD_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_JAR),
    _DefaultHeaders(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
    ),
)
_OUT_OPENER = urllib.request.build_opener(
    _DefaultHeaders({"User-Agent": USER_AGENT})
)
_THROTTLE = {"last": 0.0}  # mutable holder avoids a global statement


class BackloggdRejected(RuntimeError):
    """Backloggd-side refusals: rate limiting, credentials, bot challenges."""


def _backloggd_throttle():
    """Serialize Backloggd traffic by enforcing the minimum request gap."""
    wait = _THROTTLE["last"] + BACKLOGGD_MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _THROTTLE["last"] = time.monotonic()


def backloggd_request(path, form=None, _retried_429=False):
    """One spaced Backloggd request (HTML out, carries the session cookie)."""
    _backloggd_throttle()
    url = https_url(BACKLOGGD_URL + path)
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    try:
        with _BACKLOGGD_OPENER.open(url, data=data, timeout=40) as resp:
            return resp.geturl(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code == 429:
            # Honor the limiter like a well-behaved client: sleep what it
            # asks (bounded) and retry once. A second 429 aborts the run —
            # hammering an endpoint that is already limiting you is what
            # escalates into a blocked account.
            retry_after = _retry_after_seconds(exc.headers)
            if not _retried_429 and retry_after is not None and retry_after <= 180:
                log(
                    f"backloggd 429 on {path}; sleeping {retry_after:.0f}s "
                    "per Retry-After and retrying once"
                )
                time.sleep(retry_after + 2)
                return backloggd_request(path, form=form, _retried_429=True)
            raise BackloggdRejected(
                "backloggd rate-limited us (429); stopping all traffic this run"
            ) from exc
        if exc.code in (403, 503) and "challenge" in body.lower():
            raise BackloggdRejected(
                "backloggd served a bot challenge; refusing to evade it"
            ) from exc
        if exc.code == 422:
            # Devise re-renders the form with a 422 on a rejected login;
            # callers inspect the body rather than treat it as transport error.
            return "", body
        raise RuntimeError(f"backloggd {path} -> HTTP {exc.code}") from exc


_LOGIN_TOKEN = re.compile(
    r'<form[^>]*action="/users/sign_in/?"[^>]*>.*?'
    r'name="authenticity_token"[^>]*value="([^"]+)"',
    re.DOTALL,
)
_META_TOKEN = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
_ANY_TOKEN = re.compile(r'name="authenticity_token"[^>]*value="([^"]+)"')


def _retry_after_seconds(headers):
    for name in ("Retry-After", "RateLimit-Reset"):
        value = headers.get(name) if headers else None
        if value:
            try:
                return float(value)
            except ValueError:
                continue
    return None


def backloggd_login():
    """Form-login as the configured user; raise if the session does not stick."""
    _, body = backloggd_request("/users/sign_in")
    # The login form's action carries a trailing slash (/users/sign_in/) and
    # Rails also exposes the same session token as a meta tag; both are more
    # stable than scraping an arbitrary form's hidden field (a token from a
    # *different* form on the page gets rejected with 422).
    token = _LOGIN_TOKEN.search(body) or _META_TOKEN.search(body) or _ANY_TOKEN.search(body)
    if not token:
        raise RuntimeError(
            "could not read the authenticity_token from Backloggd's login form "
            "(markup change?)"
        )
    _, body = backloggd_request(
        "/users/sign_in",
        form={
            "authenticity_token": token.group(1),
            "user[login]": BACKLOGGD_USERNAME,
            "user[password]": BACKLOGGD_PASSWORD,
            "user[remember_me]": "1",
        },
    )
    if "Invalid Login or password" in body:
        raise BackloggdRejected(
            "Backloggd rejected those credentials — check BACKLOGGD_USERNAME "
            "(the login name) and BACKLOGGD_PASSWORD; not retrying to avoid a "
            "temporary account lock"
        )
    # Identity probe: /settings/ redirects to the login form when signed out.
    final_url, _ = backloggd_request("/settings/")
    if final_url.rstrip("/").endswith("/users/sign_in"):
        raise BackloggdRejected("Backloggd login did not stick (credentials?)")


class LibraryPageParser(HTMLParser):
    """Collect (game_id, slug, title) from a `.card.game-cover[game_id]` grid.

    Cards nest other elements, so depth counting decides when a card ends;
    void elements (img/br/...) never get a closing tag and must not count.
    """

    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
    _SLUG = re.compile(r"^/games/([^/]+)/$")
    _ID = re.compile(r"^[0-9]+$")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.entries = []
        self._cur = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        game_id = attr.get("game_id") or ""
        if self._cur is None:
            if (
                tag == "div"
                and "card" in classes
                and "game-cover" in classes
                and self._ID.match(game_id)
            ):
                self._cur = {"id": int(game_id), "slug": None, "title": None}
                self._depth = 1
            return
        if tag not in self.VOID:
            self._depth += 1
        if tag == "a" and "cover-link" in classes and self._cur["slug"] is None:
            match = self._SLUG.match(attr.get("href") or "")
            if match:
                self._cur["slug"] = match.group(1)
        elif tag == "img" and self._cur["title"] is None:
            alt = (attr.get("alt") or "").strip()
            if alt:
                self._cur["title"] = alt

    def handle_endtag(self, tag):
        if self._cur is not None and tag not in self.VOID:
            self._depth -= 1
            if self._depth <= 0:
                self.entries.append(self._cur)
                self._cur = None

    def close(self):
        super().close()
        if self._cur is not None:
            self.entries.append(self._cur)
            self._cur = None


SHELVES = ("playing", "played", "backlog", "wishlist")


def fetch_backloggd_library():
    """Normalized titles + slugs for every game on any shelf, plus the
    normalized names currently on the playing shelf (the "complete this
    entry when beaten" nag list). Own data only.

    The default /u/<user>/games/ view hides games that sit ONLY on the
    playing shelf, so each shelf is queried explicitly and the results are
    unioned by game id (a game can be on several shelves at once).
    """
    titles = set()
    slugs = set()
    playing = set()
    seen_ids = set()
    username = urllib.parse.quote(BACKLOGGD_USERNAME, safe="")
    for shelf in SHELVES:
        signature = None
        exhausted = False
        shelf_count = 0
        for page in range(1, BACKLOGGD_MAX_PAGES + 1):
            path = f"/u/{username}/games/added/type:{shelf}/" + (
                f"?page={page}" if page > 1 else ""
            )
            _, body = backloggd_request(path)
            parser = LibraryPageParser()
            parser.feed(body)
            parser.close()
            if not parser.entries:
                exhausted = True
                break
            page_signature = tuple(sorted(e["id"] for e in parser.entries))
            if page_signature == signature:
                exhausted = True
                break  # out-of-range pages re-serve the last page indefinitely
            signature = page_signature
            for entry in parser.entries:
                if entry["id"] in seen_ids:
                    continue
                seen_ids.add(entry["id"])
                shelf_count += 1
                if entry["title"]:
                    title = normalize_title(entry["title"])
                    titles.add(title)
                    if shelf == "playing":
                        playing.add(title)
                if entry["slug"]:
                    # Trailing "--N" is Backloggd's same-name disambiguator
                    # (resident-evil-4--1), not part of the title.
                    slug = re.sub(r"--\d+$", "", entry["slug"])
                    slugs.add(normalize_title(slug))
                    if shelf == "playing":
                        playing.add(normalize_title(slug))
        if not exhausted:
            log(f"WARNING: hit the {BACKLOGGD_MAX_PAGES}-page cap on {shelf}")
        log(
            f"backloggd {shelf} shelf: {shelf_count} games "
            f"({len(seen_ids)} tracked total)"
        )
    return titles, slugs, playing


def normalize_title(value):
    """Canonical form for matching: case, diacritics, punctuation, trademark
    glyphs, a trailing "(YYYY)" disambiguation year (Steam renames old games
    when remakes ship: "Modern Warfare 2 (2009)"), and zero-ten number words
    to digits (IGDB's "Resident Evil Zero" vs Steam's "Resident Evil 0").
    """
    # Trademark glyphs must go before NFKD: compatibility decomposition turns
    # "\u2122" into the letters "tm", which would fuse into the title.
    value = value.replace("™", "").replace("®", "").replace("©", "")
    value = re.sub(r"\s*\(\s*(?:19|20)\d{2}\s*\)\s*$", "", value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return " ".join(_NUMERALS.get(token, token) for token in value.split())


def _digit_tokens(normalized):
    return frozenset(t for t in normalized.split() if t.isdigit())


def match_status(name, titles, slugs):
    """None when untracked; otherwise how it matched (for logs/audit).

    Fuzzy matching needs a digit-token guard: "System Shock 2" vs a tracked
    "System Shock" scores 0.96, and silently suppressing a numbered sequel's
    nag is the one error this matcher must never make (a false nag costs a
    glance; a false suppression hides a game indefinitely). Differing digit
    tokens therefore force an exact match.
    """
    normalized = normalize_title(name)
    if normalized in titles or normalized in slugs:
        return "exact"
    digits = _digit_tokens(normalized)
    best, best_title = 0.0, ""
    for title in titles:
        if _digit_tokens(title) != digits:
            continue
        ratio = SequenceMatcher(None, normalized, title).ratio()
        if ratio > best:
            best, best_title = ratio, title
    if best >= FUZZY_THRESHOLD:
        return f"fuzzy {best:.2f} vs {best_title!r}"
    return None


# --------------------------------------------------------------------------
# Pushover (same shape as the seerr unfulfilled-requests job)


def chunk_message(message, limit=1000):
    """Split at newline boundaries to stay under Pushover's cap."""
    if len(message) <= limit:
        return [message]
    chunks = []
    while message:
        cut = message.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(message[:cut].rstrip())
        message = message[cut:].lstrip("\n")
    return chunks


def pushover(title, message, priority=0):
    """Send via Pushover, chunked and ordered."""
    token = env_str("PUSHOVER_TOKEN", "")
    user = env_str("PUSHOVER_USER_KEY", "")
    if not token or not user:
        log("PUSHOVER_TOKEN / PUSHOVER_USER_KEY not set; skipping")
        return
    chunks = chunk_message(message)
    endpoint = https_url(PUSHOVER_API_URL)
    for index, chunk in enumerate(chunks, 1):
        if index > 1:
            # Separate notifications can arrive shuffled without a gap.
            time.sleep(1)
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
        with _OUT_OPENER.open(endpoint, data=data, timeout=30) as resp:
            raw = resp.read().decode()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"non-JSON response from pushover: {raw[:200]!r}") from None
        if body.get("status") != 1:
            raise RuntimeError(f"pushover rejected notification: {body}")
    log(f"pushover notification sent ({len(chunks)} part(s))")


# --------------------------------------------------------------------------
# Report


def describe(game):
    """One report line per game."""
    hours = game["playtime_forever"] / 60
    name = game["name"]
    last_played = game.get("rtime_last_played", 0)
    line = f"• {name} — {hours:.1f} h total"
    purchase = game.get("_purchase")
    if purchase:
        line += f"; bought {purchase}"
    ach = game.get("_ach")
    if ach and ach["first"]:
        # First unlock approximates the start date; last play is the
        # authoritative end. The last unlock hints at when the run was
        # effectively over — achievements stop while play continues in
        # ongoing games (live-service titles especially).
        line += f"; played {fmt_date(ach['first'])} → {fmt_date(last_played)}"
        if ach["last"] and fmt_date(ach["last"]) != fmt_date(last_played):
            line += f", last achievement {fmt_date(ach['last'])}"
        line += f" ({ach['ratio']:.0%})"
    else:
        line += f"; last played {fmt_date(last_played)}"
        if ach:
            line += " (0%)"  # schema exists, nothing unlocked
    return line


def main():
    """Run the weekly Steam -> Backloggd comparison and notify."""
    games = fetch_steam_library()
    candidates = [
        g
        for g in games
        if g.get("rtime_last_played", 0) >= CUTOFF_EPOCH
        and g.get("playtime_forever", 0) >= MIN_PLAYTIME_MINUTES
        and g["appid"] not in IGNORE_APPIDS
    ]
    brief = [
        g
        for g in games
        if g.get("rtime_last_played", 0) >= CUTOFF_EPOCH
        and 0 < g.get("playtime_forever", 0) < MIN_PLAYTIME_MINUTES
    ]
    log(
        f"steam: {len(games)} games, {len(candidates)} played since "
        f"{BACKFILL_FROM} ({len(brief)} more under {MIN_PLAYTIME_MINUTES} min)"
    )

    backloggd_login()
    titles, slugs, playing = fetch_backloggd_library()
    log(f"backloggd: {len(titles)} tracked titles, {len(playing)} playing names")

    untracked = []
    in_progress = []
    for game in sorted(candidates, key=lambda g: g.get("rtime_last_played", 0), reverse=True):
        normalized = normalize_title(game["name"])
        exact = normalized in titles or normalized in slugs
        status = "exact" if exact else match_status(game["name"], titles, slugs)
        if not status:
            untracked.append(game)
            continue
        if not status.startswith("exact"):
            log(f"  fuzzy-matched as tracked: {game['name']!r} ({status})")
        # Tracked: nag about it only while it sits on the playing shelf —
        # the entry exists but awaits a completion (or retired/shelved)
        # date. Fuzzy matches skip the playing check (exact names only).
        if exact and normalized in playing:
            in_progress.append(game)

    if not untracked and not in_progress:
        log("all caught up; sending heartbeat")
        pushover(
            "Backloggd: all caught up",
            f"All {len(candidates)} Steam game(s) with activity since "
            f"{BACKFILL_FROM} are logged in Backloggd, none still marked "
            "playing.",
        )
        return

    # Enrich in-progress entries first: their completion percentages are
    # the most actionable signal, then the untracked tail within budget.
    for game in (in_progress + untracked)[:ACHIEVEMENT_FETCH_LIMIT]:
        game["_ach"] = achievement_window(game["appid"])
        time.sleep(STEAM_MIN_INTERVAL)

    # Purchase dates ride along on every candidate that has one; they show
    # as "bought <date>" and stand in for the start only when no
    # achievement signal exists.
    purchases = load_purchase_dates()
    filled = 0
    for game in untracked + in_progress:
        purchased = purchases.get(game["appid"])
        if purchased:
            game["_purchase"] = purchased
            filled += 1
    if filled:
        log(f"purchase dates attached to {filled} game(s)")

    sections = []
    if untracked:
        lines = [
            f"{len(untracked)} Steam game(s) played since {BACKFILL_FROM} "
            f"missing from Backloggd:",
            "",
        ]
        lines += [describe(game) for game in untracked[:MAX_REPORT]]
        if len(untracked) > MAX_REPORT:
            lines.append(f"... and {len(untracked) - MAX_REPORT} more (raise MAX_REPORT)")
        sections.append("\n".join(lines))
    if in_progress:
        lines = [
            "Still marked playing — complete the entry when beaten:",
            "",
        ]
        lines += [describe(game) for game in in_progress[:MAX_REPORT]]
        if len(in_progress) > MAX_REPORT:
            lines.append(f"... and {len(in_progress) - MAX_REPORT} more")
        sections.append("\n".join(lines))
    if len(untracked) + len(in_progress) > ACHIEVEMENT_FETCH_LIMIT:
        sections.append(
            f"(date hints shown for the {ACHIEVEMENT_FETCH_LIMIT} most relevant; "
            "the rest are last-played only)"
        )
    message = "\n\n".join(sections)
    for game in untracked:
        log(f"  untracked: {describe(game)}")
    for game in in_progress:
        log(f"  playing: {describe(game)}")
    title = f"Backloggd: {len(untracked)} game(s) to log"
    if in_progress:
        title += f", {len(in_progress)} in progress"
    pushover(title, message)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - report anything, fail the job
        log(f"ERROR: {exc!r}")
        with contextlib.suppress(Exception):  # best effort
            pushover("Backloggd steam-sync failed", repr(exc), priority=1)
        sys.exit(1)
