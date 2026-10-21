"""Run the hosted demo: `LEDGERSPY_MODE=demo python -m ledgerspy_demo` (see docs/demo/public-demo.md).

Refuses to start unless LEDGERSPY_MODE=demo and the required settings are present. The offline application
(`ledgerspy serve`) is unaffected and remains loopback-only.
"""

import contextlib
import os
import signal
import sys
import threading

import uvicorn

from ledgerspy_demo.app import build_demo_app, release_process
from ledgerspy_demo.limits import DemoConfigError
from ledgerspy_demo.settings import DemoSettings

_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM) + ((signal.SIGBREAK,) if sys.platform == "win32" else ())


class _DemoServer(uvicorn.Server):
    """uvicorn's server, except that after a graceful shutdown on SIGINT / SIGTERM (SIGBREAK on Windows) it
    returns instead of re-raising the signal, so `main` releases the data folder's lock and exits with 0."""

    @contextlib.contextmanager
    def capture_signals(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {sig: signal.signal(sig, self.handle_exit) for sig in _STOP_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)


def main() -> int:
    try:
        settings = DemoSettings.from_env(os.environ)
        app = build_demo_app(settings)  # also configures the process: folder checks, lock, temp, PDF limit
    except DemoConfigError as exc:
        release_process()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except BaseException:
        release_process()  # a failed start-up never keeps the data folder locked
        raise
    print(
        f"LedgerSpy public demo on {settings.public_origin} "
        f"(listening on {settings.bind_host}:{settings.port})"
    )
    try:
        _DemoServer(
            uvicorn.Config(
                app,
                host=settings.bind_host,
                port=settings.port,
                proxy_headers=False,  # forwarded headers are interpreted only by the front door, per settings
                server_header=False,
                access_log=False,
                ws="none",
                lifespan="on",
                log_level="warning",
                # open connections get this long after a stop signal; the front door's lifespan then waits for
                # work that outlived its request (same deadline) and stops PDF workers
                timeout_graceful_shutdown=settings.limits.shutdown_grace_seconds,
            )
        ).run()
    finally:
        release_process()
    return 0


if __name__ == "__main__":
    sys.exit(main())
