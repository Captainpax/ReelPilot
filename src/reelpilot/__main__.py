"""Allow ``python -m reelpilot`` to invoke the supported command-line entry point."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
