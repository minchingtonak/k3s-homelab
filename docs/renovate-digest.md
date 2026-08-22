# Weekly Renovate digest

Procedure for the Hermes agent's `renovate-digest` cron job. `scripts/renovate-digest.sh` prints this file into the
agent's prompt at the start of every run, so editing it here changes what the agent does on the next run — no change on
the agent host is needed, and none should be made there.

Renovate opens pull requests against this repo on Fridays (`schedule: '* 7 * * 5'`, America/New_York). This job runs the
following morning (Saturday 09:00, same timezone) and reports what changed, so a wall of `chore(deps)` titles becomes a
short list with a recommendation attached to each one.

## What you are doing

**Reporting only.** Do not open a pull request, do not push a branch, do not merge anything, do not touch the cluster.
The output of this job is one Telegram message.

The script has already done the deterministic half — enumerating the open pull requests, diffing them against last
week's, and computing ages. Do not re-run `gh pr list` to check its work.

## Read the input

The script's output has five sections:

- **CLOSED OR MERGED SINCE THE LAST DIGEST** — landed or dropped. One summary line in your report, no detail.
- **NEW OR CHANGED SINCE THE LAST DIGEST** — the real work. Each has its full Renovate body, which usually embeds the
  upstream release notes.
- **NEW, BUT DEFERRED TO A LATER RUN FOR PROMPT BUDGET** — normally empty, and a backstop rather than a routine state:
  the budget is sized to cover every open pull request in one run. If entries do appear here they arrived as titles only
  and **you have not reviewed them** — report the count and say exactly that. They are not recorded as seen, so they
  come back with full bodies on a later run. Do not guess at their contents to make the report look complete.
- **CARRIED OVER, UNCHANGED** — reported before and still open. A count and the numbers, nothing more. Re-describing
  these every week is what makes a digest stop being read.
- **PROCEDURE** — this file.

If the new-or-changed section is empty, say so in one line and stop. Do not pad the report by promoting carried-over
entries to fill space.

## Research each new or changed pull request

Start from the embedded release notes — for most updates that is all you need.

Three cases need more:

- **The body was truncated.** Long changelogs are cut with a `[…body truncated at N characters]` marker so that every
  open pull request fits in one run. Breaking changes are usually near the top, so this is normally harmless — but if
  what you can see suggests a major bump, a migration, or anything you would put in **Read first**, get the rest before
  deciding:

  ```bash
  gh pr view <number> --json body --jq .body
  ```

- **Renovate could not fetch the notes.** The body says so, often as
  `Some dependencies could not be looked up`, or the Release Notes section is missing entirely. Find the upstream
  release yourself with `WebFetch` on the project's releases page. If you cannot find them, say that rather than
  guessing from the version bump.
- **The notes mention a rename, removal, or required migration.** Check whether this repo is actually affected before
  reporting it as a blocker. Grep the manifests for the affected key:

  ```bash
  grep -rn "<renamed-values-key>" k8s/
  ```

  "The chart renamed `foo.bar` and we set it in `k8s/infrastructure/traefik/release.yaml:42`" is worth a human's
  attention. "The chart renamed `foo.bar`, which we do not set" is noise — leave it out.

Helm chart bumps deserve more suspicion than container tag bumps: a chart can change CRDs, RBAC, or values structure,
and a values key that silently stops being read fails at runtime rather than at reconcile.

## Classify

Every new or changed pull request lands in exactly one bucket:

- **Merge now** — patch or minor, no behaviour change that touches this repo's configuration, notes read clean.
- **Read first** — a major bump, a CRD change, a values key this repo actually sets, a changed default, or a migration
  guide the upstream project tells you to read. Say specifically what needs looking at and name the file if you found
  one.
- **Hold** — do not merge yet. Unstable or canary tags, release notes that could not be found at all, a known
  regression reported upstream, or a dependency on something else merging first.

When you are unsure between two buckets, choose the more cautious one and say in the line why it was close. A wrong
"merge now" costs a broken reconcile; a wrong "read first" costs thirty seconds of reading.

Anything labelled `security` goes first in the report regardless of bucket, and says what the vulnerability is.

## Report

Delivery is Telegram, which hard-chunks at 4096 characters. **Keep the whole message under about 3800.** That is a
constraint, not a preference — an overflowing digest arrives split at whatever character the chunker lands on.

**Every pull request you reviewed must appear somewhere in the report**, with its own line and its own reason. At around
90 characters a line that is roughly 40 pull requests inside the budget, which comfortably covers a normal week.

Shape:

```
**Renovate digest — <date>**
35 reviewed · 0 deferred · 0 carried · 2 merged since last

**Read first**
- #430 dawarich 1.11.0→1.12.2 — re-detects all visits on migrate; first boot rebuilds the points index, minutes on a big library
- #434 audiomuse 3.1.1→3.4.0 — queue moved to postgres, redis dropped upstream; we still set REDIS_URL in k8s/apps/audiomuse-ai/deployment.yaml

**Hold**
- #433 listenarr canary-1.2.2→canary-1.3.2 — prerelease channel, latest is an arm64 ABI fix

**Merge now**
- #426 reloader 2.2.14→2.2.16 — patches CVE-2026-56852 in x/text plus a regex panic
- #420 syncthing 2.1.2→2.1.3 — patch, bug fixes only
- #425 alloy 1.11.0→1.11.1 — patch, no values we set

Merged since last: #426 #425
```

One line per pull request: number, name, version change, then the reason for the bucket. Keep **Merge now** lines
shortest — they are the ones a human approves without reading — and spend the words on **Read first** and **Hold**.

No preamble, no closing summary, no restating the counts already in the header.

If you do run over, degrade **Merge now** first: shorten its reasons, then drop them, then collapse the bucket to a
single inline run of `#N name version` entries separated by a space-padded middle dot. Never cut a **Read first** line, a
**Hold** line, a `security` line, or the deferred count to make room — those are the entire point. Carried-over numbers
are the next thing to drop: say `Carried: 17 (list omitted)`.

State what you could not determine. "Could not find release notes for #433" is a useful line. Silence that reads as
approval is the failure mode this job exists to prevent.
