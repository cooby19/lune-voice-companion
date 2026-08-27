"""Fail-closed GPT-SoVITS worker launcher and streaming backend."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from lune.tts.circuit import TTSCircuitBreaker
from lune.tts.contracts import PCMChunk, TTSBackendError, TTSRequest
from lune.tts.protocol import (
    ControlFrame,
    PCMFrame,
    WorkerFrame,
    WorkerProtocolError,
    encode_control,
    read_frame,
)
from lune.tts_spike.manifest import check_private_manifest
from lune.tts_spike.sandbox import SandboxCheck, probe_sandbox

GPT_SOVITS_COMMIT: Final[str] = "48b1a0169a28582a8984402f82cf438d3bfa6aca"


class WorkerConnection(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def alive(self) -> bool: ...

    async def send(self, frame: ControlFrame) -> None: ...

    async def receive(self) -> WorkerFrame: ...

    async def terminate(self) -> None: ...

    async def close(self) -> None: ...


class WorkerLauncher(Protocol):
    async def launch(self) -> WorkerConnection: ...


def _sbpl_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_worker_profile(
    *,
    runtime_root: Path,
    voice_root: Path,
    temp_root: Path,
    python_root: Path,
    worker_root: Path,
) -> str:
    """Build a deny-by-default profile with no private values in process arguments."""

    read_roots = (
        Path("/System"),
        Path("/usr/lib"),
        Path("/Library/Frameworks"),
        Path("/private/var/db"),
        Path("/dev"),
        runtime_root,
        voice_root,
        temp_root,
        python_root,
        worker_root,
    )
    read_rules = " ".join(f"(subpath {_sbpl_literal(str(path))})" for path in read_roots)
    return "\n".join(
        (
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow ipc-posix*)",
            "(allow file-read-metadata)",
            f"(allow file-read* {read_rules})",
            f"(allow file-write* (subpath {_sbpl_literal(str(temp_root))}))",
            '(allow file-write-data (literal "/dev/null"))',
            "(deny network*)",
        )
    )


def _validate_runtime_root(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise TTSBackendError("setup_required") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TTSBackendError("setup_required")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise TTSBackendError("setup_required")


@dataclass(slots=True)
class _SubprocessConnection:
    process: asyncio.subprocess.Process
    temp_root: Path = field(repr=False)
    profile_path: Path = field(repr=False)
    stderr_task: asyncio.Task[None] = field(repr=False)
    _closed: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    async def send(self, frame: ControlFrame) -> None:
        writer = self.process.stdin
        if writer is None or not self.alive:
            raise WorkerProtocolError("worker_eof")
        writer.write(encode_control(frame))
        await writer.drain()

    async def receive(self) -> WorkerFrame:
        reader = self.process.stdout
        if reader is None:
            raise WorkerProtocolError("worker_eof")
        return await read_frame(reader)

    async def terminate(self) -> None:
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.0)
            except TimeoutError:
                if self.process.returncode is None:
                    self.process.kill()
                    await self.process.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.alive:
                try:
                    await self.send(ControlFrame(type="close"))
                    await asyncio.wait_for(self.process.wait(), timeout=2.0)
                except (TimeoutError, WorkerProtocolError, OSError):
                    await self.terminate()
        finally:
            self.stderr_task.cancel()
            try:
                await self.stderr_task
            except asyncio.CancelledError:
                pass
            self._cleanup_known_paths()

    def _cleanup_known_paths(self) -> None:
        try:
            self.profile_path.unlink(missing_ok=True)
            self.temp_root.rmdir()
        except OSError:
            # Unknown runtime-created files are deliberately not bulk-deleted.
            pass


async def _discard_stderr(reader: asyncio.StreamReader | None) -> None:
    if reader is None:
        return
    while await reader.read(8192):
        pass


@dataclass(frozen=True, slots=True)
class SandboxedGPTWorkerLauncher:
    python_executable: Path
    runtime_root: Path = field(repr=False)
    manifest_path: Path = field(repr=False)
    sandbox_executable: Path = Path("/usr/bin/sandbox-exec")
    worker_script: Path = field(
        default_factory=lambda: Path(__file__).with_name("gpt_worker.py"), repr=False
    )
    startup_timeout_seconds: float = 120.0

    async def launch(self) -> WorkerConnection:
        _validate_runtime_root(self.runtime_root)
        python_executable = self.python_executable.resolve(strict=False)
        if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
            raise TTSBackendError("setup_required")
        voice_root = self.manifest_path.parent
        manifest_check = await asyncio.to_thread(
            check_private_manifest,
            manifest_path=self.manifest_path,
            voice_root=voice_root,
            runtime_revision_path=self.runtime_root / ".lune-revision",
            expected_upstream_commit=GPT_SOVITS_COMMIT,
        )
        if not manifest_check.ready:
            raise TTSBackendError("setup_required")
        sandbox_check = await asyncio.to_thread(
            probe_sandbox,
            canary_path=self.manifest_path,
            sandbox_executable=self.sandbox_executable,
            python_executable=python_executable,
        )
        self._require_sandbox(sandbox_check)

        temp_root = Path(tempfile.mkdtemp(prefix="lune-gpt-worker-"))
        temp_root.chmod(0o700)
        profile_path = temp_root / "worker.sb"
        profile = build_worker_profile(
            runtime_root=self.runtime_root,
            voice_root=voice_root,
            temp_root=temp_root,
            python_root=python_executable.parent.parent,
            worker_root=self.worker_script.parent,
        )
        profile_path.write_text(profile, encoding="utf-8")
        profile_path.chmod(0o600)
        environment = self._environment(temp_root, voice_root)
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.sandbox_executable),
                "-f",
                str(profile_path),
                str(python_executable),
                "-I",
                str(self.worker_script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.runtime_root,
                env=dict(environment),
            )
        except OSError as error:
            profile_path.unlink(missing_ok=True)
            try:
                temp_root.rmdir()
            except OSError:
                pass
            raise TTSBackendError("backend_unavailable") from error
        stderr_task = asyncio.create_task(_discard_stderr(process.stderr), name="gpt-stderr-drain")
        connection = _SubprocessConnection(process, temp_root, profile_path, stderr_task)
        try:
            frame = await asyncio.wait_for(
                connection.receive(), timeout=self.startup_timeout_seconds
            )
        except (TimeoutError, WorkerProtocolError):
            await connection.terminate()
            await connection.close()
            raise TTSBackendError("backend_unavailable") from None
        if (
            not isinstance(frame, ControlFrame)
            or frame.type != "ready"
            or frame.python_version is None
            or not frame.python_version.startswith("3.10.")
        ):
            await connection.terminate()
            await connection.close()
            raise TTSBackendError("backend_unavailable")
        return connection

    @staticmethod
    def _require_sandbox(check: SandboxCheck) -> None:
        if not check.available:
            raise TTSBackendError("backend_unavailable")

    def _environment(self, temp_root: Path, voice_root: Path) -> Mapping[str, str]:
        return {
            "HOME": str(temp_root),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(temp_root),
            "XDG_CACHE_HOME": str(temp_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "LUNE_GPT_RUNTIME_ROOT": str(self.runtime_root),
            "LUNE_GPT_VOICE_ROOT": str(voice_root),
            "LUNE_GPT_MANIFEST": str(self.manifest_path),
        }


class GPTSoVITSBackend:
    """Long-lived, disposable worker with bounded cancellation and session breaker."""

    def __init__(
        self,
        launcher: WorkerLauncher,
        *,
        circuit_breaker: TTSCircuitBreaker | None = None,
        cancel_timeout_seconds: float = 0.5,
    ) -> None:
        if cancel_timeout_seconds <= 0:
            raise ValueError("cancel timeout must be positive")
        self._launcher = launcher
        self._circuit = circuit_breaker or TTSCircuitBreaker()
        self._cancel_timeout = cancel_timeout_seconds
        self._connection: WorkerConnection | None = None
        self._restart_required = False
        self._closed = False
        self._active_generation: int | None = None
        self._active_done: asyncio.Event | None = None
        self._cancelled: set[int] = set()
        self._lock = asyncio.Lock()

    @property
    def circuit_breaker(self) -> TTSCircuitBreaker:
        return self._circuit

    @property
    def worker_pid(self) -> int | None:
        connection = self._connection
        return connection.pid if connection is not None and connection.alive else None

    async def _ensure_worker(self) -> WorkerConnection:
        if self._closed or not self._circuit.allows_request:
            raise TTSBackendError("backend_unavailable")
        if self._connection is not None and self._connection.alive:
            return self._connection
        rebuilding = self._restart_required
        try:
            connection = await self._launcher.launch()
        except TTSBackendError:
            if rebuilding:
                self._circuit.record_rebuild_failure()
            else:
                self._circuit.record_failure()
            raise
        self._connection = connection
        self._restart_required = False
        return connection

    async def synthesize(self, request: TTSRequest) -> AsyncIterator[PCMChunk]:
        async with self._lock:
            self._cancelled.discard(request.generation_id)
            connection = await self._ensure_worker()
            self._active_generation = request.generation_id
            done = asyncio.Event()
            self._active_done = done
            expected_sequence = 0
            emitted = False
            try:
                await connection.send(
                    ControlFrame(
                        type="synthesize",
                        request_id=request.request_id,
                        generation_id=request.generation_id,
                        text=request.text,
                        language_hint=request.language_hint or "auto",
                    )
                )
                while True:
                    frame = await connection.receive()
                    if isinstance(frame, PCMFrame):
                        if (
                            frame.chunk.generation_id != request.generation_id
                            or frame.sequence != expected_sequence
                        ):
                            raise WorkerProtocolError("pcm_sequence_mismatch")
                        expected_sequence += 1
                        if request.generation_id not in self._cancelled:
                            emitted = True
                            yield frame.chunk
                        continue
                    if not self._matches(frame, request):
                        raise WorkerProtocolError("control_sequence_mismatch")
                    if frame.type == "error":
                        raise TTSBackendError("synthesis_failed")
                    if frame.type != "done" or frame.sequence != expected_sequence:
                        raise WorkerProtocolError("unexpected_control_frame")
                    if frame.code == "cancelled" or request.generation_id in self._cancelled:
                        raise TTSBackendError("cancelled")
                    if frame.code != "complete" or not emitted:
                        raise TTSBackendError("synthesis_failed")
                    self._circuit.record_success()
                    return
            except TTSBackendError as error:
                if error.code != "cancelled":
                    await self._worker_failed(connection)
                raise
            except (WorkerProtocolError, OSError) as error:
                if request.generation_id in self._cancelled:
                    raise TTSBackendError("cancelled") from None
                await self._worker_failed(connection)
                if isinstance(error, WorkerProtocolError):
                    raise TTSBackendError("protocol_error") from error
                raise TTSBackendError("synthesis_failed") from error
            finally:
                done.set()
                if self._active_done is done:
                    self._active_done = None
                    self._active_generation = None

    @staticmethod
    def _matches(frame: ControlFrame, request: TTSRequest) -> bool:
        if frame.type not in {"done", "error"}:
            return False
        return (
            frame.request_id == request.request_id and frame.generation_id == request.generation_id
        )

    async def _worker_failed(self, connection: WorkerConnection) -> None:
        self._circuit.record_failure()
        if self._connection is connection:
            self._connection = None
            self._restart_required = True
        await connection.terminate()
        await connection.close()

    async def cancel(self, generation_id: int) -> None:
        self._cancelled.add(generation_id)
        if self._active_generation != generation_id:
            return
        connection = self._connection
        done = self._active_done
        if connection is None or done is None:
            return
        try:
            await connection.send(ControlFrame(type="cancel", generation_id=generation_id))
            await asyncio.wait_for(done.wait(), timeout=self._cancel_timeout)
        except (TimeoutError, WorkerProtocolError, OSError):
            if self._connection is connection:
                self._connection = None
                self._restart_required = True
            await connection.terminate()
            await connection.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._active_generation is not None:
            await self.cancel(self._active_generation)
        connection = self._connection
        self._connection = None
        if connection is not None:
            await connection.close()
