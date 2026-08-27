"""Run the local Qwen spike against a verified local model directory.

This never downloads anything. It spawns the isolated worker, exercises the public
fixtures, and writes a sanitized report to the private log directory. Nothing it prints
contains prompts, replies, persona or private paths.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from lune.llm_spike.decision import decide_local_provider
from lune.llm_spike.model_pin import LocalLLMManifestCheck
from lune.llm_spike.performance import MIN_STABILITY_TURNS, LatencyBudget
from lune.llm_spike.report import build_sanitized_report, write_sanitized_report
from lune.llm_spike.runner import (
    SpikeEvidence,
    grade,
    run_cancellations,
    run_stability,
    run_tool_calls,
    summarize,
    write_console_summary,
)
from lune.llm_spike.runtime import RuntimeProbe
from lune.llm_spike.worker import QwenWorkerHost, WorkerError, worker_script_path
from lune.paths import LunePaths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local Qwen Q4 spike.")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--turns", type=int, default=MIN_STABILITY_TURNS)
    parser.add_argument("--max-tokens", type=int, default=192)
    parser.add_argument("--cancel-trials", type=int, default=5)
    parser.add_argument("--stt-final-p50-ms", type=float, default=None)
    parser.add_argument("--tts-ttfa-p50-ms", type=float, default=None)
    parser.add_argument("--summary-out", type=Path, default=None)
    parser.add_argument("--skip-report", action="store_true")
    return parser.parse_args(argv)


async def _timed_start(host: QwenWorkerHost) -> float:
    started = asyncio.get_running_loop().time()
    await host.start()
    return (asyncio.get_running_loop().time() - started) * 1000.0


async def run(args: argparse.Namespace) -> int:
    evidence = SpikeEvidence()
    script = worker_script_path()

    def build_host() -> QwenWorkerHost:
        return QwenWorkerHost(
            python_executable=args.python,
            worker_script=script,
            model_dir=args.model_dir,
        )

    cold_host = build_host()
    try:
        evidence.cold_start_ms = await _timed_start(cold_host)
    except (WorkerError, TimeoutError) as error:
        print(f"worker start failed: {getattr(error, 'code', 'timeout')}", file=sys.stderr)
        return 1
    ready = cold_host.ready
    if ready is not None:
        evidence.enable_thinking_supported = ready.enable_thinking_supported
        print(
            f"worker ready: python {ready.python_version}, mlx-lm {ready.mlx_lm_version}, "
            f"load {ready.load_ms:.0f} ms, enable_thinking={ready.enable_thinking_supported}",
            file=sys.stderr,
        )
    await cold_host.close()

    host = build_host()
    try:
        evidence.warm_start_ms = await _timed_start(host)
    except (WorkerError, TimeoutError) as error:
        print(f"warm start failed: {getattr(error, 'code', 'timeout')}", file=sys.stderr)
        return 1

    try:
        await run_stability(host, evidence, turns=args.turns, max_tokens=args.max_tokens)
        await run_cancellations(
            host, evidence, trials=args.cancel_trials, max_tokens=args.max_tokens
        )
        await run_tool_calls(host, evidence, max_tokens=args.max_tokens)
    finally:
        await host.close()

    budget = LatencyBudget(
        stt_final_p50_ms=args.stt_final_p50_ms,
        tts_ttfa_p50_ms=args.tts_ttfa_p50_ms,
    )
    grades = grade(evidence, budget=budget)
    decision = decide_local_provider(
        runtime_probes=(RuntimeProbe(name="mlx_lm_worker", status="installed"),),
        manifest_check=LocalLLMManifestCheck(reason="pin_not_established"),
        thinking_gate=grades.thinking,
        tool_gate=grades.tools,
        cancellation_gate=grades.cancellation,
        performance_gate=grades.performance,
    )

    print(write_console_summary(summarize(evidence, grades), args.summary_out))

    if not args.skip_report:
        report = build_sanitized_report(
            manifest_status="pin_not_established",
            thinking_gate=grades.thinking,
            tool_gate=grades.tools,
            cancellation_gate=grades.cancellation,
            performance_gate=grades.performance,
            decision=decision,
        )
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = LunePaths.defaults().logs / f"local-llm-spike-{stamp}.json"
        write_sanitized_report(report, destination)
        print(f"sanitized report written: {destination.name}", file=sys.stderr)

    return 0 if decision.local_enabled else 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
