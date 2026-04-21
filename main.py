"""Sentry Rewind - native app launcher. Runs Flask in a thread, pywebview for the window."""

import platform
import socket
import subprocess
import sys
import threading

import webview

from app import app, find_teslacam, get_events


def _is_dark_mode() -> bool:
    """Check if the OS is in dark mode."""
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True,
            )
            return result.stdout.strip().lower() == "dark"
        except Exception:
            pass
    return True  # default to dark


def _initial_bg() -> str:
    return "#111111" if _is_dark_mode() else "#f0f0f0"


def find_free_port() -> int:
    """Get an unused port from the OS."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int):
    """Run Flask in a threaded server so 6 cameras can fetch segments in parallel."""
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", port, app, threaded=True)
    srv.serve_forever()


if __name__ == "__main__":
    path = find_teslacam()
    print(f"Scanning {path}..." if path else "No TeslaCam drive detected yet.")
    get_events()

    port = find_free_port()

    server = threading.Thread(target=start_server, args=(port,), daemon=True)
    server.start()

    webview.create_window(
        "Sentry Rewind",
        f"http://127.0.0.1:{port}",
        width=1350,
        height=800,
        min_size=(800, 500),
        background_color=_initial_bg(),
    )
    webview.start()
