"""Allow running Aperio with ``python -m aperio``."""

import sys

from aperio.cli import main

if __name__ == "__main__":
    sys.exit(main())
