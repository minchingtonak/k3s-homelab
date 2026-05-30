from checks import Violation, resource_id

NAME = "secret-stringdata"
DESCRIPTION = "Opaque Secrets should use 'stringData' (plaintext) rather than 'data' (base64) for editor friendliness"
PATHS = ["k8s/**/*.yaml", "k8s/**/*.yml"]

# Types where 'data' is the natural form (binary payloads, opaque blobs the API
# defines explicitly as base64). Everything else — primarily 'Opaque' and
# untyped Secrets — must use stringData.
EXEMPT_TYPES = {
    "kubernetes.io/tls",
    "kubernetes.io/dockerconfigjson",
    "kubernetes.io/dockercfg",
    "kubernetes.io/ssh-auth",
    "bootstrap.kubernetes.io/token",
}

FIX_HINT = (
    "  Replace:\n"
    "    data:\n"
    "      MY_KEY: <base64>\n"
    "  With:\n"
    "    stringData:\n"
    "      MY_KEY: <plaintext>\n"
    "\n"
    "  Kubernetes base64-encodes stringData entries on apply, so the in-cluster\n"
    "  Secret is identical. For SOPS-encrypted files, decrypt, swap the field,\n"
    "  base64-decode each value, then re-encrypt."
)


def check(path: str, docs: list[dict]) -> list[Violation]:
    violations = []
    for doc in docs:
        if doc.get("kind") != "Secret":
            continue
        if doc.get("type") in EXEMPT_TYPES:
            continue
        if "data" not in doc:
            continue
        violations.append(Violation(
            path=path,
            message=f"Secret {resource_id(doc)} uses 'data' (base64); prefer 'stringData' (plaintext)",
            fix_hint=FIX_HINT,
        ))
    return violations
