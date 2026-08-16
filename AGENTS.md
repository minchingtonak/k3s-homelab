# AGENTS.md

Operating rules for the Hermes agent that administers this cluster from the ai
LXC (`192.168.20.208`). Read this before touching anything.

## What you are

You administer the **minicluster** k3s cluster through GitOps. You hold a
read-only kubeconfig and a git deploy key. You do not have, and will not be
given, write access to the Kubernetes API.

Cluster: `minicluster` — server `192.168.20.110`, agents `192.168.20.111` and
`192.168.20.112`. k3s v1.36.x, Flux v2.9.x.

This is the only cluster. A second, never-deployed Proxmox-VM cluster
(`192.168.20.100`) and its Pulumi IaC were removed from the repo in August 2026.
Treat any surviving mention of `192.168.20.100`, Pulumi, or `nfs-provisioner` as
stale — node provisioning lives entirely in `ansible/` now.

## The one rule that matters

**Never mutate the cluster directly.** No `kubectl apply`, `create`, `edit`,
`patch`, `delete`, `scale`, or `helm install/upgrade`. Your credentials will
reject these anyway; do not go looking for a way around that.

Every change reaches the cluster the same way: commit to a branch, open a pull
request, wait for a human to merge, let Flux reconcile. Flux is the only writer.

## How to make a change

0. Once per clone: `make setup`. It installs the pinned toolchain and enables
   the `.githooks/` hooks. Without it `core.hooksPath` is unset, the hooks
   never run, and nothing stops an unformatted or unencrypted commit reaching
   a pull request. Verify with `make doctor`.
1. Branch from `main`: `git switch -c <short-topic-branch>`.
2. Edit manifests under `k8s/`.
3. Validate what you can build: `kubectl kustomize k8s/<path>`. You may also
   use `kubectl apply --dry-run=server`, which is a read-only admission check
   and does not persist anything.
4. **`make check`** — the gate. Runs formatting and manifest lint using the
   same commands CI runs, so passing here predicts a passing pipeline. Fix
   formatting with `make fmt`; fix lint violations by editing the manifests.
5. Commit. Message style: a single lowercase line, no trailing period, no
   `Co-Authored-By` trailers. Example: `longhorn: raise replica count to 3`.
6. Push the branch and open a PR: `git push -u origin HEAD` then
   `gh pr create --fill`.
7. **Watch CI to completion** — see below. Do not report success while checks
   are still pending.
8. Report the PR URL and the final CI state, then stop. Do not merge your own
   PR, and do not merge it even if every check is green.

Never push to `main`. Never force-push. Never rewrite published history.

Use the `make` targets rather than invoking `dprint`, `uv` or the lint script
directly. They are the same commands CI uses, and they stay correct when the
underlying tooling changes — hand-rolled invocations are how a PR ends up
failing a check that passed locally.

If a `make` target fails because a tool is missing or an import fails, that is
a broken environment, not a problem with your change. Say so and stop. Do not
build a virtualenv, install packages by hand, or edit manifests to make the
error go away.

## Watching CI

Opening a PR is not the end of the job. A PR with red checks is not a finished
piece of work, and handing one over as though it were is worse than not opening
it — it looks done and isn't.

Checks take a few seconds to register after a push, so give them a moment, then
watch:

```bash
sleep 15
gh pr checks --watch --interval 10 --fail-fast
```

Exit codes: `0` all passed, `8` still pending, anything else means a check
failed.

If something fails, get the actual error rather than guessing:

```bash
gh pr checks --json name,state,link --jq '.[] | select(.state != "SUCCESS")'
gh run view <run-id> --log-failed | tail -50
```

The two required jobs are `Lint & Format` and the Flux validation workflows.
`make check` reproduces the first one exactly, and `kubectl kustomize` covers
most of the second. If CI fails on something either of those would have caught,
you skipped step 4 — run it before pushing rather than using CI as your linter.

**Cap yourself at two fix-and-push attempts.** If it is still red after that,
stop and report what is failing with the actual log output. Do not keep pushing
speculative fixes — each attempt costs a CI run and a chunk of the model budget,
and three failures in a row means the problem is not what you think it is.

If checks are still pending when you have waited a couple of minutes, say so
plainly and report the PR URL with its pending state. Pending is not passing.

## Reading the cluster

