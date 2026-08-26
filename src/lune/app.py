"""Thin rumps menu application entry point."""

from __future__ import annotations

from typing import Any

from lune.paths import LunePaths
from lune.readiness import check_readiness


def menu_title() -> str:
    readiness = check_readiness(LunePaths.defaults())
    return "Lune · Setup Required" if readiness.state == "setup_required" else "Lune · Mic Off"


def main() -> int:
    try:
        import rumps  # type: ignore[import-untyped]
    except ImportError:
        return 3

    class LuneMenu(rumps.App):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(menu_title(), quit_button="Quit Lune")

        @rumps.clicked("Status")  # type: ignore[misc]
        def status(self, _: Any) -> None:
            readiness = check_readiness(LunePaths.defaults())
            rumps.alert(title="Lune", message=readiness.state)

    LuneMenu().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
