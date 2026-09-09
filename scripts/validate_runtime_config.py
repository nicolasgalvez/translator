"""Validate launcher configuration before dependency and runtime side effects."""

import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_config import RuntimeConfig  # pylint: disable=wrong-import-position


def main() -> int:
    """Return a shell-friendly status after validating the process environment."""
    try:
        RuntimeConfig.from_environment(os.environ)
    except ValueError as exc:
        print(f"translator: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
