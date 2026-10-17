"""Container healthcheck.

While a bot is connected, it touches HEALTH_FILE every few seconds.
`python -m tldr_bot.health` (the Compose healthcheck) fails when the file is missing or stale,
which catches both a lost connection and a stuck event loop.
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Protocol

HEALTH_FILE = Path(os.environ.get("HEALTH_FILE", "/tmp/healthy"))
INTERVAL = 15
MAX_AGE = 60


class Connection(Protocol):
    @property
    def connected(self) -> bool: ...


async def heartbeat(connection: Connection, path: Path = HEALTH_FILE, interval: float = INTERVAL) -> None:
    while True:
        if connection.connected:
            path.touch()
        else:
            path.unlink(missing_ok=True)
        await asyncio.sleep(interval)


def is_healthy(path: Path = HEALTH_FILE, max_age: float = MAX_AGE) -> bool:
    try:
        return time.time() - path.stat().st_mtime < max_age
    except FileNotFoundError:
        return False


if __name__ == "__main__":
    sys.exit(0 if is_healthy() else 1)
