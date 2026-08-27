"""Administrative CLI that avoids secrets in argv and bulk destructive actions."""

from __future__ import annotations

import argparse
import getpass
import json
import platform
import sys
from collections.abc import Sequence

from lune import __version__
from lune.keychain import set_openai_api_key
from lune.paths import LunePaths
from lune.readiness import check_readiness


def _doctor() -> int:
    paths = LunePaths.defaults()
    readiness = check_readiness(paths)
    report = {
        "version": __version__,
        "platform": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "state": readiness.state,
        "reasons": list(readiness.reasons),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if readiness.state == "mic_off" else 2


def _set_key() -> int:
    value = getpass.getpass("OpenAI API key (stored only in macOS Keychain): ")
    set_openai_api_key(value)
    print("Key stored in macOS Keychain.")
    return 0


def _self_test() -> int:
    paths = LunePaths.defaults()
    assert paths.support.name == "Lune"
    assert paths.logs.name == "Lune"
    print("Lune import/self-test passed.")
    return 0


def _memory_list(*, export: bool = False) -> int:
    from lune.memory.store import MemoryStore

    with MemoryStore(LunePaths.defaults().database) as store:
        payload = [
            {
                "id": memory.id,
                "category": memory.category,
                "importance": memory.importance,
                "content": memory.content,
                "created_at": memory.created_at,
                **({"source_turn_id": memory.source_turn_id} if export else {}),
            }
            for memory in store.list_memories()
        ]
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _memory_search() -> int:
    from lune.memory.embedding import E5MemoryRetriever, E5SetupRequired, LocalE5Encoder
    from lune.memory.store import MemoryStore

    query = getpass.getpass("Memory search query (local only): ")
    paths = LunePaths.defaults()
    try:
        with MemoryStore(paths.database) as store:
            retriever = E5MemoryRetriever(store, LocalE5Encoder(paths.e5_manifest))
            payload = [
                {
                    "id": result.id,
                    "score": result.score,
                    "category": result.category,
                    "content": result.content,
                }
                for result in retriever.search(query)
            ]
    except E5SetupRequired as error:
        print(json.dumps({"state": "setup_required", "reason": error.reason}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _memory_forget(exact_id: str) -> int:
    from lune.memory.store import MemoryStore

    confirmation = input(f"Type the exact memory ID '{exact_id}' to confirm deletion: ")
    if confirmation != exact_id:
        print("Memory deletion cancelled.")
        return 2
    with MemoryStore(LunePaths.defaults().database) as store:
        forgotten = store.forget_memory(exact_id)
    if not forgotten:
        print("No memory matched that exact ID.", file=sys.stderr)
        return 2
    print(f"Forgot memory {exact_id}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lune")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Show opaque local setup status")
    commands.add_parser("self-test", help="Run a no-network import/package self-test")
    key = commands.add_parser("key", help="Manage the OpenAI key in macOS Keychain")
    key_commands = key.add_subparsers(dest="key_command", required=True)
    key_commands.add_parser("set", help="Securely prompt for and store a key")
    memory = commands.add_parser("memory", help="Inspect or forget individual local memories")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_commands.add_parser("list", help="List local memories")
    memory_commands.add_parser("search", help="Prompt for a local semantic search query")
    memory_commands.add_parser("export", help="Export local memories as JSON")
    forget = memory_commands.add_parser("forget", help="Hard-delete one exact memory ID")
    forget.add_argument("exact_id", help="Exact memory ID (bulk deletion is not supported)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    if args.command == "self-test":
        return _self_test()
    if args.command == "key" and args.key_command == "set":
        return _set_key()
    if args.command == "memory" and args.memory_command == "list":
        return _memory_list()
    if args.command == "memory" and args.memory_command == "search":
        return _memory_search()
    if args.command == "memory" and args.memory_command == "export":
        return _memory_list(export=True)
    if args.command == "memory" and args.memory_command == "forget":
        return _memory_forget(args.exact_id)
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    sys.exit(main())
