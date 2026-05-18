#!/usr/bin/env python3
"""
Enforce gatus.home-operations.com/endpoint on every kind: Service.
Opt out with: gatus.home-operations.com/enabled: "false"

Uses only Python stdlib — no pip packages required.
"""
import re
import sys

OPT_OUT = "gatus.home-operations.com/enabled"
REQUIRED = "gatus.home-operations.com/endpoint"

# Matches: gatus.home-operations.com/enabled: "false" / 'false' / false
OPT_OUT_RE = re.compile(
    rf'^\s+{re.escape(OPT_OUT)}:\s*["\']?false["\']?\s*$',
    re.MULTILINE | re.IGNORECASE,
)
REQUIRED_RE = re.compile(
    rf'^\s+{re.escape(REQUIRED)}:',
    re.MULTILINE,
)
PUSHOVER_RE = re.compile(r'^\s+-\s+type:\s+pushover\s*$', re.MULTILINE)
KIND_SERVICE_RE = re.compile(r'^kind:\s+Service\s*$', re.MULTILINE)

FULL_BLOCK = (
    f"  annotations:\n"
    f"    {REQUIRED}: |\n"
    f"      alerts:\n"
    f"        - type: pushover"
)
OPT_OUT_BLOCK = (
    f"  annotations:\n"
    f"    {OPT_OUT}: \"false\""
)
# metadata.name and metadata.namespace are at 2-space indent
NAME_RE = re.compile(r'^\s{2}name:\s+(\S+)', re.MULTILINE)
NS_RE = re.compile(r'^\s{2}namespace:\s+(\S+)', re.MULTILINE)


def check_file(path):
    violations = []
    try:
        with open(path) as f:
            content = f.read()
    except OSError as e:
        return [f"{path}: could not read file: {e}"]

    # Split multi-document YAML on --- separators
    docs = re.split(r'^---\s*$', content, flags=re.MULTILINE)

    for doc in docs:
        if not KIND_SERVICE_RE.search(doc):
            continue

        m = NAME_RE.search(doc)
        name = m.group(1) if m else "<unnamed>"
        m = NS_RE.search(doc)
        ns = m.group(1) if m else "<no-namespace>"

        if OPT_OUT_RE.search(doc):
            continue

        if not REQUIRED_RE.search(doc):
            violations.append(f"{path}: Service '{name}' (ns: {ns}) missing '{REQUIRED}'.")
        elif not PUSHOVER_RE.search(doc):
            violations.append(f"{path}: Service '{name}' (ns: {ns}) has '{REQUIRED}' but is missing '- type: pushover'.")

    return violations


if __name__ == "__main__":
    violations = []
    for path in sys.argv[1:]:
        violations.extend(check_file(path))
    if violations:
        for v in violations:
            print(f"  [FAIL] {v}", file=sys.stderr)
        print(file=sys.stderr)
        print("  Fix: add to metadata:", file=sys.stderr)
        print(f"{FULL_BLOCK}", file=sys.stderr)
        print(file=sys.stderr)
        print("  Or opt out internal services with:", file=sys.stderr)
        print(f"{OPT_OUT_BLOCK}", file=sys.stderr)
        sys.exit(1)
