#!/usr/bin/env bash
#
# preserve-added-date.sh - Radarr/Sonarr Custom Script
#
# Keeps the original "date added" when an existing media file is upgraded,
# so Jellyfin's "Recently Added" rows are not re-polluted by upgraded media.
#
# On upgrade events the arrs report the replaced file's original added date
# (radarr_deleteddateadded / sonarr_deleteddateadded). This script stamps
# the newly imported file's mtime with the earliest of those dates. With
# Jellyfin's "Date added behavior for new content" set to "Use file creation
# date", the item keeps its original place in Recently Added.
#
# First-time imports (isupgrade=False) are left alone on purpose: genuinely
# new media should appear as new.
#
# Event contract (wiki.servarr.com/{radarr,sonarr}/custom-scripts), event
# "Download" covers both On Import and On Upgrade:
#   *_isupgrade          True when an existing file was replaced
#   *_moviefile_path     Radarr: new movie file
#   *_episodefile_path   Sonarr: new episode file (single-file import)
#   *_episodefile_paths  Sonarr: |-delimited new files (multi-file import)
#   *_deleteddateadded   |-delimited original added dates of replaced files
#
# Script stdout is logged at Debug and stderr at Error by the arrs.

set -euo pipefail

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

event="${radarr_eventtype:-${sonarr_eventtype:-}}"
isupgrade="${radarr_isupgrade:-${sonarr_isupgrade:-}}"

case "$event" in
  Test)
    log "test event received"
    exit 0
    ;;
  Download | Import) ;;
  *)
    log "ignoring event '${event:-<none>}'"
    exit 0
    ;;
esac

if [ "$isupgrade" != "True" ]; then
  log "not an upgrade; new media keeps its new date"
  exit 0
fi

deleted_added="${radarr_deleteddateadded:-${sonarr_deleteddateadded:-}}"
if [ -z "$deleted_added" ]; then
  log "upgrade without replaced-file dates; nothing to restore"
  exit 0
fi

# New file path(s) written by this import.
files=()
if [ -n "${radarr_moviefile_path:-}" ]; then
  files+=("$radarr_moviefile_path")
fi
if [ -n "${sonarr_episodefile_path:-}" ]; then
  files+=("$sonarr_episodefile_path")
fi
if [ -n "${sonarr_episodefile_paths:-}" ]; then
  IFS='|' read -r -a multi <<<"$sonarr_episodefile_paths"
  files+=("${multi[@]}")
fi
if [ "${#files[@]}" -eq 0 ]; then
  log "no imported file paths in event; nothing to do"
  exit 0
fi

# Earliest original add date among the replaced files (multi-episode merges
# can replace several files with one; the oldest add date is the real one).
earliest=""
IFS='|' read -r -a dates <<<"$deleted_added"
for d in "${dates[@]}"; do
  [ -n "$d" ] || continue
  if ! epoch=$(to_epoch "$d"); then
    log "skipping unparseable date '$d'"
    continue
  fi
  if [ -z "$earliest" ] || [ "$epoch" -lt "$earliest" ]; then
    earliest=$epoch
  fi
done
if [ -z "$earliest" ]; then
  log "no usable original date in '$deleted_added'; leaving timestamps alone"
  exit 0
fi

restored=0
for f in "${files[@]}"; do
  if [ -e "$f" ]; then
    touch -d "@$earliest" "$f"
    restored=$((restored + 1))
    log "set '$f' dateAdded to $(date -d "@$earliest" '+%F %T %Z')"
  else
    log "imported file '$f' not found; skipping"
  fi
done
log "done: restored original dateAdded on $restored file(s)"
