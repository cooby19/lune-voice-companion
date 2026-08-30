"""UI-facing application state with no logging of private conversation data."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError

from lune.config import (
    AppConfig,
    Boundaries,
    Identity,
    Language,
    PersonaKernel,
    Proactivity,
    SentenceBounds,
    Style,
    UserProfile,
)
from lune.ipc.contracts import JSONValue
from lune.memory.embedding import E5MemoryRetriever, E5SetupRequired, LocalE5Encoder
from lune.memory.store import (
    ConversationThread,
    MemoryStore,
    StoreChange,
    StoredMemory,
    StoredMessage,
)
from lune.paths import LunePaths
from lune.readiness import Readiness, check_readiness

# The IPC transport caps a complete JSON frame at 64 KiB, measured in bytes.
# Keep each authenticated snapshot comfortably below that limit even for CJK
# text (which commonly takes three UTF-8 bytes per character).
_MESSAGE_LIMIT = 18
_MEMORY_LIMIT = 16
_THREAD_LIMIT = 40
_DISPLAY_TEXT_BYTES = 512
_PROFILE_TEXT_BYTES = 2_000
_TITLE_TEXT_BYTES = 160
# `check_readiness()` writes the all-default file on first run, so a config
# reason that survives it means the file is unreadable or could not be
# created.  Neither may be silently overwritten, so both need a repair step
# rather than one of the numbered onboarding tasks.
_CONFIG_REASONS = frozenset({"config_missing", "config_invalid"})
_PROACTIVITY_LEVELS = frozenset({"安靜", "剛好", "主動"})
_VOICE_CHOICES = frozenset({"system", "private"})
_RESPONSE_LENGTHS = frozenset({"short", "normal"})


class UiCommandError(Exception):
    """A finite app-level rejection that can safely cross the authenticated IPC."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EngineControl(Protocol):
    """The narrow UI surface of a live engine, kept independent of pywebview."""

    @property
    def state(self) -> str: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def store(self) -> MemoryStore | None: ...

    @property
    def microphone_requested(self) -> bool: ...

    @property
    def degraded_tts(self) -> bool: ...

    @property
    def output_is_builtin(self) -> bool | None: ...

    async def start(self) -> str: ...

    async def set_microphone(self, enabled: bool) -> str: ...

    async def request_microphone_access(self) -> None: ...

    async def refresh_devices(self) -> str: ...

    async def submit_text(self, text: str, *, speak_text: bool = True) -> str: ...

    def select_thread(self, thread_id: str) -> None: ...

    async def close(self) -> None: ...


type EngineFactory = Callable[[], Awaitable[EngineControl]]
type ReadinessChecker = Callable[[LunePaths], Readiness]
# An incremental UI event and its already-bounded payload.  A sink is called
# from the task that committed the change, so it must hand the frame off
# without blocking and without raising back into the store.
type UiEventSink = Callable[[str, dict[str, JSONValue]], None]

_DEFAULT_IDENTITY_NAME = "Lune"
_DEFAULT_PRESENTATION = "一位在這台 Mac 上陪你對話的語音夥伴"
_DEFAULT_USER_ADDRESS = "你"
_DEFAULT_TRAITS = ("溫柔", "坦誠")


