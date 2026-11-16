"""Dorie Play in its own window: the same local server, shown by pywebview instead of a browser tab.

    python window.py [--demo] [--port N] [--no-window] [--self-test]

Sign-in pages always open in the system browser (platforms.signin.open_url); the window only shows our page.
Only this module imports webview, and only inside functions, so the server and the tests never need it.
"""
import argparse
import ctypes
import html
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import paths
import server
import version

TITLE = 'Dorie Play'
PORTS = range(8765, 8775)   # never 8888 or 8890: sign-in uses those
LOG = paths.data_folder() / 'startup.log'
STORAGE = paths.data_folder() / 'webview'
CLOSE_TEXT = ('A search or transfer is still running. Close Dorie Play anyway? '
              'What is done so far is saved, and the same task continues next time.')
WEBVIEW2_TEXT = ('Dorie Play needs the Microsoft Edge WebView2 Runtime, which is missing on this computer. '
                 'Install it from https://developer.microsoft.com/microsoft-edge/webview2/ and open Dorie Play again.')
START_ERROR = 'Dorie Play could not start: {error}.'
START_ERROR_LOG = ' Details are in {log}.'
MUTEX = 'DoriePlay.App'
WEBVIEW2_KEYS = [
    ('HKLM', r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
    ('HKLM', r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
    ('HKCU', r'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
]
_mutex = None   # held for the life of the process; Windows frees it when the process ends


def quiet_streams():
    """A windowed Windows app has no console: printing to None would crash."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')


def start_server(app, first=None):
    """(server, port): `first`, then PORTS, then any free port; serving in a background thread."""
    for port in ([first] if first else []) + [*PORTS, 0]:
        try:
            http, port = server.serve(app, port)
        except OSError:
            continue
        threading.Thread(target=http.serve_forever, daemon=True).start()
        return http, port
    raise OSError('no free port')   # unreachable: port 0 always binds unless sockets are broken


def _read_registry(hive, path):
    import winreg
    try:
        with winreg.OpenKey({'HKLM': winreg.HKEY_LOCAL_MACHINE, 'HKCU': winreg.HKEY_CURRENT_USER}[hive], path) as key:
            return winreg.QueryValueEx(key, 'pv')[0]
    except OSError:
        return None


def has_webview2(read=None):
    read = read or _read_registry
    return any(read(hive, path) not in (None, '', '0.0.0.0') for hive, path in WEBVIEW2_KEYS)


def already_running():
    """Windows only: a second copy finds the first one's mutex. macOS itself activates a running .app."""
    global _mutex
    if sys.platform != 'win32':
        return False
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)   # GetLastError() via ctypes can be stale
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _mutex = kernel32.CreateMutexW(None, False, MUTEX)
    return ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS


def bring_to_front():
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32')
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    handle = user32.FindWindowW(None, TITLE)
    if handle:
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, 9)   # SW_RESTORE
        user32.SetForegroundWindow(handle)


def message_box(text):
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)   # MB_ICONERROR


def log_error(text):
    """Append to the start-up log, or to the temp folder when the data folder is unusable. Never raises.
    Only start-up and unexpected errors go here: no song names, keys or links."""
    entry = f'{time.strftime("%Y-%m-%d %H:%M:%S")} {text}\n'
    for target in (LOG, Path(tempfile.gettempdir()) / 'dorieplay-startup.log'):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, 'a', encoding='utf-8') as log:
                log.write(entry)
            return target
        except OSError:
            continue
    return None


def on_closing(window, app):
    # pywebview reads confirm_close right after this handler and asks itself; a dialog shown from here hangs on macOS
    window.confirm_close = app.busy()


def self_test():
    try:
        import webview
        if sys.platform == 'win32':
            import clr  # noqa: F401
            from webview.platforms import winforms  # noqa: F401
        elif sys.platform == 'darwin':
            from webview.platforms import cocoa  # noqa: F401
    except Exception as error:
        print(f'failed: {error}', flush=True)
        return 1
    try:
        from importlib.metadata import version as installed
        library = installed('pywebview')
    except Exception:   # a frozen build may carry no package metadata
        library = '?'
    print(f'ok {version.VERSION} pywebview {library}', flush=True)
    return 0


def main(argv=None):
    quiet_streams()
    parser = argparse.ArgumentParser(prog='window.py', description=__doc__.split('\n')[0])
    parser.add_argument('--demo', action='store_true', help='Invented playlist and fake services, no keys needed')
    parser.add_argument('--port', type=int, help='Start with this port (not 8888 or 8890)')
    parser.add_argument('--no-window', action='store_true', help='Only the server: print its address and keep running')
    parser.add_argument('--self-test', action='store_true', help='Check that the window libraries load')
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        paths.ensure(paths.data_folder())
        if sys.platform == 'win32' and not args.no_window and already_running():
            bring_to_front()
            return 0
        app = server.make_app(args.demo)
        http, port = start_server(app, args.port)
        url = f'http://127.0.0.1:{port}/'
        if args.no_window:
            print(url, flush=True)
            threading.Event().wait()
        if sys.platform == 'win32' and not has_webview2():
            message_box(WEBVIEW2_TEXT)
            return 1
        import webview
        window = webview.create_window(TITLE, url, width=1180, height=860, min_size=(400, 600),
                                       localization={'global.quitConfirmation': CLOSE_TEXT})
        window.events.closing += lambda: on_closing(window, app)
        threading.Thread(target=version.check, args=(app,), daemon=True).start()
        webview.start(gui='edgechromium' if sys.platform == 'win32' else None, private_mode=False,
                      storage_path=str(STORAGE))
        return 0
    except Exception as error:
        where = log_error(traceback.format_exc())
        text = START_ERROR.format(error=error) + (START_ERROR_LOG.format(log=where) if where else '')
        if sys.platform == 'win32':
            message_box(text)
        else:
            try:
                import webview
                webview.create_window(TITLE, html=f'<p style="font: 15px system-ui; margin: 2em">{html.escape(text)}</p>')
                webview.start()
            except Exception:
                pass   # the log is all that is left
        return 1


if __name__ == '__main__':
    sys.exit(main())
