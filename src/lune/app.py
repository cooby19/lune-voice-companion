"""Desktop application entry point, kept separate from the voice engine child."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Run the native window shell, preserving the explicit physical-smoke route."""

    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["--physical-smoke"]:
        from lune.physical_smoke import main as physical_smoke_main

        return physical_smoke_main(arguments[1:])

    # Importing the desktop module does not import pywebview until it is needed,
    # so normal package checks and the physical smoke command stay GUI-free.
    from lune.ui.desktop import run_desktop

    return run_desktop()


if __name__ == "__main__":
    raise SystemExit(main())