@dataclass(slots=True)
class UiRuntime:
    """Own UI selection state and translate authenticated commands to local work.

    This class deliberately never emits diagnostics.  Its snapshots contain
    private text only for the authenticated local WebView that requested it.
    """

    paths: LunePaths
    engine_factory: EngineFactory
    monotonic: Callable[[], float] = time.monotonic
    readiness_checker: ReadinessChecker = check_readiness
    event_sink: UiEventSink | None = None
    _engine: EngineControl | None = field(init=False, default=None, repr=False)
    _readiness: Readiness | None = field(init=False, default=None, repr=False)
    _active_thread_id: str | None = field(init=False, default=None)
    _call_thread_id: str | None = field(init=False, default=None)
    _call_started_at: float | None = field(init=False, default=None)
    _speak_text: bool = field(init=False, default=True)
    _profile: UserProfile = field(init=False, default_factory=UserProfile, repr=False)
    _persona: PersonaKernel | None = field(init=False, default=None, repr=False)
    _fault: str | None = field(init=False, default=None)
    _started: bool = field(init=False, default=False)
    _starting: bool = field(init=False, default=False)
    _shutdown: asyncio.Event = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._shutdown = asyncio.Event()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown.is_set()

    async def wait_for_shutdown(self) -> None:
        """Wait for the authenticated shell to request an orderly stop."""

        await self._shutdown.wait()

    async def start(self) -> None:
        if self._started:
            return
        if self._starting:
            return
        self._starting = True
        try:
            await self.refresh_setup()
        except Exception:
            # Startup exceptions can carry paths or model diagnostics.  Keep
            # the shell available and expose only an opaque recoverable state.
            self._readiness = Readiness("mic_off", ())
            self._fault = "startup_unavailable"
        finally:
            self._starting = False
            self._started = True

    async def close(self) -> None:
        engine = self._engine
        self._engine = None
        self._call_thread_id = None
        self._call_started_at = None
        if engine is None:
            return
        # Detach first: a listener firing against a closing store would read
        # rows this runtime is no longer able to broadcast.
        if engine.store is not None:
            engine.store.set_change_listener(None)
        await engine.close()

    async def refresh_setup(self) -> None:
        """Refresh opaque readiness and start the engine only when it is safe."""

        self._readiness = self.readiness_checker(self.paths)
        if self._readiness.state == "setup_required" or self._engine is not None:
            self._load_local_preferences()
            return
        try:
            engine = await self.engine_factory()
            await engine.start()
        except Exception:
            # The underlying message could reveal a local path or private
            # model state.  The UI only needs a recoverable finite condition.
            self._fault = "engine_unavailable"
            return
        store = engine.store
        if store is None or engine.session_id is None:
            await engine.close()
            self._fault = "engine_unavailable"
            return
        self._engine = engine
        self._active_thread_id = engine.session_id
        self._fault = None
        # The engine writes conversation and memory rows on its own tasks, so
        # the store is the only place that sees every UI-visible change.
        store.set_change_listener(self._on_store_change)
        self._load_local_preferences()

    async def handle(self, command: str, params: Mapping[str, JSONValue]) -> JSONValue:
        """Run a fixed UI command; callers serialize only the returned snapshot."""

        if command == "get_status":
            return self.snapshot()
        if command == "shutdown":
            self._shutdown.set()
            return {"shutdown": True}
        if not self._started:
            raise UiCommandError("starting")
        if command == "check_local_runtime":
            # There is intentionally no model downloader.  Rechecking is safe;
            # pretending to fetch a model would weaken the pinned-artifact policy.
            await self.refresh_setup()
            return self.snapshot()
        if command == "check_audio_devices":
            await self._check_audio_devices()
            return self.snapshot()
        if command == "create_thread":
            self._create_thread()
            return self.snapshot()
        if command == "select_thread":
            self._select_thread(_required_string(params, "thread_id", maximum=128))
            return self.snapshot()
        if command == "rename_thread":
            self._rename_thread(
                _required_string(params, "thread_id", maximum=128),
                _required_string(params, "title", maximum=160),
            )
            return self.snapshot()
        if command == "request_microphone_access":
            await self._request_microphone_access()
            return self.snapshot()
        if command == "set_microphone":
            requested_thread = _optional_string(params, "thread_id", maximum=128)
            if requested_thread and requested_thread != self._active_thread_id:
                raise UiCommandError("thread_not_found")
            await self._set_microphone(_required_bool(params, "enabled"))
            return self.snapshot()
        if command == "submit_text":
            requested_thread = _optional_string(params, "thread_id", maximum=128)
            if requested_thread and requested_thread != self._active_thread_id:
                raise UiCommandError("thread_not_found")
            if "speak" in params:
                self._speak_text = _required_bool(params, "speak")
            await self._submit_text(_required_string(params, "text", maximum=20_000))
            return self.snapshot()
        if command == "set_text_speech":
            self._speak_text = _required_bool(params, "enabled")
            return self.snapshot()
        if command == "forget_memory":
            memory_id = _required_string(params, "memory_id", maximum=128)
            confirmation = _required_string(params, "confirmation", maximum=128)
            if memory_id != confirmation:
                raise UiCommandError("confirmation_mismatch")
            if not self._store().forget_memory(memory_id):
                raise UiCommandError("memory_not_found")
            return self.snapshot()
        if command == "search_memories":
            query = _required_string(params, "query", maximum=2_000)
            return {"results": self._search_memories(query)}
        if command == "save_user_profile":
            self._save_user_profile(params)
            return self.snapshot()
        if command == "save_persona":
            self._save_persona(params)
            await self.refresh_setup()
            return self.snapshot()
        if command == "set_voice":
            self._set_voice(_required_string(params, "voice", maximum=16))
            return self.snapshot()
        raise UiCommandError("unsupported_command")

    def snapshot(self) -> dict[str, JSONValue]:
        """Return a bounded private view for the already authenticated WebView."""

        if not self._started:
            return self._starting_snapshot()

        readiness = self._readiness or Readiness("setup_required", ("config_missing",))
        engine = self._engine
        store = engine.store if engine is not None else None
        active_thread = self._active_thread_id
        threads = () if store is None else store.list_conversation_threads(limit=_THREAD_LIMIT)
        if active_thread is None and threads:
            active_thread = threads[0].id
            self._active_thread_id = active_thread
        messages = (
            ()
            if store is None or active_thread is None
            else store.conversation_messages(active_thread)
        )
        memories = () if store is None else store.list_memories()
        state = engine.state if engine is not None else readiness.state
        # TTS fallback is sticky at the session level, but it is deliberately
        # not folded into the pipeline's idle state: a live listening/thinking
        # state must remain accurate during a call.  Surface it only while
        # idle so the non-call banner gives the user the prescribed explanation.
        if engine is not None and engine.degraded_tts and state == "mic_off":
            state = "degraded_tts"
        call_active = self._call_thread_id is not None
        elapsed_seconds = (
            0
            if self._call_started_at is None
            else max(0, int(self.monotonic() - self._call_started_at))
        )
        return {
            "setup": self._setup_view(readiness),
            "app": {
                "state": state,
                "local_only": True,
                "phase": "test",
                "test_phase": True,
                "provider": "local_qwen",
                "degraded_tts": bool(engine is not None and engine.degraded_tts),
                "fault": self._fault,
            },
            "threads": [_thread_view(thread) for thread in threads],
            "active_thread_id": active_thread,
            "call": {
                "active": call_active,
                "thread_id": self._call_thread_id,
                "readonly": bool(call_active and active_thread != self._call_thread_id),
                "elapsed_seconds": elapsed_seconds,
                "speak_text": self._speak_text,
            },
            "device": self._device_view(engine),
            "messages": [_message_view(message) for message in messages[-_MESSAGE_LIMIT:]],
            "memories": [_memory_view(memory) for memory in memories[:_MEMORY_LIMIT]],
            "profile": {
                "name": _display_text(self._profile.name, maximum_bytes=_TITLE_TEXT_BYTES),
                "context": _display_text(self._profile.context, maximum_bytes=_PROFILE_TEXT_BYTES),
            },
            "persona": self._persona_view(),
        }

    def _starting_snapshot(self) -> dict[str, JSONValue]:
        """A private-data-free shell state while local runtime setup is running."""

        return {
            "setup": None,
            "app": {
                "state": "mic_off",
                "local_only": True,
                "phase": "test",
                "test_phase": True,
                "provider": "local_qwen",
                "fault": "starting",
            },
            "threads": [],
            "active_thread_id": None,
            "call": {
                "active": False,
                "thread_id": None,
                "readonly": False,
                "elapsed_seconds": 0,
                "speak_text": True,
            },
            "device": {
                "label": "正在準備本機音訊與模型",
                "status": "quiet",
                "unsafe": False,
                "output_builtin": False,
            },
            "messages": [],
            "memories": [],
            "profile": {"name": "", "context": ""},
            "persona": self._persona_view(),
        }

    def _setup_view(self, readiness: Readiness) -> dict[str, JSONValue] | None:
        if readiness.state != "setup_required":
            return None
        reasons = set(readiness.reasons)
        steps: list[dict[str, JSONValue]] = [
            {
                "id": "local_runtime",
                "title": "確認本機 Qwen",
                "detail": "Lune 只會檢查已驗證的本機模型與 worker，不會代你下載。",
                "status": _step_status(
                    reasons,
                    {"local_llm_model_missing", "local_llm_runtime_missing"},
                ),
            },
            {
                "id": "local_models",
                "title": "放好語音與記憶模型",
                "detail": "Whisper 與記憶模型可在背景準備；此版本只會重新檢查它們。",
                "status": _step_status(
                    reasons,
                    {"whisper_model_missing", "embedding_model_missing"},
                ),
            },
            {
                "id": "persona",
                "title": "認識你與 Lune",
                "detail": "先完成結構化人格設定；範例人格不能直接啟用。",
                "status": _step_status(
                    reasons,
                    {"persona_missing", "persona_invalid", "persona_unconfigured"},
                ),
            },
            {
                "id": "microphone",
                "title": "麥克風與耳機",
                "detail": "等你按下「打給 Lune」時才會要求麥克風；內建喇叭會先暫停收音。",
                "status": "pending",
            },
            {
                "id": "voice",
                "title": "私人聲線（選配）",
                "detail": "可以之後再說，先用系統合成音也能完整對話。",
                "status": "optional",
            },
        ]
        if reasons & _CONFIG_REASONS:
            # Ahead of the numbered steps on purpose: nothing the user can fill
            # in matters while the configuration the app reads is unusable.
            steps.insert(
                0,
                {
                    "id": "repair",
                    "title": "先修好本機設定",
                    "detail": "Lune 不會覆寫一份讀不到或無法驗證的設定；確認之後再檢查一次。",
                    "status": "required",
                },
            )
        current = next(
            (
                _SETUP_STEP_VIEWS[str(step["id"])]
                for step in steps
                if step["status"] == "required" and str(step["id"]) in _SETUP_STEP_VIEWS
            ),
            "audio",
        )
        for step in steps:
            step["complete"] = step["status"] == "complete"
        view: dict[str, JSONValue] = {
            "reasons": list(readiness.reasons),
            "steps": [dict(step) for step in steps],
            "current_step": current,
            "local_only": True,
        }
        return view

    def _device_view(self, engine: EngineControl | None) -> dict[str, JSONValue]:
        if engine is None:
            return {
                "label": "音訊裝置會在準備完成後檢查",
                "status": "quiet",
                "unsafe": False,
                "output_builtin": False,
            }
        if engine.output_is_builtin is True:
            return {
                "label": "目前是內建喇叭",
                "status": "paused_unsafe_output",
                "unsafe": True,
                "output_builtin": True,
            }
        if engine.output_is_builtin is False:
            return {
                "label": "耳機已接上",
                "status": "ready",
                "unsafe": False,
                "output_builtin": False,
            }
        return {
            "label": "正在確認音訊裝置",
            "status": "quiet",
            "unsafe": False,
            "output_builtin": False,
        }

    def _persona_view(self) -> dict[str, JSONValue]:
        persona = self._persona
        if persona is None:
            return {
                "configured": False,
                "language_ratio": 0.8,
                "proactivity": "剛好",
                "response_length": "normal",
                "voice": self._voice_choice(),
            }
        return {
            "configured": True,
            "language_ratio": persona.language.chinese_ratio,
            "proactivity": persona.proactivity.level,
            "response_length": _response_length(persona.style.default_sentences),
            "voice": self._voice_choice(),
            "identity_name": persona.identity.name,
            "presentation": persona.identity.presentation,
            "user_address": persona.identity.user_address,
            "traits": list(persona.style.traits),
            "boundaries": [
                "她不會假裝自己是人",
                "她不會刻意讓你離不開她",
                "不知道的事她會說不知道",
            ],
        }

    def _load_local_preferences(self) -> None:
        try:
            self._profile = UserProfile.load(self.paths.profile)
        except (FileNotFoundError, OSError, ValueError, ValidationError):
            self._profile = UserProfile()
        try:
            self._persona = PersonaKernel.load(self.paths.persona)
        except (FileNotFoundError, OSError, ValueError, ValidationError):
            self._persona = None

    def _on_store_change(self, change: StoreChange) -> None:
        """Publish the incremental events for one committed store change.

        This runs on whichever task performed the write, so it reads back only
        the rows the change names and hands the frames to a sink that must not
        block.  Anything an event cannot express stays the snapshot's job.
        """

        sink = self.event_sink
        if sink is None:
            return
        for event, payload in self._change_events(change):
            sink(event, payload)

    def _change_events(self, change: StoreChange) -> list[tuple[str, dict[str, JSONValue]]]:
        """Build the bounded event payloads for one change, or none at all.

        Every payload reuses the same per-item view the snapshot renders, so
        the incremental channel cannot drift from the reconciling one.
        """

        engine = self._engine
        store = None if engine is None else engine.store
        if store is None:
            return []
        if change.kind == "memories":
            # The bounded list is what the memory view renders wholesale, and it
            # is far smaller than a snapshot, so a delete needs no separate event.
            return [
                (
                    "memory_updated",
                    {
                        "memories": [
                            _memory_view(memory) for memory in store.list_memories()[:_MEMORY_LIMIT]
                        ]
                    },
                )
            ]
        if change.thread_id is None:
            return []
        if change.kind == "thread":
            thread = store.get_conversation_thread(change.thread_id)
            return [] if thread is None else [("thread_updated", {"thread": _thread_view(thread)})]
        if change.turn_id is None:
            return []
        # One completed turn contributes a final user and assistant message at
        # most, so this stays bounded without a cap of its own.
        return [
            ("message_added", {"message": _message_view(message)})
            for message in store.conversation_messages(change.thread_id)
            if message.turn_id == change.turn_id
        ]

    def _store(self) -> MemoryStore:
        engine = self._engine
        if engine is None or engine.store is None:
            raise UiCommandError("setup_required")
        return engine.store

    def _create_thread(self) -> None:
        if self._call_thread_id is not None:
            raise UiCommandError("thread_read_only")
        store = self._store()
        thread_id = store.start_session()
        self._active_thread_id = thread_id
        if self._call_thread_id is None:
            self._select_engine_thread(thread_id)

    def _select_thread(self, thread_id: str) -> None:
        if self._store().get_conversation_thread(thread_id) is None:
            raise UiCommandError("thread_not_found")
        self._active_thread_id = thread_id
        if self._call_thread_id is None:
            self._select_engine_thread(thread_id)

    def _rename_thread(self, thread_id: str, title: str) -> None:
        if self._call_thread_id is not None and thread_id != self._call_thread_id:
            raise UiCommandError("thread_read_only")
        try:
            self._store().rename_conversation_thread(thread_id, title)
        except ValueError as error:
            raise UiCommandError("invalid_thread_title") from error

    async def _set_microphone(self, enabled: bool) -> None:
        engine = self._require_engine()
        if enabled:
            active = self._active_thread_id
            if active is None:
                raise UiCommandError("thread_not_found")
            if self._call_thread_id is not None and self._call_thread_id != active:
                raise UiCommandError("thread_read_only")
            self._select_engine_thread(active)
            await engine.set_microphone(True)
            self._call_thread_id = active
            self._call_started_at = self.monotonic()
            return
        await engine.set_microphone(False)
        self._call_thread_id = None
        self._call_started_at = None

    async def _request_microphone_access(self) -> None:
        """Show macOS's permission prompt without leaving a call active.

        This is only reached from an explicit UI click.  The engine delegates to
        its CoreAudio authorizer, which does not open the microphone stream.
        """

        engine = self._require_engine()
        try:
            await engine.request_microphone_access()
        except RuntimeError as error:
            raise UiCommandError("microphone_unavailable") from error

    async def _check_audio_devices(self) -> None:
        engine = self._engine
        if engine is None:
            await self.refresh_setup()
            return
        try:
            await engine.refresh_devices()
        except RuntimeError as error:
            raise UiCommandError("audio_unavailable") from error

    async def _submit_text(self, text: str) -> None:
        engine = self._require_engine()
        active = self._active_thread_id
        if active is None:
            raise UiCommandError("thread_not_found")
        if self._call_thread_id is not None and active != self._call_thread_id:
            raise UiCommandError("thread_read_only")
        self._select_engine_thread(active)
        try:
            await engine.submit_text(text, speak_text=self._speak_text)
        except ValueError as error:
            raise UiCommandError("invalid_text") from error

    def _select_engine_thread(self, thread_id: str) -> None:
        engine = self._require_engine()
        if engine.session_id != thread_id:
            try:
                engine.select_thread(thread_id)
            except (RuntimeError, ValueError) as error:
                raise UiCommandError("thread_read_only") from error

    def _search_memories(self, query: str) -> list[JSONValue]:
        if not query.strip():
            raise UiCommandError("invalid_query")
        try:
            retriever = E5MemoryRetriever(self._store(), LocalE5Encoder(self.paths.e5_manifest))
            results = retriever.search(query)
        except E5SetupRequired as error:
            raise UiCommandError("memory_search_unavailable") from error
        matches: list[JSONValue] = []
        for result in results:
            matches.append(
                {
                    "id": result.id,
                    "content": _display_text(result.content),
                    "match": "很接近" if result.score >= 0.84 else "有點接近",
                }
            )
        return matches

    def _save_user_profile(self, params: Mapping[str, JSONValue]) -> None:
        try:
            profile = UserProfile(
                name=_optional_string(params, "name", maximum=80),
                context=_optional_string(params, "context", maximum=8_000),
            )
            profile.save(self.paths.profile)
        except (OSError, ValidationError, ValueError) as error:
            raise UiCommandError("invalid_profile") from error
        self._profile = profile

    def _save_persona(self, params: Mapping[str, JSONValue]) -> None:
        try:
            persona = _persona_from_params(self._persona, self._profile, params)
            persona.save(self.paths.persona)
        except (OSError, ValidationError, ValueError) as error:
            raise UiCommandError("invalid_persona") from error
        self._persona = persona
        voice = _optional_string(params, "voice", maximum=16)
        if voice:
            self._set_voice(voice)

    def _set_voice(self, voice: str) -> None:
        if voice not in _VOICE_CHOICES:
            raise UiCommandError("invalid_voice")
        try:
            config = AppConfig.load(self.paths.config)
            preferred = "avspeech" if voice == "system" else "gpt_sovits"
            updated = config.model_copy(
                update={"tts": config.tts.model_copy(update={"preferred_backend": preferred})}
            )
            updated.save(self.paths.config)
        except (OSError, ValidationError, ValueError) as error:
            raise UiCommandError("invalid_voice") from error

    def _voice_choice(self) -> str:
        try:
            backend = AppConfig.load(self.paths.config).tts.preferred_backend
            return "private" if backend == "gpt_sovits" else "system"
        except (OSError, ValidationError, ValueError):
            return "system"

    def _require_engine(self) -> EngineControl:
        if self._engine is None:
            raise UiCommandError("setup_required")
        return self._engine


