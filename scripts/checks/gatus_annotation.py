import re

from checks import Violation

NAME = "gatus-annotation"
DESCRIPTION = "every kind:Service must have the gatus endpoint annotation (or opt out)"

OPT_OUT = "gatus.home-operations.com/enabled"
REQUIRED = "gatus.home-operations.com/endpoint"

OPT_OUT_RE = re.compile(
    rf'^\s+{re.escape(OPT_OUT)}:\s*["\']?false["\']?\s*$',
    re.MULTILINE | re.IGNORECASE,
)
REQUIRED_RE = re.compile(rf'^\s+{re.escape(REQUIRED)}:', re.MULTILINE)
PUSHOVER_RE = re.compile(r'^\s+-\s+type:\s+pushover\s*$', re.MULTILINE)
KIND_SERVICE_RE = re.compile(r'^kind:\s+Service\s*$', re.MULTILINE)
NAME_RE = re.compile(r'^\s{2}name:\s+(\S+)', re.MULTILINE)
NS_RE = re.compile(r'^\s{2}namespace:\s+(\S+)', re.MULTILINE)

FIX_HINT = (
    f"  annotations:\n"
    f"    {REQUIRED}: |\n"
    f"      alerts:\n"
    f"        - type: pushover\n"
    f"\n"
    f"  Or opt out internal services with:\n"
    f"  annotations:\n"
    f'    {OPT_OUT}: "false"'
)


def check(path: str, content: str) -> list[Violation]:
    violations = []
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
            violations.append(Violation(
                path=path,
                message=f"Service '{name}' (ns: {ns}) missing '{REQUIRED}'",
                fix_hint=FIX_HINT,
            ))
        elif not PUSHOVER_RE.search(doc):
            violations.append(Violation(
                path=path,
                message=f"Service '{name}' (ns: {ns}) has '{REQUIRED}' but is missing '- type: pushover'",
                fix_hint=FIX_HINT,
            ))

    return violations
