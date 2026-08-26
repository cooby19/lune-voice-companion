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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lune")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Show opaque local setup status")
    commands.add_parser("self-test", help="Run a no-network import/package self-test")
    key = commands.add_parser("key", help="Manage the OpenAI key in macOS Keychain")
    key_commands = key.add_subparsers(dest="key_command", required=True)
    key_commands.add_parser("set", help="Securely prompt for and store a key")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    if args.command == "self-test":
        return _self_test()
    if args.command == "key" and args.key_command == "set":
        return _set_key()
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    sys.exit(main())
