"""One-click Windows launcher for the packaged application."""

import ctypes
import json
import logging
import os
import socket
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

APP_TITLE = "myDATA Classifier"
MUTEX_NAME = "Local\\myDATAClassifier-8CC7589D-8531-4D61-917B-ED77EDAD83FB"
LOGGER = logging.getLogger(__name__)


def user_data_dir() -> Path:
    """Return the per-user, persistent application data directory."""
    override = os.getenv("MYDATA_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "myDATA Classifier"
    return Path.home() / "AppData" / "Local" / "myDATA Classifier"


def _open_running_instance(runtime_file: Path) -> bool:
    try:
        details = json.loads(runtime_file.read_text(encoding="utf-8"))
        url = str(details["url"])
        with urllib.request.urlopen(url, timeout=1):
            pass
    except OSError, ValueError, KeyError:
        return False
    webbrowser.open(url)
    return True


def _acquire_single_instance(runtime_file: Path):
    """Acquire the Windows mutex, reopening the existing instance if present."""
    if sys.platform != "win32":
        return object()
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise OSError("Could not create the application mutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _open_running_instance(runtime_file)
        kernel32.CloseHandle(handle)
        return None
    return handle


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    # Set this before importing app/db: both resolve their storage paths at import.
    data_dir = user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MYDATA_DATA_DIR"] = str(data_dir)
    runtime_file = data_dir / "runtime.json"

    mutex = _acquire_single_instance(runtime_file)
    if mutex is None:
        return 0

    try:
        import tkinter as tk

        from waitress.server import create_server

        from app import app

        port = _available_port()
        url = f"http://127.0.0.1:{port}/"
        server = create_server(app, host="127.0.0.1", port=port, threads=6)

        if "--smoke-test" in sys.argv:
            server_thread = threading.Thread(target=server.run, daemon=True)
            server_thread.start()
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError(f"Unexpected HTTP status: {response.status}")
            server.close()
            return 0

        runtime_file.write_text(json.dumps({"url": url}), encoding="utf-8")

        root = tk.Tk()
        root.title(APP_TITLE)
        root.resizable(False, False)
        root.geometry("420x180")

        tk.Label(root, text=APP_TITLE, font=("Segoe UI", 16, "bold")).pack(pady=(22, 6))
        tk.Label(
            root,
            text="Η εφαρμογή εκτελείται τοπικά και είναι έτοιμη για χρήση.",
            font=("Segoe UI", 10),
        ).pack(pady=(0, 16))

        buttons = tk.Frame(root)
        buttons.pack()
        tk.Button(
            buttons,
            text="Άνοιγμα εφαρμογής",
            command=lambda: webbrowser.open(url),
            width=20,
            font=("Segoe UI", 10),
        ).pack(side=tk.LEFT, padx=5)

        def stop() -> None:
            server.close()
            runtime_file.unlink(missing_ok=True)
            root.destroy()

        tk.Button(
            buttons,
            text="Τερματισμός",
            command=stop,
            width=12,
            font=("Segoe UI", 10),
        ).pack(side=tk.LEFT, padx=5)
        root.protocol("WM_DELETE_WINDOW", stop)

        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        root.after(500, lambda: webbrowser.open(url))
        root.mainloop()
        return 0
    except Exception as exc:
        LOGGER.exception("The application failed to start")
        try:
            import tkinter.messagebox

            tkinter.messagebox.showerror(
                APP_TITLE, f"Η εφαρμογή δεν μπόρεσε να ξεκινήσει.\n\n{exc}"
            )
        except Exception:
            LOGGER.exception("Could not display the startup error dialog")
        return 1
    finally:
        runtime_file.unlink(missing_ok=True)
        if sys.platform == "win32" and mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
