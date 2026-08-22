# Weekly Renovate digest

Procedure for the Hermes agent's `renovate-digest` cron job. `scripts/renovate-digest.sh` prints this file into the
agent's prompt at the start of every run, so editing it here changes what the agent does next Friday — no change on the
agent host is needed, and none should be made there.

Renovate opens pull requests against this repo on Fridays (`schedule: '* 7 * * 5'`, America/New_York). This job reads
them a few hours later and reports what changed, so a wall of `chore(deps)` titles becomes a short list with a
recommendation attached to each one.

## What you are doing

**Reporting only.** Do not open a pull request, do not push a branch, do not merge anything, do not touch the cluster.
The output of this job is one Discord message.

The script has already done the deterministic half — enumerating the open pull requests, diffing them against last
week's, and computing ages. Do not re-run `gh pr list` to check its work.

## Read the input

The script's output has five sections:

- **CLOSED OR MERGED SINCE THE LAST DIGEST** — landed or dropped. One summary line in your report, no detail.
- **NEW OR CHANGED SINCE THE LAST DIGEST** — the real work. Each has its full Renovate body, which usually embeds the
  upstream release notes.
- **NEW, BUT DEFERRED TO A LATER RUN FOR PROMPT BUDGET** — new, but there were more open pull requests than fit in one
  prompt, so these arrived as titles only. **You have not reviewed them.** Report the count and say exactly that. They
  are not recorded as seen, so they come back with full bodies on a later run — do not guess at their contents now to
  make the report look complete.
- **CARRIED OVER, UNCHANGED** — reported before and still open. A count and the numbers, nothing more. Re-describing
  these every week is what makes a digest stop being read.
- **PROCEDURE** — this file.

If the new-or-changed section is empty, say so in one line and stop. Do not pad the report by promoting carried-over
entries to fill space.

## Research each new or changed pull request

Start from the embedded release notes — for most updates that is all you need.

Two cases need more:

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

Delivery is Discord, which hard-chunks at 2000 characters. **Keep the whole message under about 1800.** That is a
constraint, not a preference — an overflowing digest arrives split mid-sentence.

Shape:

```
**Renovate digest — <date>**
<N> reviewed · <N> deferred · <N> carried · <N> merged since last

**Merge now**
- #440 traefik 41.1.1→41.2.0 — CRD update, chart applies it. no values we set changed.

**Read first**
- #444 homepage v1→v2 — MAJOR. services.yaml schema changed; we set it in k8s/apps/homepage/configmap.yaml.

**Hold**
- #433 listenarr canary-1.3.0 — canary tag, no published notes.

Deferred, not reviewed (23): #439 #438 #437 …
Carried (17): #443 #442 #441 …
Merged since last: #426 #425
```

One line per pull request: number, what it is, the version change, then the reason for the bucket. No preamble, no
closing summary, no restating the counts you already put in the header.

If you are over budget, cut from **Merge now** first — those are the lines a human acts on without reading. Never cut a
**Hold**, a `security` line, or the deferred count to make room; drop the carried-over numbers instead and say
`Carried: 17 (list omitted)`.

State what you could not determine. "Could not find release notes for #433" is a useful line. Silence that reads as
approval is the failure mode this job exists to prevent.