def _persona_from_params(
    current: PersonaKernel | None,
    profile: UserProfile,
    params: Mapping[str, JSONValue],
) -> PersonaKernel:
    ratio = _persona_ratio(params)
    proactivity = _persona_proactivity(params)
    response_length = _optional_string(params, "response_length", maximum=16) or "normal"
    if proactivity not in _PROACTIVITY_LEVELS or response_length not in _RESPONSE_LENGTHS:
        raise ValueError("invalid persona controls")
    longest = 2 if response_length == "short" else 3
    bounds = SentenceBounds(min=1, max=longest)
    if current is None:
        # The UI intentionally exposes only the four safe controls specified
        # for the test phase.  Stable identity, presentation and boundaries are
        # conservative application defaults rather than hidden free-form knobs.
        identity_name = (
            _optional_string(params, "identity_name", maximum=80) or _DEFAULT_IDENTITY_NAME
        )
        presentation = _optional_string(params, "presentation", maximum=80) or _DEFAULT_PRESENTATION
        user_address = (
            _optional_string(params, "user_address", maximum=80)
            or profile.name
            or _DEFAULT_USER_ADDRESS
        )
        traits = _traits(params) if "traits" in params else _DEFAULT_TRAITS
        return PersonaKernel(
            identity=Identity(
                name=identity_name,
                presentation=presentation,
                user_address=user_address,
            ),
            language=Language(chinese_ratio=ratio),
            style=Style(traits=traits, default_sentences=bounds),
            boundaries=Boundaries(),
            proactivity=Proactivity(level=proactivity),
        )
    data = current.model_dump(mode="python")
    data["language"]["chinese_ratio"] = ratio
    data["proactivity"]["level"] = proactivity
    data["style"]["default_sentences"] = {"min": bounds.min, "max": bounds.max}
    for key in ("identity_name", "presentation", "user_address"):
        value = _optional_string(params, key, maximum=80)
        if value:
            destination = {
                "identity_name": "name",
                "presentation": "presentation",
                "user_address": "user_address",
            }[key]
            data["identity"][destination] = value
    if "traits" in params:
        data["style"]["traits"] = list(_traits(params))
    return PersonaKernel.model_validate(data)