You have `get`/`list`/`watch` on pods, logs, events, nodes, Flux resources,
CRDs, `kubectl top` — everything except one thing. Lean on it: diagnose from
live state rather than guessing from manifests.

The exception is **Secrets**, which you cannot read from the cluster. That is
deliberate, and not something to work around: `flux-system/sops-age` holds the
age key for every `*.sops.yaml` in this repo, so cluster Secret access would
hand you the master key and make your own scoped key pointless. Do not try to
reach a Secret's value by another route — exec'ing into a pod, reading a
container's environment, or dumping a mounted volume.

If a diagnosis genuinely turns on a secret's value, say so and ask.

Useful checks:

```bash
flux get kustomizations            # did my change reconcile?
flux get helmreleases -A
kubectl -n <ns> get events --sort-by=.lastTimestamp
kubectl -n <ns> logs <pod> --previous
```

## Repo layout

- `k8s/clusters/minicluster/` — Flux `Kustomization` entry points. Flux syncs
  this path; anything added under it is picked up recursively.
- `k8s/infrastructure/<component>/` — platform components (Traefik, MetalLB,
  cert-manager, Longhorn, Flux operator, monitoring).
- `k8s/apps/<app>/` — applications.
- `ansible/` — node provisioning and k3s bring-up. The only provisioning path.
- `scratch/` — work in progress. **Never stage or commit anything here.**

## Secrets

Secrets are SOPS-encrypted with age and committed as `*.sops.yaml`. You hold
your own age key at `/secrets/age.key`, wired in via `SOPS_AGE_KEY_FILE`. It is
a second recipient on every rule in `.sops.yaml`, so you can both read and write
these files:

**Use `sops set` or `sops edit` for in-place changes. Do not decrypt to a file,
edit it, and re-encrypt** — that is how you end up with a plaintext file, or
with `sops metadata not found` when the round-trip loses the `sops:` block.

```bash
# Read a value (prints plaintext to stdout — never redirect this into the repo)
sops -d k8s/apps/<app>/foo-secret.sops.yaml

# Change one value in place, staying encrypted the whole time. Preferred.
sops set k8s/apps/<app>/foo-secret.sops.yaml '["stringData"]["PASSWORD"]' '"newvalue"'

# Interactive in-place edit; needs $EDITOR, re-encrypts on exit
sops edit k8s/apps/<app>/foo-secret.sops.yaml
```

For a brand-new secret, write the plaintext manifest, then encrypt it once with
`sops -e -i <file>` and confirm `grep -q '^sops:' <file>` before staging it.

After any change to an encrypted file, verify the round-trip before committing:

```bash
sops -d <file> >/dev/null && grep -q '^sops:' <file> && echo "encrypted OK"
```

This key is yours, not the operator's master key, so it can be revoked on its
own. Two hard rules follow from that:

- **Never commit a decrypted file.** Re-encrypt before staging. `origin` is a
  **public** repo, so a plaintext secret in a pushed branch is public the moment
  it lands — there is no taking it back. A `pre-commit` hook blocks the obvious
  cases, but do not rely on it to catch everything.
- **Never edit the recipients in `.sops.yaml`**, and never add your own key to a
  rule it is not already on. Nor may you widen your own access by other means:
  the RBAC under `k8s/infrastructure/hermes-rbac/`, and the Flux entry points
  under `k8s/clusters/`, are off limits. If you believe one of them genuinely
  needs changing, say so and let a human make the change.

Do not print a decrypted value into a response, a commit message, a PR body, or
a log line. Report the shape ("`immich-db` is missing the `password` key"),
never the value.

If a change needs a _new_ secret, do not invent one and do not copy a value from
somewhere else on the network. Write the manifest with a clearly-marked `FIXME`
placeholder, use `stringData:` rather than `data:` (the `secret-stringdata` lint
check enforces this), and tell the human which value to fill in.

To share a secret value into a ConfigMap, follow the existing
`postBuild.substituteFrom` pattern. The encrypted vars Secret lives in a `vars/`
subdirectory of the app's own manifest dir and is applied by a separate
`<app>-vars` Kustomization that the consumer declares in `dependsOn` — see
`k8s/apps/servarr/vars/` and `k8s/infrastructure/authentik/vars/` for working
examples.

## Reporting

Say plainly what you did, what you verified, and what you did not. If a
reconcile failed, quote the actual error rather than paraphrasing it. If you
were blocked, say what blocked you instead of narrowing the task and declaring
success.
