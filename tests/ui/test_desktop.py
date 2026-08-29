from __future__ import annotations

import io
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from lune.ipc.contracts import PROTOCOL_VERSION
from lune.ui.desktop import (
    WINDOW_SHUTDOWN_SCRIPT,
    DesktopShell,
    DesktopStartupError,
    OneTimeBootstrapBridge,
    UiBootstrap,
    parse_engine_handoff,
)


class FakeProcess:
    def __init__(self, handoff: str, *, exits_during_grace: bool = False) -> None:
        self.stdout = io.StringIO(handoff)
        self._exited = False
        self._exits_during_grace = exits_during_grace
        self.terminated = 0
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return 0 if self._exited else None

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self._exited:
            return 0
        if self._exits_during_grace:
            self._exited = True
            return 0
        raise subprocess.TimeoutExpired("lune-engine", timeout)

    def terminate(self) -> None:
        self.terminated += 1
        self._exited = True


class FakeLauncher:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.argv: tuple[str, ...] | None = None

    def start(self, argv: Sequence[str]) -> FakeProcess:
        self.argv = tuple(argv)
        return self.process


class FakeClosingEvent:
    def __init__(self) -> None:
        self._callbacks: list[Callable[[], object]] = []

    def __iadd__(self, callback: Callable[[], object]) -> FakeClosingEvent:
        self._callbacks.append(callback)
        return self

    def fire(self) -> None:
        for callback in tuple(self._callbacks):
            callback()


class FakeWindowEvents:
    def __init__(self) -> None:
        self.closing = FakeClosingEvent()


class FakeWindow:
    def __init__(self) -> None:
        self.events = FakeWindowEvents()
        self.evaluated_scripts: list[str] = []

    def evaluate_js(self, script: str) -> None:
        self.evaluated_scripts.append(script)


class FakeWebview:
    def __init__(self) -> None:
        self.window: FakeWindow | None = None
        self.window_args: dict[str, object] | None = None
        self.bootstrap: dict[str, int | str] | None = None

    def create_window(
        self,
        title: str,
        *,
        url: str,
        js_api: object,
        width: int,
        height: int,
        min_size: tuple[int, int],
    ) -> FakeWindow:
        self.window = FakeWindow()
        self.window_args = {
            "title": title,
            "url": url,
            "js_api": js_api,
            "width": width,
            "height": height,
            "min_size": min_size,
        }
        return self.window

    def start(self) -> None:
        assert self.window is not None
        assert self.window_args is not None
        bridge = self.window_args["js_api"]
        assert isinstance(bridge, OneTimeBootstrapBridge)
        self.bootstrap = bridge.get_bootstrap()
        self.window.events.closing.fire()


def _handoff(*, token: str = "one-time-token") -> str:  # noqa: S107 - inert test fixture
    return f'{{"port":43123,"protocol":{PROTOCOL_VERSION},"token":"{token}"}}\n'


def test_validated_handoff_becomes_renderer_bootstrap_without_token_repr() -> None:
    bootstrap = parse_engine_handoff(_handoff(token="private-token"))  # noqa: S106 - test fixture

    assert bootstrap.url == "ws://127.0.0.1:43123"
    assert bootstrap.protocol == PROTOCOL_VERSION
    assert bootstrap.payload() == {
        "url": "ws://127.0.0.1:43123",
        "protocol": PROTOCOL_VERSION,
        "token": "private-token",
    }
    assert "private-token" not in repr(bootstrap)


@pytest.mark.parametrize(
    "line",
    (
        "",
        '{"port":43123,"protocol":1,"token":"missing-newline"}',
        '{"port":43123,"protocol":1,"token":"duplicate","token":"duplicate"}\n',
        '{"port":0,"protocol":1,"token":"bad-port"}\n',
        '{"port":43123,"protocol":999,"token":"bad-protocol"}\n',
        '{"url":"ws://127.0.0.1:43123","protocol":1,"token":"wrong-shape"}\n',
    ),
)
def test_invalid_engine_handoff_is_rejected_without_parsing_fallback(line: str) -> None:
    with pytest.raises(DesktopStartupError):
        parse_engine_handoff(line)


def test_bootstrap_bridge_releases_the_private_value_once() -> None:
    bridge = OneTimeBootstrapBridge(
        UiBootstrap(
            url="ws://127.0.0.1:43123",
            protocol=PROTOCOL_VERSION,
            token="private-token",  # noqa: S106 - test fixture
        )
    )

    assert bridge.get_bootstrap() == {
        "url": "ws://127.0.0.1:43123",
        "protocol": PROTOCOL_VERSION,
        "token": "private-token",
    }
    assert bridge.get_bootstrap() is None
    assert "private-token" not in repr(bridge)


def test_desktop_shell_uses_local_file_bridge_and_sigterm_fallback() -> None:
    process = FakeProcess(_handoff())
    launcher = FakeLauncher(process)
    webview = FakeWebview()
    index = Path(__file__).parents[2] / "src" / "lune" / "ui" / "static" / "index.html"
    shell = DesktopShell(
        launcher=launcher,
        webview_loader=lambda: webview,
        static_index=index,
        python_executable="/fixed/python",
        handoff_timeout_s=0.1,
        close_grace_period_s=0.01,
    )

    assert shell.run() == 0
    assert launcher.argv == ("/fixed/python", "-m", "lune.engine", "--ui-ipc")
    assert webview.bootstrap == {
        "url": "ws://127.0.0.1:43123",
        "protocol": PROTOCOL_VERSION,
        "token": "one-time-token",
    }
    assert webview.window_args is not None
    assert webview.window_args["url"] == index.resolve().as_uri()
    assert webview.window is not None
    assert webview.window.evaluated_scripts == [WINDOW_SHUTDOWN_SCRIPT]
    assert process.terminated == 1
    assert process.wait_timeouts == [0.01, 0.01]


def test_desktop_shell_keeps_a_renderer_orderly_shutdown_when_child_exits_in_grace_period() -> None:
    process = FakeProcess(_handoff(), exits_during_grace=True)
    webview = FakeWebview()
    shell = DesktopShell(
        launcher=FakeLauncher(process),
        webview_loader=lambda: webview,
        static_index=Path(__file__).parents[2] / "src" / "lune" / "ui" / "static" / "index.html",
        handoff_timeout_s=0.1,
        close_grace_period_s=0.01,
    )

    assert shell.run() == 0
    assert process.terminated == 0
    assert process.wait_timeouts == [0.01]


def test_invalid_child_handoff_does_not_open_gui_and_terminates_child() -> None:
    process = FakeProcess("not-json\n")
    launcher = FakeLauncher(process)
    webview_requested = False

    def load_webview() -> FakeWebview:
        nonlocal webview_requested
        webview_requested = True
        return FakeWebview()

    shell = DesktopShell(
        launcher=launcher,
        webview_loader=load_webview,
        static_index=Path(__file__).parents[2] / "src" / "lune" / "ui" / "static" / "index.html",
        handoff_timeout_s=0.1,
        close_grace_period_s=0.01,
    )

    assert shell.run() == 3
    assert webview_requested is False
    assert process.terminated == 1
