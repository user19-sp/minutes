"""Queue worker entrypoint.

    python -m backend.app.worker

Runs as its own container in docker-compose. Keeping it a separate process from
the API is the point: a long transcription must not occupy a request handler, and
the worker can be scaled or restarted without touching the API.
"""

from __future__ import annotations

import signal
import sys
import threading

from backend.app.db import init_db
from backend.app.observability.logging import configure_logging, get_logger
from backend.app.services.queue import run_worker, worker_identity


def main() -> int:
    configure_logging()
    log = get_logger("worker")

    # Register the tool allow-list before any run is claimed, so the registry is
    # never half-built when work arrives.
    from backend.app.agent import tools  # noqa: F401

    init_db()

    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("worker_shutdown_requested", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("worker_boot", worker=worker_identity())
    run_worker(stop=stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
