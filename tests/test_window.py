"""The window module (window.py) without pywebview: ports, start-up errors, closing, the server-only mode.

Run from the project folder:  python -m unittest discover tests
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import server  # noqa: E402
import window  # noqa: E402


class Window(unittest.TestCase):
    def test_missing_streams_go_nowhere(self):
        with mock.patch.object(sys, 'stdout', None), mock.patch.object(sys, 'stderr', None):
            window.quiet_streams()
            self.addCleanup(sys.stdout.close)
            self.addCleanup(sys.stderr.close)
            print('x')
            print('y', file=sys.stderr)

    def test_a_busy_port_is_skipped_and_sign_in_ports_are_never_used(self):
        blocker = socket.socket()
        self.addCleanup(blocker.close)
        blocker.bind(('127.0.0.1', 0))
        blocker.listen()
        busy = blocker.getsockname()[1]
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        free = probe.getsockname()[1]
        probe.close()
        app = server.App(Path(tempfile.mkdtemp()) / 'plans', demo=True)
        with mock.patch.object(window, 'PORTS', (busy, free)):
            http, port = window.start_server(app)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.assertEqual(port, free)
        self.assertNotIn(8888, window.PORTS)
        self.assertNotIn(8890, window.PORTS)

    def test_webview2_is_found_only_with_a_real_version(self):
        self.assertFalse(window.has_webview2(lambda hive, path: None))
        self.assertFalse(window.has_webview2(lambda hive, path: '0.0.0.0'))
        self.assertFalse(window.has_webview2(lambda hive, path: ''))
        self.assertTrue(window.has_webview2(lambda hive, path: '131.0.2903.70' if hive == 'HKCU' else None))

    def test_closing_asks_only_while_a_task_runs(self):
        class FakeWindow:
            confirm_close = None
        for busy in (True, False):
            fake, app = FakeWindow(), mock.Mock()
            app.busy.return_value = busy
            self.assertIsNone(window.on_closing(fake, app))
            self.assertIs(fake.confirm_close, busy)

    def test_a_failed_start_is_logged_somewhere_readable(self):
        with tempfile.TemporaryDirectory() as scratch:
            blocker = Path(scratch) / 'file'
            blocker.write_text('not a folder')
            fallback = Path(tempfile.gettempdir()) / 'dorieplay-startup.log'
            self.addCleanup(fallback.unlink, missing_ok=True)
            with mock.patch.object(window, 'LOG', blocker / 'startup.log'):
                self.assertEqual(window.log_error('x'), fallback)
            self.assertIn('x', fallback.read_text(encoding='utf-8'))

    def test_only_the_window_module_imports_webview(self):
        files = [*PROJECT.glob('*.py'), *PROJECT.glob('platforms/*.py'), *PROJECT.glob('tests/*.py')]
        self.assertGreater(len(files), 10)
        for path in files:
            if path.name == 'window.py':
                continue
            text = path.read_text(encoding='utf-8')
            self.assertIsNone(re.search(r'^\s*(import webview|from webview)', text, re.M), path.name)
        self.assertNotIn('pywebview', (PROJECT / 'requirements.txt').read_text())
        self.assertIn('pywebview==6.2.1', (PROJECT / 'requirements-window.txt').read_text())

    def test_server_only_mode_serves_the_page(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch) / 'new'
            child = subprocess.Popen([sys.executable, 'window.py', '--demo', '--no-window'], cwd=PROJECT,
                                     stdout=subprocess.PIPE, text=True,
                                     env={**os.environ, 'DORIEPLAY_HOME': str(home), 'DORIEPLAY_SONGBASE': ''})
            self.addCleanup(child.stdout.close)
            self.addCleanup(child.wait, timeout=10)
            self.addCleanup(child.terminate)
            line = []
            reader = threading.Thread(target=lambda: line.append(child.stdout.readline()), daemon=True)
            reader.start()
            reader.join(30)
            self.assertTrue(line and line[0].startswith('http://127.0.0.1:'), line)
            page = urllib.request.urlopen(line[0].strip(), timeout=10).read().decode()
            self.assertIn('name="dorie-token"', page)
            self.assertNotIn('__TOKEN__', page)
            self.assertTrue(home.is_dir())
            if os.name != 'nt':   # Windows has no such modes
                self.assertEqual(home.stat().st_mode & 0o777, 0o700)


if __name__ == '__main__':
    unittest.main()
