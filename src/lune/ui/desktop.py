"""The small native shell that hosts Lune's authenticated local Web UI.

The renderer is intentionally just a bundled ``file:`` page.  It receives the
one-time WebSocket credential through pywebview's JavaScript bridge rather than
through a URL, environment variable, command line, or a log.  Keeping the
process launch and GUI module injectable makes this boundary unit-testable
without opening a window or touching any audio device.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self, cast

from lune.ipc.contracts import PROTOCOL_VERSION, IPCConnectionInfo

HANDOFF_TIMEOUT_S = 8.0
CLOSE_GRACE_PERIOD_S = 1.0
MAX_HANDOFF_BYTES = 512
WINDOW_TITLE = "Lune"
WINDOW_WIDTH = 1_280
WINDOW_HEIGHT = 820
WINDOW_MIN_SIZE = (960, 640)
WINDOW_SHUTDOWN_SCRIPT = "window.__luneShutdown && window.__luneShutdown()"


class DesktopStartupError(RuntimeError):
    """An opaque desktop startup failure safe to return to the app entry point."""


@dataclass(frozen=True, slots=True)
class UiBootstrap:
    """The renderer-facing form of a validated one-time engine handoff."""

    url: str
    protocol: int
    token: str = field(repr=False)

    def payload(self) -> dict[str, int | str]:
        """Produce the exact JSON-compatible value consumed by the static UI."""

        return {"url": self.url, "protocol": self.protocol, "token": self.token}


class OneTimeBootstrapBridge:
    """Expose the credential to the bundled renderer exactly once.

    pywebview invokes JavaScript APIs from a backend thread, so the consumed
    flag is protected even though the normal UI calls this method only once.
    Returning ``None`` after consumption is deliberate: an old renderer cannot
    recover a token after a local WebSocket disconnect.
    """

    def __init__(self, bootstrap: UiBootstrap) -> None:
        self._bootstrap = bootstrap
        self._consumed = False
        self._lock = threading.Lock()

    def get_bootstrap(self) -> dict[str, int | str] | None:
        """Return the private handoff once, then make it unavailable."""

        with self._lock:
            if self._consumed:
                return None
            self._consumed = True
            return self._bootstrap.payload()


class EngineChild(Protocol):
    """The deliberately small child-process surface the shell needs."""

    @property
    def stdout(self) -> object: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...


class EngineLauncher(Protocol):
    """Create the engine child with a fixed, non-shell command."""

    def start(self, argv: Sequence[str]) -> EngineChild: ...


class ClosingEvent(Protocol):
    """The pywebview event hook used for orderly child teardown."""

    def __iadd__(self, callback: Callable[[], object]) -> Self: ...


class WindowEvents(Protocol):
    closing: ClosingEvent


class WebviewWindow(Protocol):
    """Only the two window capabilities used by the shell."""

    events: WindowEvents

    def evaluate_js(self, script: str) -> object: ...


class WebviewRuntime(Protocol):
    """Small injectable portion of the pywebview module."""

    def create_window(
        self,
        title: str,
        *,
        url: str,
        js_api: object,
        width: int,
        height: int,
        min_size: tuple[int, int],
    ) -> WebviewWindow | None: ...

    def start(self) -> None: ...


class SubprocessEngineLauncher:
    """The production engine launcher; command arguments are fixed by the shell."""

    def start(self, argv: Sequence[str]) -> EngineChild:
        # The argv is a fixed local Python module invocation.  There is no shell
        # and no user-controlled token or transcript is included in it.
        return cast(
            EngineChild,
            subprocess.Popen(  # noqa: S603
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="strict",
            ),
        )


def bundled_index_path() -> Path:
    """Locate the bundled static renderer without relying on a working directory."""

    return Path(__file__).with_name("static") / "index.html"


def parse_engine_handoff(line: str) -> UiBootstrap:
    """Parse the child-only handoff line into the renderer's finite schema.

    The child writes ``port``, not a WebSocket URL.  Constructing the URL here
    keeps the renderer unaware of loopback binding details and validates the
    protocol/token using the shared IPC contract.
    """

    if not isinstance(line, str) or not line.endswith("\n"):
        raise DesktopStartupError("invalid_engine_handoff")
    try:
        if len(line.encode("utf-8")) > MAX_HANDOFF_BYTES:
            raise DesktopStartupError("invalid_engine_handoff")
    except UnicodeEncodeError as error:
        raise DesktopStartupError("invalid_engine_handoff") from error
    raw = line[:-1]
    if not raw or "\r" in raw or "\n" in raw:
        raise DesktopStartupError("invalid_engine_handoff")
    try:
        decoded = json.loads(raw, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise DesktopStartupError("invalid_engine_handoff") from error
    if not isinstance(decoded, dict) or set(decoded) != {"port", "protocol", "token"}:
        raise DesktopStartupError("invalid_engine_handoff")
    port = decoded["port"]
    protocol = decoded["protocol"]
    token = decoded["token"]
    if (
        type(port) is not int
        or type(protocol) is not int
        or protocol != PROTOCOL_VERSION
        or not isinstance(token, str)
    ):
        raise DesktopStartupError("invalid_engine_handoff")
    try:
        connection = IPCConnectionInfo(port=port, protocol=protocol, token=token)
    except ValueError as error:
        raise DesktopStartupError("invalid_engine_handoff") from error
    return UiBootstrap(url=connection.url, protocol=connection.protocol, token=connection.token)


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys instead of silently choosing a credential."""

    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def read_engine_handoff(
    process: EngineChild,
    *,
    timeout_s: float = HANDOFF_TIMEOUT_S,
) -> UiBootstrap:
    """Read the sole bounded handoff line without blocking desktop startup forever."""

    if timeout_s <= 0:
        raise ValueError("handoff timeout must be positive")
    stdout = process.stdout
    if not hasattr(stdout, "readline"):
        raise DesktopStartupError("missing_engine_handoff")
    reader = cast(_ReadableTextStream, stdout)
    result_queue: queue.Queue[str | Exception] = queue.Queue(maxsize=1)
    thread = threading.Thread(
        target=_read_handoff_line,
        args=(reader, result_queue),
        name="lune-ui-handoff-reader",
        daemon=True,
    )
    thread.start()
    try:
        result = result_queue.get(timeout=timeout_s)
    except queue.Empty as error:
        raise DesktopStartupError("missing_engine_handoff") from error
    if isinstance(result, Exception):
        raise DesktopStartupError("missing_engine_handoff") from result
    return parse_engine_handoff(result)


