from checks import Violation, resource_id

NAME = "deployment-strategy-recreate"
DESCRIPTION = "Deployments that mount a PVC must set strategy.type: Recreate (single-replica apps on RWO Longhorn volumes deadlock on rolling updates with a Multi-Attach error)"
PATHS = ["k8s/**/*.yaml", "k8s/**/*.yml"]

FIX_HINT = (
    "  Add under the Deployment's spec:\n"
    "    spec:\n"
    "      strategy:\n"
    "        type: Recreate\n"
    "\n"
    "  Rolling updates start the new pod before the old one releases its RWO\n"
    "  volume; when the two land on different nodes the new pod cannot attach\n"
    "  and the rollout stalls. Recreate terminates the old pod first.\n"
    "\n"
    "  Stateless Deployments (no PVC) are exempt and keep rolling updates."
)


def _mounts_pvc(doc: dict) -> bool:
    spec = (doc.get("spec") or {}).get("template") or {}
    volumes = (spec.get("spec") or {}).get("volumes") or []
    return any(isinstance(v, dict) and "persistentVolumeClaim" in v for v in volumes)


def check(path: str, docs: list[dict]) -> list[Violation]:
    violations = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        if not _mounts_pvc(doc):
            continue
        strategy = (doc.get("spec") or {}).get("strategy") or {}
        if strategy.get("type") == "Recreate":
            continue
        found = strategy.get("type") or "RollingUpdate (default)"
        violations.append(Violation(
            path=path,
            message=f"Deployment {resource_id(doc)} mounts a PVC but has strategy.type {found}; must be 'Recreate'",
            fix_hint=FIX_HINT,
        ))
    return violations
