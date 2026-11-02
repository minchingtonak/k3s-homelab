#!/usr/bin/env bash
#
# preserve-added-date.sh - Radarr/Sonarr Custom Script
#
# Keeps the original "date added" when an existing media file is upgraded,
# so Jellyfin's "Recently Added" rows are not re-polluted by upgraded media.
#
# Two phases (both handled by this one script):
#
#   On Grab:    snapshot the mtime of every existing file of the movie/series
#               (read from the arr's own API, since Grab events carry no file
#               paths) into /config/.preserve-added-date/.
#   On Import:  stamp the newly imported file's mtime with the earliest
#               snapshotted mtime of the file(s) it replaced (matched via
#               *_deletedpaths), falling back to *_deleteddateadded when no
#               snapshot exists.
#
# The snapshot is preferred over the arr's *_deleteddateadded because the arr
# only knows its own import-chain date: items whose dates were back-dated
# (older than the chain) would otherwise snap forward on every upgrade.
#
# First-time imports (isupgrade=False) are left alone on purpose: genuinely
# new media should appear as new.
#
# Event contract (wiki.servarr.com/{radarr,sonarr}/custom-scripts):
#   Grab:
#   *_eventtype=Grab, *_movie_id / *_series_id (used to fetch file paths)
#   Import/Upgrade ("Download" in both apps):
#   *_isupgrade          True when an existing file was replaced
#   *_moviefile_path     Radarr: new movie file
#   *_episodefile_path   Sonarr: new episode file (single-file import)
#   *_episodefile_paths  Sonarr: |-delimited new files (multi-file import)
#   *_deletedpaths       |-delimited full paths of the replaced files
#   *_deleteddateadded   |-delimited arr-side added dates of replaced files
#
# Script stdout is logged at Debug and stderr at Error by the arrs.

set -euo pipefail

STATE_DIR="${PRESERVE_STATE_DIR:-/config/.preserve-added-date}"

log() { echo "preserve-added-date: $*"; }

# Arr timestamps are UTC; parse explicit zones as given, treat naive ones as UTC.
to_epoch() {
  local d=$1
  if [[ "$d" =~ (Z|[+-][0-9]{2}:?[0-9]{2})$ ]]; then
    date -d "$d" +%s
  else
    date -u -d "$d" +%s
  fi
}

state_key() {
  if [ -n "${radarr_movie_id:-}" ]; then
    echo "$STATE_DIR/movie-$radarr_movie_id"
  elif [ -n "${sonarr_series_id:-}" ]; then
    echo "$STATE_DIR/series-$sonarr_series_id"
  fi
}

# ---- On Grab: snapshot current file mtimes ------------------------------
handle_grab() {
  mkdir -p "$STATE_DIR"
  find "$STATE_DIR" -type f -mtime +30 -delete 2>/dev/null || true

  local key files_json
  key=$(sed -n "s/.*<ApiKey>\([^<]*\)<.*/\1/p" /config/config.xml)
  if [ -n "${radarr_movie_id:-}" ]; then
    files_json=$(curl -sf -H "X-Api-Key: $key" \
      "http://localhost:7878/api/v3/moviefile?movieId=$radarr_movie_id") || files_json="[]"
  elif [ -n "${sonarr_series_id:-}" ]; then
    files_json=$(curl -sf -H "X-Api-Key: $key" \
      "http://localhost:8989/api/v3/episodefile?seriesId=$sonarr_series_id") || files_json="[]"
  else
    log "grab: no movie/series id in event; nothing to snapshot"
    return 0
  fi

  local keyfile n=0 p
  keyfile=$(state_key)
  : >"$keyfile.tmp"
  while IFS= read -r p; do
    [ -n "$p" ] && [ -e "$p" ] || continue
    printf '%s\t%s\n' "$(stat -c %Y "$p")" "$p" >>"$keyfile.tmp"
    n=$((n + 1))
  done < <(jq -r '.[].path' <<<"$files_json")
  mv "$keyfile.tmp" "$keyfile"
  log "grab: snapshotted $n existing file timestamp(s)"
}