def _persona_ratio(params: Mapping[str, JSONValue]) -> float:
    if "chinese_ratio" in params:
        ratio = _optional_float(params, "chinese_ratio", default=0.8)
    else:
        ratio = _optional_float(params, "language_ratio", default=0.8)
    # The browser range uses 0-100 whereas the persisted schema uses 0-1.
    return ratio / 100.0 if ratio > 1.0 else ratio


def _persona_proactivity(params: Mapping[str, JSONValue]) -> str:
    raw = _optional_string(params, "initiative", maximum=32)
    if not raw:
        raw = _optional_string(params, "proactivity", maximum=32) or "剛好"
    return {"gentle": "安靜", "balanced": "剛好", "proactive": "主動"}.get(raw, raw)


def _traits(params: Mapping[str, JSONValue]) -> tuple[str, ...]:
    value = params.get("traits")
    if not isinstance(value, list) or not value:
        raise ValueError("at least one persona trait is required")
    traits = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if not traits or len(traits) > 12 or any(len(item) > 80 for item in traits):
        raise ValueError("invalid persona traits")
    return traits


def _required_string(params: Mapping[str, JSONValue], key: str, *, maximum: int) -> str:
    value = params.get(key)
    if not isinstance(value, str):
        raise UiCommandError("invalid_params")
    clean = value.strip()
    if not clean or len(clean) > maximum:
        raise UiCommandError("invalid_params")
    return clean


