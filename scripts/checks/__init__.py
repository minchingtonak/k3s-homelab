from dataclasses import dataclass


@dataclass
class Violation:
    path: str
    message: str
    fix_hint: str | None = None
