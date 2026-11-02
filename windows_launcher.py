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
from ctypes import wintypes as wt
from pathlib import Path

APP_TITLE = "myDATA Classifier"
MUTEX_NAME = "Local\\myDATAClassifier-8CC7589D-8531-4D61-917B-ED77EDAD83FB"
LOGGER = logging.getLogger(__name__)
TRAY_CLASS = "myDATAClassifier-tray"
MB_ICONERROR = 0x10

# Win32 σταθερές
WM_DESTROY, WM_COMMAND, WM_NULL = 0x0002, 0x0111, 0x0000
WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0203, 0x0205
WM_TRAY = 0x8000 + 1  # WM_APP + 1
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
IMAGE_ICON, LR_LOADFROMFILE, SM_CXSMICON = 1, 0x10, 49
CMD_OPEN, CMD_EXIT = 1, 2

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON),
    ]


def _win32():
    user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
    ]
    user32.LoadImageW.restype = wt.HANDLE
    user32.LoadImageW.argtypes = [
        wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT,
    ]
    user32.CreatePopupMenu.restype = wt.HMENU
    user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [
        wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID,
    ]
    user32.DestroyMenu.argtypes = [wt.HMENU]
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DestroyWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    return user32, shell32


def open_window(url: str) -> None:
    """Δικό της παράθυρο χωρίς γραμμή διευθύνσεων (Chromium --app)· ο Edge
    υπάρχει σε κάθε Windows 10/11. Το startfile (ShellExecute) βρίσκει το exe
    από τα «App Paths» του registry — δεν είναι στο PATH."""
    for exe in ("msedge", "chrome"):
        try:
            os.startfile(exe, arguments=f"--app={url}")
            return
        except OSError:
            continue
    webbrowser.open(url)


def run_tray(url: str, icon_path: Path) -> None:
    """Εικονίδιο στο tray — μπλοκάρει μέχρι το «Exit»."""
    user32, shell32 = _win32()
    hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
    nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), uID=1,
                          uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY)

    def show_menu(hwnd):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, CMD_OPEN, "Άνοιγμα εφαρμογής")
        user32.AppendMenuW(menu, 0, CMD_EXIT, "Τερματισμός")
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)  # αλλιώς το μενού δεν κλείνει με κλικ έξω
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            user32.PostMessageW(hwnd, WM_COMMAND, cmd, 0)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam == WM_RBUTTONUP:
                show_menu(hwnd)
            elif lparam == WM_LBUTTONDBLCLK:
                open_window(url)
            return 0
        if msg == WM_COMMAND:
            if wparam == CMD_OPEN:
                open_window(url)
            elif wparam == CMD_EXIT:
                user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wndproc)  # κρατάμε αναφορά: αλλιώς ο GC σβήνει το callback
    wc = WNDCLASSW(lpfnWndProc=proc, hInstance=hinst, lpszClassName=TRAY_CLASS)
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, TRAY_CLASS, APP_TITLE, 0, 0, 0, 0, 0,
                                  None, None, hinst, None)
    size = user32.GetSystemMetrics(SM_CXSMICON)
    nid.hWnd = hwnd
    nid.hIcon = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, size, size,
                                  LR_LOADFROMFILE)
    nid.szTip = f"{APP_TITLE} — {url}"
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        raise OSError("Δεν μπήκε το εικονίδιο στο system tray")

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


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
    open_window(url)
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


def _check_mcp(timeout: float = 30) -> None:
    """Smoke test: ο MCP server απαντά 401 χωρίς token."""
    import time
    import urllib.error

    deadline = time.monotonic() + timeout
    while True:
        try:
            urllib.request.urlopen("http://127.0.0.1:8342/mcp", timeout=2)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return
            raise
        except OSError:
            if time.monotonic() > deadline:
                raise RuntimeError("MCP server did not start") from None
            time.sleep(0.5)
        else:
            raise RuntimeError("MCP server answered without a token")


def main() -> int:
    # Set this before importing app/db: both resolve their storage paths at import.
    data_dir = user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MYDATA_DATA_DIR"] = str(data_dir)
    runtime_file = data_dir / "runtime.json"

    if "--mcp" in sys.argv:  # διεργασία-παιδί: ο MCP server (mcp_server.spawn), πριν από το mutex
        if sys.stdout is None:  # windowed exe: χωρίς κονσόλα
            sys.stdout = sys.stderr = open(data_dir / "mcp.log", "a", encoding="utf-8")
        import mcp_server

        mcp_server.main(http=True)
        return 0

    mutex = _acquire_single_instance(runtime_file)
    if mutex is None:
        return 0

    mcp = None
    try:
        from waitress.server import create_server

        from app import app

        port = _available_port()
        url = f"http://127.0.0.1:{port}/"
        server = create_server(app, host="127.0.0.1", port=port, threads=6)

        import mcp_server

        mcp = mcp_server.spawn()  # ponytail: ορφανό αν σκοτωθεί ο launcher, Job Object αν ενοχλήσει

        if "--smoke-test" in sys.argv:
            server_thread = threading.Thread(target=server.run, daemon=True)
            server_thread.start()
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError(f"Unexpected HTTP status: {response.status}")
            server.close()
            _check_mcp()
            return 0

        runtime_file.write_text(json.dumps({"url": url}), encoding="utf-8")

        threading.Thread(target=server.run, daemon=True).start()
        open_window(url)
        run_tray(url, Path(app.static_folder) / "app.ico")
        server.close()
        return 0
    except Exception as exc:
        LOGGER.exception("The application failed to start")
        ctypes.windll.user32.MessageBoxW(
            None, f"Η εφαρμογή δεν μπόρεσε να ξεκινήσει.\n\n{exc}", APP_TITLE, MB_ICONERROR
        )
        return 1
    finally:
        if mcp:
            mcp.terminate()
        runtime_file.unlink(missing_ok=True)
        if sys.platform == "win32" and mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
