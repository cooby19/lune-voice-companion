from __future__ import annotations

from pathlib import Path

from lune.paths import LunePaths


def test_canonical_paths() -> None:
    paths = LunePaths.defaults(Path("/Users/test"))
    assert paths.persona == Path("/Users/test/Library/Application Support/Lune/persona/kernel.yaml")
    assert paths.database.name == "lune.sqlite3"
    assert paths.logs == Path("/Users/test/Library/Logs/Lune")