class _ReadableTextStream(Protocol):
    def readline(self, size: int = -1) -> str: ...


def _read_handoff_line(
    stream: _ReadableTextStream,
    results: queue.Queue[str | Exception],
) -> None:
    try:
        # Limiting the initial read also protects the parent if a broken child
        # prints a large diagnostic instead of the compact private handoff.
        results.put_nowait(stream.readline(MAX_HANDOFF_BYTES + 1))
    except Exception as error:  # pragma: no cover - platform pipe failure
        results.put_nowait(error)


def _load_pywebview() -> WebviewRuntime:
    """Load the optional native backend only in the desktop entry path."""

    try:
        import webview
    except ImportError as error:
        raise DesktopStartupError("pywebview_unavailable") from error
    return cast(WebviewRuntime, webview)


@dataclass(slots=True)
class DesktopShell:
    """Own one renderer window and exactly one local engine child process."""

    launcher: EngineLauncher = field(default_factory=SubprocessEngineLauncher)
    webview_loader: Callable[[], WebviewRuntime] = _load_pywebview
    static_index: Path = field(default_factory=bundled_index_path)
    python_executable: str = sys.executable
    handoff_timeout_s: float = HANDOFF_TIMEOUT_S
    close_grace_period_s: float = CLOSE_GRACE_PERIOD_S
    _process: EngineChild | None = field(init=False, default=None, repr=False)
    _window: WebviewWindow | None = field(init=False, default=None, repr=False)
    _shutdown_attempted: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        if self.handoff_timeout_s <= 0:
            raise ValueError("handoff timeout must be positive")
        if self.close_grace_period_s <= 0:
            raise ValueError("close grace period must be positive")

    def run(self) -> int:
        """Launch the child, hand its one-time credential to the local page, and run GUI."""

        try:
            static_url = self._static_url()
            process = self.launcher.start(self._engine_argv())
            self._process = process
            bootstrap = read_engine_handoff(process, timeout_s=self.handoff_timeout_s)
            bridge = OneTimeBootstrapBridge(bootstrap)
            webview = self.webview_loader()
            window = webview.create_window(
                WINDOW_TITLE,
                url=static_url,
                js_api=bridge,
                width=WINDOW_WIDTH,
                height=WINDOW_HEIGHT,
                min_size=WINDOW_MIN_SIZE,
            )
            if window is None:
                raise DesktopStartupError("window_unavailable")
            self._window = window
            window.events.closing += self._on_window_closing
            webview.start()
            return 0
        except Exception:
            # Native backend/process exceptions can contain filesystem or
            # platform details.  The app entry point receives only a status.
            return 3
        finally:
            if not self._shutdown_attempted:
                self._stop_child(wait_for_renderer=False)

    def _engine_argv(self) -> tuple[str, str, str, str]:
        return (self.python_executable, "-m", "lune.engine", "--ui-ipc")

    def _static_url(self) -> str:
        if not self.static_index.is_file():
            raise DesktopStartupError("static_ui_missing")
        return self.static_index.resolve().as_uri()

    def _on_window_closing(self) -> None:
        """Ask the live renderer to use IPC shutdown before sending SIGTERM."""

        window = self._window
        if window is not None:
            with suppress(Exception):
                window.evaluate_js(WINDOW_SHUTDOWN_SCRIPT)
        self._stop_child(wait_for_renderer=True)

    def _stop_child(self, *, wait_for_renderer: bool) -> None:
        """Wait briefly for IPC shutdown, then terminate only our own child."""

        if self._shutdown_attempted:
            return
        self._shutdown_attempted = True
        process = self._process
        if process is None or _has_exited(process):
            return
        if wait_for_renderer and _wait_for_exit(process, self.close_grace_period_s):
            return
        try:
            process.terminate()
        except (OSError, ValueError):
            return
        _wait_for_exit(process, self.close_grace_period_s)


def _has_exited(process: EngineChild) -> bool:
    try:
        return process.poll() is not None
    except (OSError, ValueError):
        return True


def _wait_for_exit(process: EngineChild, timeout_s: float) -> bool:
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False
    except (OSError, ValueError):
        return _has_exited(process)
    return True


def run_desktop() -> int:
    """Run Lune's native desktop shell with production collaborators."""

    return DesktopShell().run()
