#!/usr/bin/env python3
"""Start Sourcerers and open it in your browser.

    python run.py                 # http://127.0.0.1:8765
    python run.py --port 9000
    python run.py --no-browser
    python run.py --host 0.0.0.0  # expose on your LAN
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _pick_port(host: str, preferred: int) -> int:
    for candidate in range(preferred, preferred + 20):
        if _port_free(host, candidate):
            return candidate
    raise SystemExit(f"no free port between {preferred} and {preferred + 19}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Sourcerers locally")
    parser.add_argument("--host", default=os.environ.get("SOURCERERS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SOURCERERS_PORT", "8765")))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    args = parser.parse_args()

    try:
        import uvicorn  # noqa: F401
        from fastapi import FastAPI  # noqa: F401
    except ImportError:
        print("Missing dependencies. Install them first:\n")
        print("    python -m pip install -r requirements.txt\n")
        raise SystemExit(1)

    if not _port_free(args.host, args.port):
        fallback = _pick_port(args.host, args.port + 1)
        print(f"  Port {args.port} is busy — using {fallback} instead.", flush=True)
        args.port = fallback

    if not args.no_browser and not os.environ.get("SOURCERERS_NO_BROWSER"):
        port = args.port
        threading.Thread(
            target=lambda: (time.sleep(1.4), webbrowser.open(f"http://127.0.0.1:{port}")),
            daemon=True,
        ).start()

    banner = r"""
   ____                        _
  / ___|  ___  _   _ _ __ ___  ___| |__   ___ _ __ ___
  \___ \ / _ \| | | | '__/ __|/ _ \ '_ \ / _ \ '__/ __|
   ___) | (_) | |_| | | | (__|  __/ | | |  __/ |  \__ \
  |____/ \___/ \__,_|_|  \___|\___|_| |_|\___|_|  |___/
    """
    print(banner)
    print(f"  Sourcerers running at  http://{args.host}:{args.port}", flush=True)
    if args.host not in ("127.0.0.1", "localhost"):
        print("  ! Reachable from your network — anyone on it can see your library.", flush=True)
    print("  Press Ctrl+C to stop.\n", flush=True)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        log_level="info",
        reload=args.reload,
    )


if __name__ == "__main__":
    sys.exit(main())
