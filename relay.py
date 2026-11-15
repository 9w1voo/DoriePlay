"""SoundCloud sign-in relay. The program on a user's computer sends a sign-in code or a refresh token;
the relay adds the app's client_id and client_secret, asks SoundCloud, and sends back only the token fields.
It keeps nothing on disk and writes no log (no addresses, no tokens): per-address counters live in memory
and are dropped every hour. Plain HTTP on 127.0.0.1: on the server a proxy (Caddy) adds HTTPS, and the
address is then the last X-Forwarded-For entry, the one that proxy added.

    python relay.py [--port 8772] [--secret PATH]     PATH: JSON {"client_id": ..., "client_secret": ...}
"""
import argparse
import ipaddress
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN_URL = 'https://secure.soundcloud.com/oauth/token'
REDIRECT = 'http://127.0.0.1:8890/callback'   # the fixed one the program uses; a caller cannot choose another
PATH = '/v1/soundcloud/token'
PORT = 8772
PER_ADDRESS = 60        # requests per hour from one address (IPv6: one /64 network)
TOTAL = 1000            # forwards to SoundCloud per hour, all addresses together
TABLE = 50000           # addresses remembered at once
PARALLEL = 8            # forwards in flight at once
CONNECTIONS = 32        # open connections at once
READ_TIMEOUT = 10       # seconds for a whole request to arrive
UPSTREAM_TIMEOUT = 20   # seconds SoundCloud may take
MAX_BODY = 4096
MAX_VALUE = 2048
TOKEN_FIELDS = ('access_token', 'refresh_token', 'expires_in', 'scope', 'token_type')


def load_secret(path):
    """(client_id, client_secret) from a JSON file. Errors never repeat what the file holds."""
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('The secret file is not valid JSON.') from None
    client_id = data.get('client_id') if isinstance(data, dict) else None
    client_secret = data.get('client_secret') if isinstance(data, dict) else None
    if not (isinstance(client_id, str) and client_id and isinstance(client_secret, str) and client_secret):
        raise ValueError('The secret file needs "client_id" and "client_secret" as non-empty text.')
    return client_id, client_secret


def address_key(address):
    """The counting key: an IPv4 address as it is, an IPv6 address as its /64 network (one home gets a
    whole /64, so counting single addresses would be no limit); None when it is not an address."""
    try:
        parsed = ipaddress.ip_address(str(address).strip().split('%')[0])
    except ValueError:
        return None
    if parsed.version == 6:
        return str(ipaddress.ip_network(f'{parsed}/64', strict=False))
    return str(parsed)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # the 3xx then surfaces as an HTTPError


def _forward(form):
    """POST the form to SoundCloud. -> (status, parsed JSON object or None, headers). Network errors raise."""
    request = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(), method='POST',
                                     headers={'Accept': 'application/json', 'User-Agent': 'DoriePlay-relay'})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=UPSTREAM_TIMEOUT)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status, headers = response.status if hasattr(response, 'status') else response.code, dict(response.headers.items())
        raw = response.read(65536)
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    return status, parsed if isinstance(parsed, dict) else None, headers