def _optional_string(params: Mapping[str, JSONValue], key: str, *, maximum: int) -> str:
    value = params.get(key, "")
    if not isinstance(value, str):
        raise UiCommandError("invalid_params")
    clean = value.strip()
    if len(clean) > maximum:
        raise UiCommandError("invalid_params")
    return clean


def _required_bool(params: Mapping[str, JSONValue], key: str) -> bool:
    value = params.get(key)
    if type(value) is not bool:
        raise UiCommandError("invalid_params")
    return value


def _optional_float(params: Mapping[str, JSONValue], key: str, *, default: float) -> float:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("invalid number")
    number = float(value)
    if not 0.0 <= number <= 100.0:
        raise ValueError("number outside range")
    return number


def _thread_view(thread: ConversationThread) -> dict[str, JSONValue]:
    """One thread entry, shaped identically for snapshots and `thread_updated`."""

    return {
        "id": thread.id,
        "title": _display_text(thread.title, maximum_bytes=_TITLE_TEXT_BYTES),
        "title_source": thread.title_source,
        "updated_at": thread.updated_at,
    }


def _message_view(message: StoredMessage) -> dict[str, JSONValue]:
    """One message, shaped identically for snapshots and `message_added`.

    ``memory_ids`` carries identifiers only, never memory text: the interface
    reads the wording from the memory list it already holds, so a memory the
    user forgot cannot come back through a message.
    """

    return {
        "id": message.id,
        "thread_id": message.thread_id,
        "turn_id": message.turn_id,
        "role": message.role,
        "content": _display_text(message.content),
        "created_at": message.created_at,
        "memory_ids": list(message.memory_ids),
    }


