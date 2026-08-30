"""Claims about the shipped page, checked against the shipped files.

`src/lune/ui/static/` has no build step and no JavaScript test runner, so a
selector that matches nothing and a branch that can never be true both ship in
silence.  Each test here reads the real asset and pins one narrow claim; none
of them says the window looks right, which still needs a person.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

import lune.ui
from lune.audio.coreaudio import MicrophoneAuthorizationStatus
from tests.ui.test_runtime import _setup_runtime

STATIC = Path(lune.ui.__file__).parent / "static"


def _asset(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    """Return one brace-balanced function, so a claim cannot match elsewhere."""

    start = source.index(f"function {name}(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name}() is not brace balanced")


def _styled_class_names(css: str) -> set[str]:
    names: set[str] = set()
    for selector in re.findall(r"([^{}]+)\{", css):
        if selector.strip().startswith("@"):
            continue
        names.update(re.findall(r"\.([A-Za-z0-9_-]+)", selector))
    return names


def test_no_rule_styles_a_name_the_markup_only_uses_as_an_id() -> None:
    """`.setup-step-list` styled an element whose class is `step-list`.

    Both rules that used it were responsive ones, so the step list kept its
    single column at every width and nothing looked broken enough to notice.
    The id is right there in the same file, which is exactly why the typo
    reads as correct.
    """

    html = _asset("index.html")
    ids = set(re.findall(r'id="([^"]+)"', html))
    classes: set[str] = set()
    for value in re.findall(r'class="([^"]*)"', html):
        classes.update(value.split())
    js = _asset("app.js")
    for value in re.findall(r'className[:=]\s*"([^"]*)"', js):
        classes.update(value.split())
    for value in re.findall(r"className[:=]\s*`([^`]*)", js):
        classes.update(re.sub(r"\$\{.*", " ", value).split())

    styled_ids = sorted(name for name in _styled_class_names(_asset("app.css")) & ids)
    assert [name for name in styled_ids if name not in classes] == []


def test_every_setup_step_stays_openable() -> None:
    """Step 4 was listed and permanently disabled.

    The gate wanted the local, model and persona reasons all clear, which is
    the state `check_readiness` reports as `mic_off`: setup ends and the whole
    screen goes away.  A step nobody can open is not a step.
    """

    body = _function_body(_asset("app.js"), "renderSetup")
    assert "disabled" not in body


@pytest.mark.asyncio
async def test_every_command_the_setup_cards_offer_runs_without_an_engine(tmp_path: Path) -> None:
    """Setup deliberately runs without an engine, so it must not need one.

    Step 4's own button is the reason this is checked from both ends: it used
    to call a command only a running engine could serve, which is why the card
    ended up with nothing it could do.
    """

    source = _asset("app.js")
    cards = source[source.index("function renderSetup(") : source.index("function statusFor(")]
    offered = set(re.findall(r'command: "([a-z_]+)"', cards))
    assert "request_microphone_access" in offered

    runtime = await _setup_runtime(tmp_path, "persona_unconfigured")
    try:
        for command in sorted(offered):
            await runtime.handle(command, {})
    finally:
        await runtime.close()


def test_the_audio_card_has_copy_for_every_permission_state_the_runtime_sends() -> None:
    """Step 4 now reports a macOS decision, so every decision needs an answer.

    A missing key would silently fall back to the "cannot read it" copy and
    tell the user the opposite of what macOS actually said.
    """

    source = _asset("app.js")
    start = source.index("const MICROPHONE_CARD_COPY = {")
    block = source[start : source.index("\n  };", start)]
    assert set(re.findall(r"^\s{4}([a-z]+):", block, re.M)) == set(
        get_args(MicrophoneAuthorizationStatus)
    )


def test_every_whole_snapshot_marks_the_client_ready() -> None:
    """The reply to `get_status` is the only state a static setup screen gets.

    Applying it without clearing the connection line left the shell reporting
    「正在取得 Lune 的目前狀態…」 with `aria-busy="true"` for the whole session.
    """

    source = _asset("app.js")
    assert "applySnapshot(" not in _function_body(source, "receiveSocketMessage")
    accept = _function_body(source, "acceptSnapshot")
    assert "state.ready = true;" in accept
    assert 'setConnectionText("", "connected");' in accept
