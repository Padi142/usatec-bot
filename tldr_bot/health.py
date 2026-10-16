"""Container healthcheck.

While the bot is connected to the Signal API, it touches HEALTH_FILE every few seconds.
`python -m tldr_bot.health` (the healthcheck in docker-compose.yml) fails when the file is missing or stale,
which catches both a lost Signal connection and a stuck event loop.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from .signal_api import SignalClient

HEALTH_FILE = Path(os.environ.get("HEALTH_FILE", "/tmp/healthy"))
INTERVAL = 15
MAX_AGE = 60


async def heartbeat(signal: SignalClient, path: Path = HEALTH_FILE, interval: float = INTERVAL) -> None:
    while True:
        if signal.connected:
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
