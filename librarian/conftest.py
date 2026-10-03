"""Pytest root conftest: make librarian importable without installation.

Works from a fresh clone (no .pth, no editable install): pytest loads this
conftest from the rootdir before collecting tests.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
