from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from lune.config import AppConfig, PersonaKernel
from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore
from lune.paths import LunePaths
from lune.readiness import Readiness
from lune.ui.runtime import UiCommandError, UiRuntime


class FakeEngine:
    """A no-hardware engine surface for UI command tests."""

    def __init__(self) -> None:
        self._store = MemoryStore.ephemeral()
        self._session_id = self._store.start_session("thread-one")
        self._state = "mic_off"
        self._microphone_requested = False
        self._degraded_tts = False
        self._permission_requests = 0
        self._device_refreshes = 0
        self.submissions: list[tuple[str, bool]] = []
        self.closed = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def store(self) -> MemoryStore:
        return self._store

    @property
    def microphone_requested(self) -> bool:
        return self._microphone_requested

    @property
    def degraded_tts(self) -> bool:
        return self._degraded_tts

    @property
    def output_is_builtin(self) -> bool:
        return False

    @property
    def permission_requests(self) -> int:
        return self._permission_requests

    @property
    def device_refreshes(self) -> int:
        return self._device_refreshes

    async def start(self) -> str:
        return self._state

    async def set_microphone(self, enabled: bool) -> str:
        self._microphone_requested = enabled
        self._state = "listening" if enabled else "mic_off"
        return self._state

    async def request_microphone_access(self) -> None:
        self._permission_requests += 1

    async def refresh_devices(self) -> str:
        self._device_refreshes += 1
        return self._state

    async def submit_text(self, text: str, *, speak_text: bool = True) -> str:
        self.submissions.append((text, speak_text))
        return self._state

    def select_thread(self, thread_id: str) -> None:
        if self._store.get_conversation_thread(thread_id) is None:
            raise ValueError("unknown thread")
        if self._microphone_requested:
            raise RuntimeError("call active")
        self._session_id = thread_id

    async def close(self) -> None:
        self.closed = True
        self._store.close()


def _ready(_paths: LunePaths) -> Readiness:
    return Readiness("mic_off", ())


async def _runtime(tmp_path: Path) -> tuple[UiRuntime, FakeEngine]:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    AppConfig().save(paths.config)
    engine = FakeEngine()

    async def build() -> FakeEngine:
        return engine

    runtime = UiRuntime(paths, build, readiness_checker=_ready)
    await runtime.start()
    return runtime, engine