def serve(client_id, client_secret, port=PORT, host='127.0.0.1', per_address=PER_ADDRESS, total=TOTAL,
          table=TABLE, parallel=PARALLEL, connections=CONNECTIONS):
    """The server, not yet started. The limit window is one hour (fixed: a burst at its edge can reach 2x)."""
    lock = threading.Lock()
    counts, window, forwards = {}, [time.monotonic()], [0]
    forwarding = threading.BoundedSemaphore(parallel)
    open_connections = threading.BoundedSemaphore(connections)
    read_timeout = READ_TIMEOUT

    def scrub(value):
        return value.replace(client_secret, '***') if isinstance(value, str) else value

    def count(key):
        """True when this address may go on. Called only for requests that are well formed."""
        with lock:
            if time.monotonic() - window[0] > 3600:
                counts.clear()
                forwards[0] = 0
                window[0] = time.monotonic()
            if key not in counts and len(counts) >= table:
                return False
            counts[key] = counts.get(key, 0) + 1
            return counts[key] <= per_address

    def count_forward():
        with lock:
            if forwards[0] >= total:
                return False
            forwards[0] += 1
            return True

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def process_request(self, request, client_address):
            if not open_connections.acquire(blocking=False):
                request.close()
                return
            super().process_request(request, client_address)

        def shutdown_request(self, request):
            try:
                super().shutdown_request(request)
            finally:
                open_connections.release()

        def handle_error(self, request, client_address):   # privacy: nothing is printed, not even a traceback
            pass

    class Handler(BaseHTTPRequestHandler):
        server_version = 'DorieRelay'
        timeout = read_timeout   # per read; the whole request also has a deadline (see read_body)

        def setup(self):
            super().setup()
            self.deadline = time.monotonic() + read_timeout

        def log_message(self, *args):   # privacy: nothing about requests is written anywhere
            pass

        def answer(self, status, body, headers=()):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def refuse(self, status, error):
            """Answer an error. Unread request data is read off first (a little): closing a socket with
            unread data makes the client see a reset instead of this answer."""
            length = self.headers.get('Content-Length', '')
            if length.isascii() and length.isdigit() and not self.body_read:
                try:
                    self.read_body(min(int(length), 2 * MAX_BODY))
                except OSError:
                    pass
            self.answer(status, {'error': error})

        def read_body(self, length):
            """Exactly `length` bytes, within the deadline for the whole request. Raises OSError."""
            body = b''
            while len(body) < length:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(length - len(body))
                if not chunk:
                    raise ConnectionError
                body += chunk
            self.body_read = True
            return body

        def not_found(self):
            self.answer(404, {'error': 'not_found'})

        do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = not_found

        def address(self):
            peer = self.client_address[0]
            forwarded = self.headers.get('X-Forwarded-For') if peer in ('127.0.0.1', '::1') else None
            key = address_key(forwarded.split(',')[-1]) if forwarded else None
            return key or address_key(peer) or peer

        def do_POST(self):
            self.body_read = False
            if self.path != PATH:
                return self.not_found()
            if self.headers.get('Origin') is not None or self.headers.get('Sec-Fetch-Mode') is not None:
                return self.refuse(403, 'forbidden')
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                return self.refuse(415, 'unsupported_media_type')
            length = self.headers.get('Content-Length', '')
            if not (length.isascii() and length.isdigit()) or int(length) == 0:
                return self.refuse(400, 'bad_request')
            if int(length) > MAX_BODY:
                return self.refuse(413, 'too_large')
            try:
                raw = self.read_body(int(length))
            except OSError:   # too slow, or gone: no answer, just close
                self.close_connection = True
                return
            form = self.form(raw)
            if form is None:
                return self.refuse(400, 'bad_request')
            if not count(self.address()):
                return self.answer(429, {'error': 'relay_limit'})
            if not forwarding.acquire(blocking=False):
                return self.answer(503, {'error': 'relay_busy'})
            try:
                if not count_forward():
                    return self.answer(503, {'error': 'relay_busy'})
                try:
                    status, parsed, headers = _forward(form)
                    self.pass_on(status, parsed, headers)
                except Exception:   # noqa: BLE001  whatever went wrong, the caller learns nothing more
                    self.answer(502, {'error': 'soundcloud_refused'})
            finally:
                forwarding.release()

        def form(self, raw):
            """The form to send to SoundCloud, or None when the request is not a plain code/refresh one."""
            try:
                body = json.loads(raw)
            except ValueError:
                return None
            if not isinstance(body, dict):
                return None

            def text(name, low=1, high=MAX_VALUE):
                value = body.get(name)
                return value if isinstance(value, str) and low <= len(value) <= high else None

            grant = body.get('grant_type')
            if grant == 'authorization_code':
                fields = {'code': text('code'), 'code_verifier': text('code_verifier', 43, 128)}
            elif grant == 'refresh_token':
                fields = {'refresh_token': text('refresh_token')}
            else:
                return None
            if None in fields.values():
                return None
            return {'client_id': client_id, 'client_secret': client_secret, 'grant_type': grant, **fields,
                    'redirect_uri': REDIRECT}

        def pass_on(self, status, parsed, headers):
            extra = [(name, scrub(value)) for name, value in headers.items() if name.lower().startswith('x-ratelimit-')]
            if status == 200 and parsed and all(isinstance(parsed.get(k), str) for k in ('access_token', 'refresh_token')):
                return self.answer(200, {k: scrub(parsed[k]) for k in TOKEN_FIELDS if k in parsed}, extra)
            if status in (400, 401) and parsed and isinstance(parsed.get('error'), str) and len(parsed['error']) <= 100:
                body = {'error': scrub(parsed['error'])}
                if isinstance(parsed.get('error_description'), str):
                    body['error_description'] = scrub(parsed['error_description'])[:200]
                return self.answer(status, body, extra)
            if status == 429:
                wait = next((v for k, v in headers.items() if k.lower() == 'retry-after'), '')
                if wait.isascii() and wait.isdigit():
                    extra.append(('Retry-After', wait))
                return self.answer(503, {'error': 'soundcloud_busy'}, extra)
            self.answer(502, {'error': 'soundcloud_refused'}, extra)

    return Server((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='SoundCloud sign-in relay for Dorie Play.')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--secret', default=os.path.join(os.environ.get('CREDENTIALS_DIRECTORY', ''), 'soundcloud'))
    args = parser.parse_args(argv)
    if not os.path.exists(args.secret):
        print(f'Secret file not found: {args.secret}', file=sys.stderr)
        raise SystemExit(2)
    try:
        client_id, client_secret = load_secret(args.secret)
    except ValueError as error:
        print(error, file=sys.stderr)
        raise SystemExit(2)
    httpd = serve(client_id, client_secret, port=args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
