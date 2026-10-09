"""Standalone worker process: `python -m app.worker_main`.

Run as many as you like (same machine or not) against the same database + storage; the lease protocol in
worker.py guarantees each job is processed by one worker at a time. Use with CERTGEN_WORKER_MODE=external on the API.
"""
import logging
import signal
import threading

from app.config import Settings
from app.main import build_runtime


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    engine, _, _, make_worker = build_runtime(settings)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())  # graceful: finish current certificate, release the job

    wake = threading.Event()
    threads = [threading.Thread(target=make_worker(stop).run_forever, args=(wake,)) for _ in range(settings.worker_threads)]
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():
            t.join(timeout=0.5)  # short joins keep the main thread responsive to signals on Windows
    engine.dispose()


if __name__ == "__main__":
    main()
