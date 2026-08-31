#!/usr/bin/env bash
#
# Weekly watchdog for external-dns restarts.
#
# Run by the Hermes agent's `external-dns-restarts` cron job in no_agent mode:
# this script's stdout is the entire delivered message, so it stays silent
# while both external-dns deployments are healthy, speaks once when it first
# confirms health, and alerts on any restart, crashloop, or unavailable
# replica after that.
#
# Context: external-dns was restarting every ~10 minutes until the
# webhook-provider read-timeout fix landed on 2026-08-31. This watch answers
# "did the fix hold?" without anyone having to look.
#
# Exit codes: 0 healthy, alerting, or confirmation delivered; 1 on tool
# failure — the scheduler turns a non-zero exit into an error alert, so a
# broken kubeconfig can never fail silently.
#
# Reads the cluster only. Never writes to it.
#
# The cron entry point is a stub on the agent host (HERMES_HOME/scripts/) that
# fetches origin/main and runs this file from git, so nothing runs before it is
# reviewed. The stub owns the environment: it exports PATH, KUBECONFIG, and
# GIT_SSH_COMMAND (deploy key under /secrets — the scheduler's minimal env has
# no ~/.ssh and no aqua shims on PATH).

set -euo pipefail

NS="${EXTERNAL_DNS_NS:-external-dns}"
KUBECTL="${EXTERNAL_DNS_KUBECTL:-/home/hermes/.hermes/bin/kubectl}"
export KUBECONFIG="${KUBECONFIG:-/secrets/kubeconfig}"
STATE_DIR="${HERMES_STATE_DIR:-${HERMES_HOME:-$HOME/.hermes}/state}"
CONFIRMED_MARKER="${STATE_DIR}/external-dns-restarts.confirmed"
DEPLOYMENTS=(external-dns external-dns-unifi)

# One line per container: pod, pod start, container, restarts, waiting reason,
# last termination reason. jsonpath renders missing fields as empty strings,
# which the parsing below relies on.
container_lines="$("${KUBECTL}" -n "${NS}" get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.startTime}{"\t"}{range .status.containerStatuses[*]}{.name}{"\t"}{.restartCount}{"\t"}{.state.waiting.reason}{"\t"}{.lastState.terminated.reason}{"\n"}{end}{end}')" || {
  echo "external-dns watchdog: kubectl get pods -n ${NS} failed — check kubeconfig and cluster access" >&2
  exit 1
}

problems=()

now="$(date +%s)"
while IFS=$'\t' read -r pod started container restarts waiting last_reason; do
  [ -n "${pod}" ] || continue
  restarts="${restarts:-0}"
  started_epoch="$(date -d "${started}" +%s 2>/dev/null || printf '%s' "${now}")"
  age_h=$(( (now - started_epoch) / 3600 ))
  if [ "${restarts}" -gt 0 ]; then
    problems+=("pod ${pod} (age ${age_h}h): container ${container} has ${restarts} restarts, last termination: ${last_reason:-unknown}")
  fi
  if [ -n "${waiting}" ]; then
    problems+=("pod ${pod} (age ${age_h}h): container ${container} waiting: ${waiting}")
  fi
done < <(printf '%s\n' "${container_lines}")

for dep in "${DEPLOYMENTS[@]}"; do
  ready="$("${KUBECTL}" -n "${NS}" get deploy "${dep}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" || {
    echo "external-dns watchdog: kubectl get deploy ${dep} failed — renamed? deleted?" >&2
    exit 1
  }
  if [ "${ready:-0}" != "1" ]; then
    problems+=("deployment ${dep}: ${ready:-0}/1 replicas ready")
  fi
done

if [ "${#problems[@]}" -gt 0 ]; then
  echo "external-dns watchdog: restarts detected — the fix did not hold."
  echo ""
  printf '%s\n' "${problems[@]}"
  echo ""
  echo "diagnose: kubectl -n ${NS} get events --sort-by=.lastTimestamp"
  rm -f "${CONFIRMED_MARKER}"
  exit 0
fi

if [ ! -f "${CONFIRMED_MARKER}" ]; then
  mkdir -p "${STATE_DIR}"
  touch "${CONFIRMED_MARKER}"
  echo "external-dns watchdog: healthy — both deployments ready, 0 restarts on every container."
  echo "(one-time confirmation; the weekly check stays silent while this holds)"
fi
