"""Deterministic checks for the repository's agent-facing Markdown harness.

Prose goes stale silently.  These checks catch the two failure modes this
repository has actually produced: documentation that points at a file which
moved or vanished, and `path:line` anchors that keep pointing at a line number
long after the code moved (a 2026-08-30 audit found 13 of 18 already wrong).
Symbol names survive edits; line numbers do not, so line anchors are rejected.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Markdown an agent is expected to read.  Generated, vendored and worktree
# trees are never scanned.
DOC_GLOBS = ("*.md", "docs/*.md", "src/**/AGENTS.md", ".claude/skills/*/SKILL.md")

REQUIRED_DOCS = ("AGENTS.md", "CLAUDE.md")

# Directories whose contents are tracked source, so a reference into them must
# resolve.  Anything else (private state, upstream repositories, model IDs) is
# deliberately out of scope.
TRACKED_ROOTS = ("src", "tests", "docs", "scripts", "examples", ".github", ".claude")

_SUFFIXES = "py|md|toml|yaml|yml|js|css|html|json|lock|sh"

_LINE_ANCHOR = re.compile(rf"`([^`\s]+\.(?:{_SUFFIXES})):(\d+)`")
_TRACKED_PATH = re.compile(
    rf"(?<![A-Za-z0-9_./-])((?:{'|'.join(re.escape(name) for name in TRACKED_ROOTS)})"
    rf"/[A-Za-z0-9_./ -]*?\.(?:{_SUFFIXES}))(?![A-Za-z0-9_-])"
)
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_CLAUDE_IMPORT = re.compile(r"^@(\S+)\s*$")

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_FIELD = re.compile(r"^([A-Za-z_-]+):\s*(.*)$", re.MULTILINE)


def _documents(root: Path) -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in DOC_GLOBS:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                seen.setdefault(path, None)
    return list(seen)


def _check_references(root: Path, document: Path, text: str) -> list[str]:
    findings: list[str] = []
    rel = document.relative_to(root)
    # One missing target is one problem, however many ways it is written.
    reported: set[str] = set()
    for match in _LINE_ANCHOR.finditer(text):
        findings.append(
            f"{rel}: line anchor `{match.group(1)}:{match.group(2)}` — "
            f"cite the symbol name instead; line numbers rot"
        )
    for match in _TRACKED_PATH.finditer(text):
        target = match.group(1)
        if not (root / target).exists() and target not in reported:
            reported.add(target)
            findings.append(f"{rel}: reference to missing path {target}")
    for match in _MARKDOWN_LINK.finditer(text):
        target = match.group(1)
        if "://" in target or target.startswith(("#", "mailto:")):
            continue
        resolved = target.split("#", 1)[0]
        if not (root / resolved).exists() and resolved not in reported:
            reported.add(resolved)
            findings.append(f"{rel}: link to missing path {target}")
    return findings


def _check_imports(root: Path, document: Path, text: str) -> list[str]:
    findings: list[str] = []
    rel = document.relative_to(root)
    for line in text.splitlines():
        match = _CLAUDE_IMPORT.match(line)
        if match and not (root / match.group(1)).exists():
            findings.append(f"{rel}: imports missing file @{match.group(1)}")
    return findings


def _check_skill(root: Path, skill: Path) -> list[str]:
    rel = skill.relative_to(root)
    header = _FRONT_MATTER.match(skill.read_text(encoding="utf-8"))
    if header is None:
        return [f"{rel}: missing YAML front matter"]
    fields = {key: value.strip() for key, value in _FIELD.findall(header.group(1))}
    findings: list[str] = []
    expected = skill.parent.name
    actual = fields.get("name")
    if actual != expected:
        findings.append(f"{rel}: front matter name must be {expected!r}, got {actual!r}")
    if not fields.get("description"):
        findings.append(f"{rel}: front matter needs a non-empty description")
    return findings


def check_docs(root: Path) -> list[str]:
    """Return every finding, newest checks last.  An empty list means green."""

    findings: list[str] = []
    for name in REQUIRED_DOCS:
        if not (root / name).is_file():
            findings.append(f"{name}: required agent document is missing")
    for document in _documents(root):
        text = document.read_text(encoding="utf-8")
        findings.extend(_check_references(root, document, text))
        if document.name == "CLAUDE.md":
            findings.extend(_check_imports(root, document, text))
    for skill in sorted(root.glob(".claude/skills/*/SKILL.md")):
        findings.extend(_check_skill(root, skill))
    return findings


def main() -> int:
    findings = check_docs(ROOT)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"Documentation harness check failed ({len(findings)} findings).", file=sys.stderr)
        return 1
    print(f"Documentation harness check passed ({len(_documents(ROOT))} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
