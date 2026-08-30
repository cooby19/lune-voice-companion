"""The agent-documentation harness check, including on this repository itself."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    """Load the script by path; `scripts/` is deliberately not an import package."""

    path = REPO_ROOT / "scripts" / "check_docs.py"
    spec = importlib.util.spec_from_file_location("lune_check_docs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_docs = _load().check_docs


def _repository(root: Path, *, agents: str = "# AGENTS\n", claude: str = "# CLAUDE\n") -> Path:
    (root / "AGENTS.md").write_text(agents, encoding="utf-8")
    (root / "CLAUDE.md").write_text(claude, encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "real.py").write_text("value = 1\n", encoding="utf-8")
    return root


def _skill(root: Path, name: str, body: str) -> None:
    directory = root / ".claude" / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(body, encoding="utf-8")


def test_this_repository_passes_its_own_documentation_check() -> None:
    """The check is only worth having if the tracked docs actually satisfy it."""

    assert check_docs(REPO_ROOT) == []


def test_a_minimal_valid_repository_has_no_findings(tmp_path: Path) -> None:
    _repository(
        tmp_path,
        agents="# AGENTS\n\n`src/real.py` 的 `value`，見 [CLAUDE.md](CLAUDE.md)。\n",
        claude="# CLAUDE\n\n@AGENTS.md\n",
    )
    _skill(tmp_path, "verify", "---\nname: verify\ndescription: does a thing\n---\n\n# Verify\n")

    assert check_docs(tmp_path) == []


def test_a_missing_required_document_is_reported(tmp_path: Path) -> None:
    _repository(tmp_path)
    (tmp_path / "CLAUDE.md").unlink()

    assert any("CLAUDE.md" in finding for finding in check_docs(tmp_path))


def test_a_line_anchor_is_rejected_even_when_the_line_exists(tmp_path: Path) -> None:
    """Line numbers survive nothing; a valid one today is still the wrong citation."""

    _repository(tmp_path, agents="# AGENTS\n\n見 `src/real.py:1`。\n")

    findings = check_docs(tmp_path)
    assert len(findings) == 1
    assert "line anchor" in findings[0]


def test_a_reference_to_a_moved_file_is_reported(tmp_path: Path) -> None:
    _repository(tmp_path, agents="# AGENTS\n\n見 `src/gone.py`。\n")

    findings = check_docs(tmp_path)
    assert len(findings) == 1
    assert "missing path src/gone.py" in findings[0]


def test_references_outside_the_tracked_roots_are_left_alone(tmp_path: Path) -> None:
    """Private local state is named on purpose and is never present in the repo."""

    _repository(tmp_path, agents="# AGENTS\n\n`persona/kernel.yaml` 與 `models/whisper/x.json`。\n")

    assert check_docs(tmp_path) == []


def test_a_broken_relative_link_is_reported(tmp_path: Path) -> None:
    """Links reach files the backtick scanner never sees; external URLs are left alone."""

    _repository(tmp_path, agents="# AGENTS\n\n[授權](LICENSE)、[外部](https://example.com)\n")

    findings = check_docs(tmp_path)
    assert len(findings) == 1
    assert "link to missing path LICENSE" in findings[0]


def test_one_missing_target_written_two_ways_is_reported_once(tmp_path: Path) -> None:
    _repository(tmp_path, agents="# AGENTS\n\n`src/gone.py`，見 [檔案](src/gone.py)。\n")

    assert len(check_docs(tmp_path)) == 1


def test_a_broken_claude_import_is_reported(tmp_path: Path) -> None:
    _repository(tmp_path, claude="# CLAUDE\n\n@SHARED.md\n")

    findings = check_docs(tmp_path)
    assert len(findings) == 1
    assert "imports missing file @SHARED.md" in findings[0]


def test_skill_front_matter_must_exist_and_match_its_directory(tmp_path: Path) -> None:
    _repository(tmp_path)
    _skill(tmp_path, "no-header", "# No header\n")
    _skill(tmp_path, "wrong-name", "---\nname: other\ndescription: does a thing\n---\n")
    _skill(tmp_path, "no-description", "---\nname: no-description\ndescription:\n---\n")

    findings = check_docs(tmp_path)
    assert len(findings) == 3
    assert any("missing YAML front matter" in finding for finding in findings)
    assert any("must be 'wrong-name'" in finding for finding in findings)
    assert any("non-empty description" in finding for finding in findings)
