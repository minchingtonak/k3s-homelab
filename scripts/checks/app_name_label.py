from checks import Violation, resource_id

NAME = "app-name-label"
DESCRIPTION = "every kind:Deployment must have app.kubernetes.io/name under metadata.labels"
PATHS = ["k8s/apps/**/*.yaml", "k8s/apps/**/*.yml"]

FIX_HINT = (
    "  metadata:\n"
    "    labels:\n"
    "      app.kubernetes.io/name: <app-name>"
)


def check(path: str, docs: list[dict]) -> list[Violation]:
    violations = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        labels = (doc.get("metadata") or {}).get("labels") or {}
        if not labels.get("app.kubernetes.io/name"):
            violations.append(Violation(
                path=path,
                message=f"Deployment {resource_id(doc)} missing 'app.kubernetes.io/name' label under metadata.labels",
                fix_hint=FIX_HINT,
            ))
    return violations