# ---- On Import/Upgrade: restore the timestamp ---------------------------
handle_import() {
  local isupgrade="${radarr_isupgrade:-${sonarr_isupgrade:-}}"
  if [ "$isupgrade" != "True" ]; then
    log "not an upgrade; new media keeps its new date"
    return 0
  fi

  # New file path(s) written by this import.
  local files=()
  if [ -n "${radarr_moviefile_path:-}" ]; then
    files+=("$radarr_moviefile_path")
  fi
  if [ -n "${sonarr_episodefile_path:-}" ]; then
    files+=("$sonarr_episodefile_path")
  fi
  if [ -n "${sonarr_episodefile_paths:-}" ]; then
    local multi=()
    IFS='|' read -r -a multi <<<"$sonarr_episodefile_paths"
    files+=("${multi[@]}")
  fi
  if [ "${#files[@]}" -eq 0 ]; then
    log "no imported file paths in event; nothing to do"
    return 0
  fi

  # 1) Preferred: the snapshot taken at grab time, matched against the files
  #    this import replaced (*_deletedpaths), earliest timestamp wins.
  local epoch=""
  local keyfile
  keyfile=$(state_key)
  if [ -n "$keyfile" ] && [ -f "$keyfile" ]; then
    local del="${radarr_deletedpaths:-${sonarr_deletedpaths:-}}"
    local delbasenames="|"
    if [ -n "$del" ]; then
      local IFS='|' d
      for d in $del; do
        [ -n "$d" ] && delbasenames+="${d##*/}|"
      done
    fi
    local t p
    while IFS=$'\t' read -r t p; do
      [ -n "$t" ] || continue
      if [ "$delbasenames" != "|" ] && [[ "$delbasenames" != *"|$p|"* ]] && [[ "$delbasenames" != *"|${p##*/}|"* ]]; then
        continue
      fi
      if [ -z "$epoch" ] || [ "$t" -lt "$epoch" ]; then
        epoch=$t
      fi
    done <"$keyfile"
    rm -f "$keyfile"
    [ -n "$epoch" ] && log "restoring from grab snapshot ($(date -d "@$epoch" '+%F %T %Z'))"
  fi

  # 2) Fallback: the arr's own added-dates for the replaced files.
  if [ -z "$epoch" ]; then
    local deleted_added="${radarr_deleteddateadded:-${sonarr_deleteddateadded:-}}"
    if [ -z "$deleted_added" ]; then
      log "upgrade without replaced-file dates; nothing to restore"
      return 0
    fi
    local IFS='|' dates d
    read -r -a dates <<<"$deleted_added"
    for d in "${dates[@]}"; do
      [ -n "$d" ] || continue
      local t
      if ! t=$(to_epoch "$d"); then
        log "skipping unparseable date '$d'"
        continue
      fi
      if [ -z "$epoch" ] || [ "$t" -lt "$epoch" ]; then
        epoch=$t
      fi
    done
    [ -n "$epoch" ] && log "restoring from arr dateAdded ($(date -d "@$epoch" '+%F %T %Z'))"
  fi

  if [ -z "$epoch" ]; then
    log "no usable original date found; leaving timestamps alone"
    return 0
  fi

  local restored=0 f
  for f in "${files[@]}"; do
    if [ -e "$f" ]; then
      touch -d "@$epoch" "$f"
      restored=$((restored + 1))
      log "set '$f' dateAdded to $(date -d "@$epoch" '+%F %T %Z')"
    else
      log "imported file '$f' not found; skipping"
    fi
  done
  log "done: restored original dateAdded on $restored file(s)"
}

event="${radarr_eventtype:-${sonarr_eventtype:-}}"
case "$event" in
  Test)
    log "test event received"
    ;;
  Grab)
    handle_grab
    ;;
  Download | Import)
    handle_import
    ;;
  *)
    log "ignoring event '${event:-<none>}'"
    ;;
esac
