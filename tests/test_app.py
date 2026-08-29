from __future__ import annotations

import pytest

from lune import app, physical_smoke


def test_app_dispatches_explicit_physical_smoke_without_starting_rumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str] | None] = []

    def run(argv: list[str] | None = None) -> int:
        received.append(argv)
        return 17

    monkeypatch.setattr(physical_smoke, "main", run)

    assert app.main(["--physical-smoke", "preflight"]) == 17
    assert received == [["preflight"]]


def test_app_dispatches_the_window_shell_without_importing_a_menu_bar_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lune.ui import desktop

    monkeypatch.setattr(desktop, "run_desktop", lambda: 23)

    assert app.main([]) == 23
