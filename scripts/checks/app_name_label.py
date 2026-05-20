import re

from checks import Violation

NAME = "app-name-label"
DESCRIPTION = "every kind:Deployment must have app.kubernetes.io/name under metadata.labels"

KIND_DEPLOYMENT_RE = re.compile(r'^kind:\s+Deployment\s*$', re.MULTILINE)
NAME_RE = re.compile(r'^\s{2}name:\s+(\S+)', re.MULTILINE)
NS_RE = re.compile(r'^\s{2}namespace:\s+(\S+)', re.MULTILINE)

# Matches the metadata: block (0-indent key) up to the next 0-indent key.
# We look for `labels:` inside it, then `app.kubernetes.io/name:` inside that.
METADATA_BLOCK_RE = re.compile(
    r'^metadata:\s*\n((?:[ \t]+.*\n?)*)',
    re.MULTILINE,
)
LABELS_BLOCK_RE = re.compile(
    r'^\s+labels:\s*\n((?:\s{4,}.*\n?)*)',
    re.MULTILINE,
)
APP_NAME_RE = re.compile(r'app\.kubernetes\.io/name\s*:')

FIX_HINT = (
    "  metadata:\n"
    "    labels:\n"
    "      app.kubernetes.io/name: <app-name>"
)


def check(path: str, content: str) -> list[Violation]:
    if "k8s/apps/" not in path:
        return []
    violations = []
    docs = re.split(r'^---\s*$', content, flags=re.MULTILINE)

    for doc in docs:
        if not KIND_DEPLOYMENT_RE.search(doc):
            continue

        m = NAME_RE.search(doc)
        name = m.group(1) if m else "<unnamed>"
        m = NS_RE.search(doc)
        ns = m.group(1) if m else "<no-namespace>"

        has_label = False
        meta_m = METADATA_BLOCK_RE.search(doc)
        if meta_m:
            meta_body = meta_m.group(1)
            labels_m = LABELS_BLOCK_RE.search(meta_body)
            if labels_m and APP_NAME_RE.search(labels_m.group(1)):
                has_label = True

        if not has_label:
            violations.append(Violation(
                path=path,
                message=f"Deployment '{name}' (ns: {ns}) missing 'app.kubernetes.io/name' label under metadata.labels",
                fix_hint=FIX_HINT,
            ))

    return violations
