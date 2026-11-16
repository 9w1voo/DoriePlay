"""Apple Music developer token service. The MusicKit private key never leaves the server; the program on a
user's computer asks for a short-lived ES256 token (JWT) and uses it to read Apple Music. Nothing is kept on
disk and nothing about requests is written anywhere (no addresses, no tokens): per-address counters live in
memory and are dropped every hour. Plain HTTP on 127.0.0.1: a proxy (Caddy) adds HTTPS, and the address is
then the last X-Forwarded-For entry, the one that proxy added.

    AM_TEAM_ID=... AM_KEY_ID=... python amtoken.py [--port 8773] [--key PATH]     PATH: the .p8 key
"""
import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from relay import _Deadline, address_key

PATH = '/v1/applemusic/token'
PORT = 8773
LIFETIME = 43200        # seconds a token lives (12 h)
RENEW = 21600           # a new one is made when less than this is left (6 h)
PER_ADDRESS = 60        # requests per hour from one address (IPv6: one /64 network)
TABLE = 50000           # addresses remembered at once
CONNECTIONS = 32        # open connections at once
READ_TIMEOUT = 10       # seconds for a whole request to arrive


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def der_to_raw(der):
    """openssl's DER signature (SEQUENCE of two INTEGERs) -> 64 bytes r||s, as JWT ES256 wants."""
    def integer(at):
        if der[at] != 2:
            raise ValueError
        size = der[at + 1]
        return int.from_bytes(der[at + 2:at + 2 + size], 'big'), at + 2 + size

    if der[0] != 0x30:
        raise ValueError
    r, at = integer(2)
    s, _ = integer(at)
    return r.to_bytes(32, 'big') + s.to_bytes(32, 'big')


def mint(team_id, key_id, key_path, now):
    """(JWT, exp). Signs with the system openssl. Raises when the key or openssl does not work."""
    compact = lambda value: json.dumps(value, separators=(',', ':')).encode()
    exp = now + LIFETIME
    signing_input = b64(compact({'alg': 'ES256', 'kid': key_id})) + '.' + \
        b64(compact({'iss': team_id, 'iat': now, 'exp': exp}))
    done = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', key_path], input=signing_input.encode(),
                          capture_output=True, timeout=10, check=True)
    return signing_input + '.' + b64(der_to_raw(done.stdout)), exp


def serve(team_id, key_id, key_path, port=PORT, host='127.0.0.1', per_address=PER_ADDRESS, table=TABLE,
          connections=CONNECTIONS):
    """The server, not yet started. The limit window is one hour (fixed: a burst at its edge can reach 2x)."""
    lock = threading.Lock()
    counts, window = {}, [time.monotonic()]
    current = [None, 0]     # the token everyone gets, and its exp
    open_connections = threading.BoundedSemaphore(connections)

    def count(key):
        """True when this address may go on."""
        with lock:
            if time.monotonic() - window[0] > 3600:
                counts.clear()
                window[0] = time.monotonic()
            if key not in counts and len(counts) >= table:
                return False
            counts[key] = counts.get(key, 0) + 1
            return counts[key] <= per_address

    def token():
        """(token, exp), or None when signing fails. Made under the lock: concurrent callers mint once."""
        with lock:
            now = int(time.time())
            if current[0] is None or current[1] - now < RENEW:
                try:
                    current[0], current[1] = mint(team_id, key_id, key_path, now)
                except Exception:   # noqa: BLE001  nothing about the failure leaves this function
                    current[0], current[1] = None, 0
                    return None
            return current[0], current[1]

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
        server_version = 'DorieToken'
        timeout = READ_TIMEOUT

        def setup(self):
            super().setup()
            self.rfile = io.BufferedReader(_Deadline(self.connection, time.monotonic() + READ_TIMEOUT))

        def log_message(self, *args):   # privacy: nothing about requests is written anywhere
            pass

        def answer(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def not_found(self):
            self.answer(404, {'error': 'not_found'})

        do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = not_found

        def address(self):
            peer = self.client_address[0]
            forwarded = self.headers.get('X-Forwarded-For') if peer in ('127.0.0.1', '::1') else None
            key = address_key(forwarded.split(',')[-1]) if forwarded else None
            return key or address_key(peer) or peer

        def do_GET(self):
            if self.path != PATH:
                return self.not_found()
            if self.headers.get('Origin') is not None or self.headers.get('Sec-Fetch-Mode') is not None:
                return self.answer(403, {'error': 'forbidden'})
            if not count(self.address()):
                return self.answer(429, {'error': 'token_limit'})
            made = token()
            if made is None:
                return self.answer(503, {'error': 'token_unavailable'})
            self.answer(200, {'token': made[0], 'expires_at': made[1]})

    return Server((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Apple Music developer token service for Dorie Play.')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--key', default=os.path.join(os.environ.get('CREDENTIALS_DIRECTORY', ''), 'applemusic'))
    args = parser.parse_args(argv)
    ids = {name: os.environ.get(name, '') for name in ('AM_TEAM_ID', 'AM_KEY_ID')}
    for name, value in ids.items():
        if not re.fullmatch(r'[A-Z0-9]{10}', value):
            print(f'{name} must be exactly 10 characters, A-Z and 0-9.', file=sys.stderr)
            raise SystemExit(2)
    if not os.path.exists(args.key):
        print('The key file was not found.', file=sys.stderr)
        raise SystemExit(2)
    httpd = serve(ids['AM_TEAM_ID'], ids['AM_KEY_ID'], args.key, port=args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
