"""Engine entry point. Runtime services are composed in later modules."""

from __future__ import annotations

import asyncio

from lune.paths import LunePaths
from lune.readiness import check_readiness


async def run() -> int:
    readiness = check_readiness(LunePaths.defaults())
    if readiness.state == "setup_required":
        return 2
    # The full IPC server owns the long-running lifecycle after composition.
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
