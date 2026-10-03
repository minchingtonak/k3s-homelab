"""Allow ``python3 -m librarian`` as an alternative to bin/librarian."""

import sys

from librarian.cli import main

sys.exit(main())
