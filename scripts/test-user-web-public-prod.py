#!/usr/bin/env python3
"""Run User Web Playwright tests against the published Nginx bundle.

Chromium cannot reach the host's non-loopback listener in some sandboxed test
environments. This process exposes a temporary loopback TCP forward to the
formal Nginx listener; it does not serve a local build or mutate public-prod.
"""

from __future__ import annotations

import os
from pathlib import Path
import select
import socket
import socketserver
import subprocess
import sys
import threading
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
USER_WEB_ROOT = REPOSITORY_ROOT / "frontend/user-web"


class _ForwardingHandler(socketserver.BaseRequestHandler):
    target: tuple[str, int]

    def handle(self) -> None:
        upstream = socket.create_connection(self.target, timeout=10)
        peers = [self.request, upstream]
        try:
            while True:
                readable, _, _ = select.select(peers, [], [], 30)
                for current in readable:
                    data = current.recv(65_536)
                    if not data:
                        return
                    destination = upstream if current is self.request else self.request
                    destination.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            upstream.close()


class _ForwardingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    public_web_base = os.environ.get("PUBLIC_WEB_BASE", "")
    parsed = urlsplit(public_web_base)
    target_host = os.environ.get("PUBLIC_PROD_TARGET_HOST", parsed.hostname or "127.0.0.1")
    target_port = int(os.environ.get("PUBLIC_PROD_TARGET_PORT", str(parsed.port or (443 if parsed.scheme == "https" else 80))))
    local_port = int(os.environ.get("PUBLIC_PROD_TEST_PORT", "15178"))
    _ForwardingHandler.target = (target_host, target_port)

    with _ForwardingServer(("127.0.0.1", local_port), _ForwardingHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = os.environ.copy()
        environment["PLAYWRIGHT_PUBLIC_PROD_URL"] = f"http://127.0.0.1:{local_port}"
        command = [
            str(USER_WEB_ROOT / "node_modules/.bin/playwright"),
            "test",
            "--config=playwright.public-prod.config.ts",
            *sys.argv[1:],
        ]
        completed = subprocess.run(
            command,
            cwd=USER_WEB_ROOT,
            env=environment,
            check=False,
        )
        server.shutdown()
        thread.join(timeout=5)
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