def _memory_view(memory: StoredMemory) -> dict[str, JSONValue]:
    """One memory, shaped identically for snapshots and `memory_updated`."""

    return {
        "id": memory.id,
        "content": _display_text(memory.content),
        "source": memory.source,
        "importance": memory.importance,
        "created_at": memory.created_at,
    }


def _display_text(value: str, *, maximum_bytes: int = _DISPLAY_TEXT_BYTES) -> str:
    """Keep snapshots beneath IPC bounds without altering the persisted source."""

    if maximum_bytes < 8:
        raise ValueError("display text byte bound is too small")
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    prefix = encoded[: maximum_bytes - len("…".encode())].decode("utf-8", errors="ignore")
    return prefix.rstrip() + "…"


def _response_length(bounds: SentenceBounds) -> str:
    return "short" if bounds.max <= 2 else "normal"


# Steps the user can act on, keyed by the id the onboarding screen renders.
# A required step missing from this map would leave `current_step` falling
# through to an unrelated card, so anything blocking must appear here.
_SETUP_STEP_VIEWS = {
    "repair": "repair",
    "local_runtime": "local",
    "local_models": "models",
    "persona": "persona",
}


def _step_status(reasons: set[str], relevant: set[str]) -> str:
    return "required" if reasons & relevant else "complete"
