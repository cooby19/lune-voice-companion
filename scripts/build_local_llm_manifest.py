"""Hash a locally produced Q4 model directory and write its pinned manifest.

Run this only after the quantized artifact exists. It computes a SHA-256 for every file
that the runtime actually loads, writes a private manifest next to them, and prints the
pin literals to paste into `lune.llm_spike.model_pin`. Pinning is what turns "some files on
disk" into a verifiable artifact, so the manifest is written once and never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

RUNTIME_FILES = (
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write the pinned local model manifest.")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument(
        "--revision",
        required=True,
        help="40-character upstream commit hash the artifact was derived from.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    model_dir: Path = args.model_dir
    if not model_dir.is_dir():
        print("model directory not found", file=sys.stderr)
        return 1
    if len(args.revision) != 40 or any(c not in "0123456789abcdef" for c in args.revision):
        print("revision must be a 40-character lowercase hex commit hash", file=sys.stderr)
        return 1

    entries: list[dict[str, str]] = []
    for name in RUNTIME_FILES:
        path = model_dir / name
        if not path.is_file():
            print(f"missing runtime file: {name}", file=sys.stderr)
            return 1
        path.chmod(0o600)
        entries.append({"relative_path": name, "sha256": sha256_of(path)})

    model_dir.chmod(0o700)
    manifest = {
        "schema_version": 1,
        "model_id": args.model_id,
        "revision": args.revision,
        "files": entries,
    }
    destination = model_dir / "manifest.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        file_descriptor = os.open(destination, flags, 0o600)
    except FileExistsError:
        print("manifest already exists; not overwriting", file=sys.stderr)
        return 1
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(manifest, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
    finally:
        os.close(file_descriptor)

    print("Paste into lune/llm_spike/model_pin.py:\n")
    print("LOCAL_LLM_PIN: Final[ModelPin | None] = ModelPin(")
    print(f'    model_id="{args.model_id}",')
    print(f'    revision="{args.revision}",')
    print("    files=(")
    for entry in entries:
        print(
            f'        PinnedModelFile(relative_path="{entry["relative_path"]}", '
            f'sha256="{entry["sha256"]}"),'
        )
    print("    ),")
    print(")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
