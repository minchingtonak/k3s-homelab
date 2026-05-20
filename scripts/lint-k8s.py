#!/usr/bin/env python3
"""
Pre-commit linter for k8s YAML manifests.

To add a check: import the module and add it to CHECKS.
Each check module must expose NAME, DESCRIPTION, PATHS, and check(path, content) -> list[Violation].
PATHS is a list of fnmatch glob patterns; the check runs only on matching files.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from checks import Violation, _applies
from checks import gatus_annotation, app_name_label

CHECKS = [
    gatus_annotation,
    app_name_label,
]


def run(paths: list[str]) -> bool:
    """Return True if all checks pass."""
    # violations[check_name] = list[Violation]
    by_check: dict[str, list[Violation]] = {c.NAME: [] for c in CHECKS}

    for path in paths:
        applicable = [chk for chk in CHECKS if _applies(path, chk.PATHS)]
        if not applicable:
            continue
        try:
            content = Path(path).read_text()
        except OSError as e:
            print(f"  [ERROR] could not read {path}: {e}", file=sys.stderr)
            return False

        for chk in applicable:
            by_check[chk.NAME].extend(chk.check(path, content))

    any_fail = any(v for v in by_check.values())
    if not any_fail:
        return True

    hints_printed: set[str] = set()
    for chk in CHECKS:
        violations = by_check[chk.NAME]
        if not violations:
            continue
        print(f"\n  [{chk.NAME}] {chk.DESCRIPTION}", file=sys.stderr)
        for v in violations:
            print(f"    [FAIL] {v.path}: {v.message}", file=sys.stderr)
            if v.fix_hint and chk.NAME not in hints_printed:
                hints_printed.add(chk.NAME)
                print(file=sys.stderr)
                print("    Fix:", file=sys.stderr)
                for line in v.fix_hint.splitlines():
                    print(f"    {line}", file=sys.stderr)
                print(file=sys.stderr)

    return False


if __name__ == "__main__":
    if not run(sys.argv[1:]):
        sys.exit(1)
