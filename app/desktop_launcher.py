"""
EBAM G-code Studio desktop/offline launcher.
Runs the Streamlit application from a local bundled folder.
Used both from source and from PyInstaller builds.
"""
from __future__ import annotations

import os
import sys
import socket
import threading
import time
import webbrowser
from pathlib import Path


def resource_path(relative: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return Path(__file__).resolve().parent / relative


def find_free_port(start: int = 8501, stop: int = 8599) -> int:
    for port in range(start, stop + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def open_browser_later(url: str, delay: float = 2.0) -> None:
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def main() -> None:
    app_path = resource_path("app.py")
    if not app_path.exists():
        print("ERROR: app.py not found:", app_path)
        input("Press Enter to close...")
        raise SystemExit(2)

    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    print("================================================")
    print("EBAM G-code Studio - local offline launcher")
    print("================================================")
    print("No internet is required for running the app.")
    print("Local address:", url)
    print("If browser does not open automatically, copy this address into browser.")
    print("Close this window to stop the application.")
    print("================================================")

    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

    open_browser_later(url)

    # Launch Streamlit inside current Python process. This works from source and is also the
    # most reliable entrypoint for PyInstaller builds.
    try:
        from streamlit.web import cli as stcli
    except Exception as exc:
        print("ERROR: Streamlit is not installed or not bundled.")
        print(exc)
        input("Press Enter to close...")
        raise SystemExit(3)

    sys.argv = [
        "streamlit", "run", str(app_path),
        "--server.address=127.0.0.1",
        f"--server.port={port}",
        "--server.headless=true",
        "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false",
        "--global.developmentMode=false",
    ]
    stcli.main()


if __name__ == "__main__":
    main()
