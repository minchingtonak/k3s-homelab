import yaml

from checks import Violation, resource_id

NAME = "gatus-annotation"
DESCRIPTION = "every kind:Service must have the gatus endpoint annotation (or opt out)"
PATHS = ["k8s/**/*.yaml", "k8s/**/*.yml"]

OPT_OUT = "gatus.home-operations.com/enabled"
REQUIRED = "gatus.home-operations.com/endpoint"

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


def _has_pushover(endpoint_value: str) -> bool:
    try:
        body = yaml.safe_load(endpoint_value)
    except yaml.YAMLError:
        return False
    alerts = (body or {}).get("alerts") if isinstance(body, dict) else None
    if not isinstance(alerts, list):
        return False
    return any(isinstance(a, dict) and a.get("type") == "pushover" for a in alerts)


def check(path: str, docs: list[dict]) -> list[Violation]:
    violations = []
    for doc in docs:
        if doc.get("kind") != "Service":
            continue

        annotations = (doc.get("metadata") or {}).get("annotations") or {}

        opt_out = annotations.get(OPT_OUT)
        if str(opt_out).lower() == "false" or opt_out is False:
            continue

        endpoint = annotations.get(REQUIRED)
        if endpoint is None:
            violations.append(Violation(
                path=path,
                message=f"Service {resource_id(doc)} missing '{REQUIRED}'",
                fix_hint=FIX_HINT,
            ))
        elif not _has_pushover(str(endpoint)):
            violations.append(Violation(
                path=path,
                message=f"Service {resource_id(doc)} has '{REQUIRED}' but is missing '- type: pushover'",
                fix_hint=FIX_HINT,
            ))

    return violations
