from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass
class Violation:
    path: str
    message: str
    fix_hint: str | None = None


def _applies(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, p) for p in patterns)


def resource_id(doc: dict) -> str:
    meta = doc.get("metadata") or {}
    name = meta.get("name", "<unnamed>")
    ns = meta.get("namespace", "<no-namespace>")
    return f"'{name}' (ns: {ns})"
