"""Browser sign-in shared by the services: show the link, open it, wait for the answer with a time limit.

The printed line "Sign in to <service> in your browser: <link>" is what the page turns into a sign-in
card (parse). Standard library only.
"""
import html
import re
import socket
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

TIMEOUT = 300       # seconds a sign-in may take
READ_TIMEOUT = 5    # seconds a connected browser may take to send its request
LABELS = ('Spotify', 'YouTube', 'SoundCloud')
HOSTS = frozenset({'accounts.spotify.com', 'accounts.google.com', 'secure.soundcloud.com'})
_LINE = re.compile(r'Sign in to (\w+) in your browser: (\S+)')


def open_url(url):
    """The one place our Python opens an address. A Mac App Store build replaces this function."""
    webbrowser.open(url)


def ask(label, url):
    print(f'Sign in to {label} in your browser: {url}')
    open_url(url)


def parse(line):
    """{'service', 'url'} for a sign-in line of a known service pointing at its sign-in host, else None."""
    match = _LINE.fullmatch(line)
    if not match:
        return None
    label, url = match.groups()
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:   # a malformed link such as "https://[x"
        return None
    if label in LABELS and parsed.scheme == 'https' and host in HOSTS:
        return {'service': label, 'url': url}
    return None


class ExclusiveBind:
    """Mixin for socketserver servers: nobody else may listen on our port.

    On Windows SO_REUSEADDR lets another program take a port that is in use, so it is replaced by
    SO_EXCLUSIVEADDRUSE. On POSIX SO_REUSEADDR only allows taking a port left in TIME_WAIT; without it a
    quick restart on 8765 would fail, so there it stays.
    """
    allow_reuse_port = False   # SO_REUSEPORT would let two servers share the port

    if sys.platform == 'win32':
        allow_reuse_address = False

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


class _Server(ExclusiveBind, HTTPServer):
    timeout = 1   # handle_request returns every second, so the deadline is checked


def wait_for_code(label, auth_url, redirect_uri, state, timeout=TIMEOUT):
    """Show the sign-in link and wait until the browser comes back to redirect_uri; return the code."""
    redirect = urlparse(redirect_uri)
    host, port = redirect.hostname, redirect.port or 80
    result = {}

    class Callback(BaseHTTPRequestHandler):
        timeout = READ_TIMEOUT   # a connection that sends nothing is dropped, then the deadline is checked

        def do_GET(self):
            url = urlparse(self.path)
            if url.path != (redirect.path or '/'):
                self.send_error(404)
                return
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            if query.get('state') != state:   # an older attempt's tab must not end this one
                message = 'This sign-in page is out of date. Use the newest one.'
            else:
                result.update(query)
                message = (f'Signed in to {label}. You can close this tab and go back to Dorie Play.'
                           if 'code' in query else f'{label} sign-in failed. You can close this tab.')
            body = f'<!doctype html><meta charset="utf-8"><title>Dorie Play</title><p>{html.escape(message)}</p>'
            body = body.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass   # the request line holds the code

    try:
        server = _Server((host, port), Callback)
    except OSError as error:
        raise ValueError(f'Cannot wait for the {label} sign-in on {host}:{port}: the port is busy. '
                         'Another Dorie Play may be waiting for a sign-in: close it and try again.') from error
    try:
        ask(label, auth_url)
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise PermissionError(f'{label} sign-in was not finished within {timeout // 60} minutes. '
                              'Start again and finish signing in in the browser on this computer.')
    if 'code' not in result:
        raise PermissionError(f"{label} sign-in failed: "
                              f"{result.get('error_description') or result.get('error') or 'no code came back'}")
    print(f'Signed in to {label}.')
    return result['code']
