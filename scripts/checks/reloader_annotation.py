from checks import Violation, resource_id

NAME = "reloader-secret-annotation"
DESCRIPTION = (
    "Deployments that consume a Secret (env, envFrom, or volume) must carry a "
    "stakater Reloader annotation so pods roll when the Secret changes"
)
PATHS = ["k8s/**/*.yaml", "k8s/**/*.yml"]

ANNOTATIONS = (
    "secret.reloader.stakater.com/reload",
    "reloader.stakater.com/auto",
)

FIX_HINT = (
    "  Add under the Deployment's metadata:\n"
    "    metadata:\n"
    "      annotations:\n"
    "        secret.reloader.stakater.com/reload: <secret-name>\n"
    "\n"
    "  Or, to auto-detect every Secret/ConfigMap referenced by the pod template:\n"
    "    metadata:\n"
    "      annotations:\n"
    '        reloader.stakater.com/auto: "true"\n'
    "\n"
    "  Without this, env values seeded from a Secret become stale until someone\n"
    "  manually restarts the pod."
)


def _container_uses_secret(container: dict) -> bool:
    for env in container.get("env") or []:
        if isinstance(env, dict) and "secretKeyRef" in (env.get("valueFrom") or {}):
            return True
    for env_from in container.get("envFrom") or []:
        if isinstance(env_from, dict) and "secretRef" in env_from:
            return True
    return False


def _uses_secret(doc: dict) -> bool:
    pod_spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}

    containers = (pod_spec.get("containers") or []) + (pod_spec.get("initContainers") or [])
    if any(_container_uses_secret(c) for c in containers if isinstance(c, dict)):
        return True

    for volume in pod_spec.get("volumes") or []:
        if isinstance(volume, dict) and "secret" in volume:
            return True

    return False


def _has_reloader_annotation(doc: dict) -> bool:
    annotations = (doc.get("metadata") or {}).get("annotations") or {}
    return any(annotations.get(a) for a in ANNOTATIONS)


def check(path: str, docs: list[dict]) -> list[Violation]:
    violations = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        if not _uses_secret(doc):
            continue
        if _has_reloader_annotation(doc):
            continue
        violations.append(Violation(
            path=path,
            message=f"Deployment {resource_id(doc)} consumes a Secret but has no Reloader annotation",
            fix_hint=FIX_HINT,
        ))
    return violations