@pytest.mark.asyncio
async def test_snapshot_is_test_phase_and_call_bound_thread_is_read_only(tmp_path: Path) -> None:
    runtime, engine = await _runtime(tmp_path)
    try:
        initial = runtime.snapshot()
        assert initial["app"]["test_phase"] is True
        assert initial["app"]["provider"] == "local_qwen"
        assert initial["active_thread_id"] == "thread-one"

        created = await runtime.handle("create_thread", {})
        second = str(created["active_thread_id"])
        await runtime.handle("set_microphone", {"enabled": True, "thread_id": second})
        await runtime.handle("select_thread", {"thread_id": "thread-one"})

        readonly = runtime.snapshot()
        assert readonly["call"]["readonly"] is True
        with pytest.raises(UiCommandError, match="thread_read_only"):
            await runtime.handle("submit_text", {"text": "不應寫到旁邊的對話"})

        await runtime.handle("select_thread", {"thread_id": second})
        await runtime.handle("submit_text", {"text": "正在通話的文字", "speak": False})
        assert engine.submissions == [("正在通話的文字", False)]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_persona_has_safe_hidden_defaults_and_audio_recheck_does_not_open_mic(
    tmp_path: Path,
) -> None:
    runtime, engine = await _runtime(tmp_path)
    try:
        await runtime.handle("request_microphone_access", {})
        await runtime.handle("check_audio_devices", {})
        assert engine.permission_requests == 1
        assert engine.device_refreshes == 1
        assert engine.microphone_requested is False

        await runtime.handle(
            "save_persona",
            {
                "chinese_ratio": 0.67,
                "initiative": "proactive",
                "response_length": "short",
                "voice": "system",
            },
        )
        saved = PersonaKernel.load(runtime.paths.persona)
        assert saved.identity.name == "Lune"
        assert saved.style.traits == ("溫柔", "坦誠")
        assert saved.language.chinese_ratio == pytest.approx(0.67)
        assert saved.proactivity.level == "主動"
        assert saved.style.default_sentences.max == 2
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_sticky_tts_fallback_is_visible_when_the_call_is_idle(tmp_path: Path) -> None:
    runtime, engine = await _runtime(tmp_path)
    try:
        engine._degraded_tts = True
        snapshot = runtime.snapshot()
        assert snapshot["app"]["state"] == "degraded_tts"

        await runtime.handle("set_microphone", {"enabled": True})
        assert runtime.snapshot()["app"]["state"] == "listening"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_forget_memory_requires_exact_ui_confirmation(tmp_path: Path) -> None:
    runtime, engine = await _runtime(tmp_path)
    try:
        turn_id = engine.store.begin_turn("thread-one", 1, turn_id="turn-one")
        engine.store.accept_user_transcript(turn_id, "使用者訊息")
        engine.store.append_assistant_text_delivery(turn_id, "Lune 回覆")
        engine.store.complete_turn(turn_id)
        vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
        vector[0] = 1.0
        stored = engine.store.add_memory(
            memory_id="memory-one",
            content="要保留的記憶",
            category="explicit_request",
            importance=0.8,
            embedding=vector,
            embedding_model="test",
            embedding_revision="test",
            source_turn_id=turn_id,
        )
        assert stored is not None

        with pytest.raises(UiCommandError, match="confirmation_mismatch"):
            await runtime.handle(
                "forget_memory", {"memory_id": "memory-one", "confirmation": "not-the-id"}
            )
        await runtime.handle(
            "forget_memory", {"memory_id": "memory-one", "confirmation": "memory-one"}
        )
        assert engine.store.list_memories() == ()
    finally:
        await runtime.close()


async def _setup_runtime(tmp_path: Path, *reasons: str) -> UiRuntime:
    """A runtime stopped at setup, so no engine and no private data are needed."""

    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")

    async def build() -> FakeEngine:
        raise AssertionError("setup must not start the engine")

    def blocked(_paths: LunePaths) -> Readiness:
        return Readiness("setup_required", reasons)

    runtime = UiRuntime(paths, build, readiness_checker=blocked)
    await runtime.start()
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["config_missing", "config_invalid"])
async def test_a_config_reason_opens_a_repair_step_it_can_act_on(
    tmp_path: Path, reason: str
) -> None:
    runtime = await _setup_runtime(tmp_path, reason)
    try:
        setup = runtime.snapshot()["setup"]
        assert setup is not None
        # Without a step of its own the only reason left would match nothing and
        # `current_step` would fall through to the unrelated audio card.
        assert setup["current_step"] == "repair"
        repair = setup["steps"][0]
        assert repair["id"] == "repair"
        assert repair["status"] == "required"
        assert repair["complete"] is False
        assert reason in setup["reasons"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_a_config_reason_outranks_the_numbered_steps(tmp_path: Path) -> None:
    runtime = await _setup_runtime(tmp_path, "persona_missing", "config_invalid")
    try:
        setup = runtime.snapshot()["setup"]
        assert setup is not None
        assert setup["current_step"] == "repair"
        assert str(setup["steps"][0]["id"]) == "repair"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_a_readable_config_leaves_the_numbered_steps_alone(tmp_path: Path) -> None:
    runtime = await _setup_runtime(tmp_path, "persona_unconfigured")
    try:
        setup = runtime.snapshot()["setup"]
        assert setup is not None
        assert setup["current_step"] == "persona"
        assert all(str(step["id"]) != "repair" for step in setup["steps"])
    finally:
        await runtime.close()
