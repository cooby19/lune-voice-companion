"""Small deterministic secret/private-asset scanner used by public CI."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_LIMIT = 2 * 1024 * 1024
FORBIDDEN_SUFFIXES = {
    ".ckpt",
    ".pth",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
    ".wav",
    ".aiff",
    ".aif",
    ".flac",
    ".mp3",
}
FORBIDDEN_NAMES = {"kernel.yaml", "config.toml", ".env"}
PATTERNS = {
    "OpenAI-style key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def tracked_files() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable not found")
    # `git` is resolved from PATH once and argv is fixed; no scanned content is executed.
    completed = subprocess.run(  # noqa: S603
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / raw.decode() for raw in completed.stdout.split(b"\0") if raw]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        rel = path.relative_to(ROOT)
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"private asset path: {rel}")
            continue
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if len(data) > TEXT_LIMIT or b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}: {rel}")
    if findings:
        print("Secret/private data scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print(f"Secret/private data scan passed ({len(tracked_files())} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
