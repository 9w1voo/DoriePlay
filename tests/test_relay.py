"""The SoundCloud sign-in relay: adds the app's secret, forwards to SoundCloud, keeps nothing.
Run from the project folder:  python -m unittest discover tests   (no network: local servers only)"""
import contextlib
import http.client
import io
import json
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relay  # noqa: E402

CODE = {'grant_type': 'authorization_code', 'code': 'c1', 'code_verifier': 'v' * 43}
TOKENS = {'access_token': 'a', 'refresh_token': 'r', 'expires_in': 3599, 'scope': '', 'token_type': 'bearer'}


class FakeSoundCloud:
    """A stand-in for SoundCloud's token endpoint: records every form it gets, answers `reply`."""

    def __init__(self):
        self.forms = []
        self.reply = (200, json.dumps(TOKENS).encode(), {})
        self.hold = None            # an Event: answer only after it is set
        self.started = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                form = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                fake.forms.append({k: v[0] for k, v in form.items()})
                fake.started.set()
                if fake.hold:
                    fake.hold.wait(10)
                status, body, headers = fake.reply
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/oauth/token'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSoundCloud()
        self.addCleanup(self.fake.close)
        patch = mock.patch.object(relay, 'TOKEN_URL', self.fake.url)
        patch.start()
        self.addCleanup(patch.stop)

    def start(self, **options):
        httpd = relay.serve('id1', 's1', port=0, **options)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def call(self, port, body=CODE, method='POST', path='/v1/soundcloud/token', headers=None, raw=None):
        sent = {'Content-Type': 'application/json'}
        sent.update(headers or {})
        data = raw if raw is not None else json.dumps(body).encode()
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        try:
            connection.putrequest(method, path, skip_accept_encoding=True)
            for name, value in sent.items():
                if value is not None:
                    connection.putheader(name, value)
            if 'Content-Length' not in sent and method == 'POST':
                connection.putheader('Content-Length', str(len(data)))
            connection.endheaders(data if method == 'POST' else None)
            response = connection.getresponse()
            raw_body = response.read()
            return response.status, response, raw_body
        finally:
            connection.close()

    def json_call(self, port, *args, **kwargs):
        status, response, raw = self.call(port, *args, **kwargs)
        return status, json.loads(raw), response


class TestRelay(Base):
    def test_a_code_is_exchanged_with_the_secret_and_only_token_fields_come_back(self):
        self.fake.reply = (200, json.dumps({**TOKENS, 'extra': 'x', 'client_secret': 's1'}).encode(),
                           {'X-Ratelimit-Remaining': '90', 'Set-Cookie': 'k=v'})
        port = self.start()
        status, response, raw = self.call(port)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), TOKENS)
        self.assertNotIn(b's1', raw)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertEqual(response.getheader('X-Ratelimit-Remaining'), '90')
        self.assertIsNone(response.getheader('Set-Cookie'))
        self.assertEqual(self.fake.forms, [{'client_id': 'id1', 'client_secret': 's1', 'grant_type': 'authorization_code',
                                            'code': 'c1', 'code_verifier': 'v' * 43, 'redirect_uri': relay.REDIRECT}])

    def test_refresh_sends_the_fixed_redirect_and_ignores_other_fields(self):
        port = self.start()
        status, _, _ = self.call(port, {'grant_type': 'refresh_token', 'refresh_token': 'r1',
                                        'redirect_uri': 'http://evil', 'client_id': 'x', 'scope': 'y'})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.forms, [{'client_id': 'id1', 'client_secret': 's1', 'grant_type': 'refresh_token',
                                            'refresh_token': 'r1', 'redirect_uri': relay.REDIRECT}])

    def test_anything_but_code_and_refresh_is_refused_before_soundcloud(self):
        port = self.start()
        for body in ({'grant_type': 'client_credentials'},
                     {'grant_type': 'authorization_code', 'code_verifier': 'v' * 43},
                     {**CODE, 'code_verifier': 'v' * 42},
                     {**CODE, 'code_verifier': 'v' * 129},
                     {**CODE, 'code': 'c' * 2049},
                     {'grant_type': 'refresh_token', 'refresh_token': 'r' * 2049},
                     {'grant_type': 'refresh_token', 'refresh_token': 5}):
            self.assertEqual(self.call(port, body)[0], 400, body)
        self.assertEqual(self.call(port, raw=b'not json')[0], 400)
        self.assertEqual(self.call(port, raw=b'[1, 2]')[0], 400)
        self.assertEqual(self.call(port, raw=b'x' * 4097)[0], 413)
        for length in ('abc', '0', '-1'):
            self.assertEqual(self.call(port, headers={'Content-Length': length}, raw=b'')[0], 400, length)
        # No Content-Length at all: send the headers by hand.
        with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
            sock.sendall(b'POST /v1/soundcloud/token HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n\r\n')
            self.assertTrue(sock.recv(100).startswith(b'HTTP/1.0 400'))
        self.assertEqual(self.fake.forms, [])

    def test_browser_requests_are_refused(self):
        port = self.start()
        for kind in ('text/plain', 'text/plain; charset=application/json'):
            self.assertEqual(self.call(port, headers={'Content-Type': kind})[0], 415, kind)
        self.assertEqual(self.call(port, headers={'Origin': ''})[0], 403)
        self.assertEqual(self.call(port, headers={'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.call(port, headers={'Sec-Fetch-Mode': 'no-cors'})[0], 403)
        status, response, _ = self.call(port, method='OPTIONS', headers={'Origin': 'https://example.com'})
        self.assertEqual(status, 404)
        self.assertFalse([name for name, _ in response.getheaders() if name.lower().startswith('access-control')])
        self.assertEqual(self.call(port, method='GET')[0], 404)
        self.assertEqual(self.call(port, path='/other')[0], 404)
        self.assertEqual(self.fake.forms, [])
        status, _, _ = self.call(port, headers={'Content-Type': 'application/json; charset=utf-8'})
        self.assertEqual(status, 200)

    def test_soundcloud_answers_are_narrowed(self):
        port = self.start()

        def ask(status, body, headers=None):
            self.fake.reply = (status, body if isinstance(body, bytes) else json.dumps(body).encode(), headers or {})
            return self.json_call(port)

        status, body, _ = ask(400, {'error': 'invalid_grant', 'error_description': 'x' * 500, 'client_secret': 's1'})
        self.assertEqual((status, body['error'], len(body['error_description'])), (400, 'invalid_grant', 200))
        self.assertNotIn('s1', json.dumps(body))
        status, body, _ = ask(400, {'error': 'invalid_grant', 'error_description': 'bad secret s1'})
        self.assertEqual(status, 400)
        self.assertNotIn('s1', json.dumps(body))
        self.assertEqual(ask(400, {'error': 5})[:2], (502, {'error': 'soundcloud_refused'}))
        self.assertEqual(ask(400, {'error': 'e' * 101})[0], 502)
        self.assertEqual(ask(401, {'error': 'invalid_client'})[:2], (401, {'error': 'invalid_client'}))
        status, body, response = ask(429, {'error': 'busy'}, {'Retry-After': '30'})
        self.assertEqual((status, body, response.getheader('Retry-After')), (503, {'error': 'soundcloud_busy'}, '30'))
        status, body, response = ask(429, {'error': 'busy'}, {'Retry-After': 'soon'})
        self.assertEqual((status, body, response.getheader('Retry-After')), (503, {'error': 'soundcloud_busy'}, None))
        self.assertEqual(ask(403, b'<html>no</html>')[:2], (502, {'error': 'soundcloud_refused'}))
        self.assertEqual(ask(202, TOKENS)[0], 502)
        self.assertEqual(ask(200, {'access_token': 'a'})[0], 502)
        before = len(self.fake.forms)
        self.assertEqual(ask(302, b'', {'Location': self.fake.url})[0], 502)
        self.assertEqual(len(self.fake.forms), before + 1)   # the redirect was not followed
        self.fake.close()
        self.assertEqual(self.json_call(port)[:2], (502, {'error': 'soundcloud_refused'}))

    def test_limits_per_address_per_network_and_in_total(self):
        port = self.start(per_address=2)

        def status(forwarded):
            return self.json_call(port, headers={'X-Forwarded-For': forwarded})[:2]

        self.assertEqual(status('1.1.1.1')[0], 200)
        self.assertEqual(status('1.1.1.1')[0], 200)
        self.assertEqual(status('1.1.1.1'), (429, {'error': 'relay_limit'}))
        self.assertEqual(status('2a01:4f9::1')[0], 200)
        self.assertEqual(status('2a01:4f9::2')[0], 200)
        self.assertEqual(status('2a01:4f9::3')[0], 429)
        self.assertEqual(status('9.9.9.9, 3.3.3.3')[0], 200)
        self.assertEqual(status('3.3.3.3')[0], 200)
        self.assertEqual(status('8.8.8.8, 3.3.3.3')[0], 429)
        self.assertEqual(status('junk')[0], 200)
        self.assertEqual(status('junk')[0], 200)
        self.assertEqual(status('also junk')[0], 429)

        port = self.start(per_address=1)
        headers = {'X-Forwarded-For': '4.4.4.4'}
        self.assertEqual(self.call(port, method='GET', headers=headers)[0], 404)
        self.assertEqual(self.call(port, headers={**headers, 'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.call(port, {'grant_type': 'x'}, headers=headers)[0], 400)
        self.assertEqual(self.call(port, headers=headers)[0], 200)

        port = self.start(total=2)
        self.assertEqual([self.json_call(port, headers={'X-Forwarded-For': f'7.7.7.{n}'})[0] for n in (1, 2, 3)],
                         [200, 200, 503])
        self.assertEqual(self.json_call(port, headers={'X-Forwarded-For': '7.7.7.4'})[1], {'error': 'relay_busy'})

        port = self.start(table=2)
        for n in range(1, 6):
            self.assertEqual(self.call(port, {'grant_type': 'x'}, headers={'X-Forwarded-For': f'5.5.5.{n}'})[0], 400)
        results = [self.call(port, headers={'X-Forwarded-For': f'6.6.6.{n}'})[0] for n in (1, 2, 3)]
        self.assertEqual(results, [200, 200, 429])

    def test_the_limits_start_over_after_an_hour(self):
        offset = [0]
        clock = types.SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0])
        with mock.patch.object(relay, 'time', clock):
            port = self.start(per_address=1, total=1)
            first = {'X-Forwarded-For': '1.1.1.1'}
            self.assertEqual(self.json_call(port, headers=first)[0], 200)
            self.assertEqual(self.json_call(port, headers=first), (429, {'error': 'relay_limit'}, mock.ANY))
            self.assertEqual(self.json_call(port, headers={'X-Forwarded-For': '2.2.2.2'})[:2],
                             (503, {'error': 'relay_busy'}))
            offset[0] = 3601
            self.assertEqual(self.json_call(port, headers=first)[0], 200)

    def test_too_many_at_once_are_turned_away(self):
        port = self.start(parallel=1)
        self.fake.hold = threading.Event()
        first = []
        thread = threading.Thread(target=lambda: first.append(self.call(port)[0]))
        thread.start()
        self.assertTrue(self.fake.started.wait(5))
        self.assertEqual(self.json_call(port)[:2], (503, {'error': 'relay_busy'}))
        self.fake.hold.set()
        thread.join(10)
        self.assertEqual(first, [200])

        port = self.start(connections=2)
        quiet = [socket.create_connection(('127.0.0.1', port)) for _ in range(2)]
        for sock in quiet:
            self.addCleanup(sock.close)
        third = socket.create_connection(('127.0.0.1', port), timeout=3)
        self.addCleanup(third.close)
        try:
            self.assertEqual(third.recv(10), b'')
        except ConnectionResetError:
            pass
        for sock in quiet:
            sock.close()
        for _ in range(40):
            if self.call(port)[0] == 200:
                break
            time.sleep(0.05)
        else:
            self.fail('the connections were never given back')

    def test_a_slow_client_is_dropped_and_nothing_is_printed(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(relay, 'READ_TIMEOUT', 1):
                port = self.start()
            head = (b'POST /v1/soundcloud/token HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
                    b'Content-Length: 100\r\n\r\n')
            started = time.monotonic()
            with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
                sock.sendall(head)
                self.assertEqual(sock.recv(100), b'')
            self.assertLess(time.monotonic() - started, 3)

            started = time.monotonic()
            with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
                sock.sendall(head)
                for _ in range(15):
                    try:
                        sock.sendall(b' ')
                    except OSError:
                        break
                    time.sleep(0.3)
            self.assertLess(time.monotonic() - started, 3)

            started = time.monotonic()   # headers that never end: one byte every 0.3 s
            with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
                sock.sendall(b'POST /v1/soundcloud/token HTTP/1.1\r\nX-A: ')
                for _ in range(15):
                    try:
                        sock.sendall(b'a')
                    except OSError:
                        break
                    time.sleep(0.3)
            self.assertLess(time.monotonic() - started, 3)

            self.assertEqual(self.call(port)[0], 200)
            with mock.patch.object(relay, '_forward', side_effect=RuntimeError('boom s1')):
                self.assertEqual(self.json_call(port)[:2], (502, {'error': 'soundcloud_refused'}))
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))

    def test_the_secret_file_is_read_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / 'bad'
            bad.write_text(json.dumps({'client_id': 'idvalue', 'secret': 'hidden-value'}))
            with self.assertRaises(ValueError) as caught:
                relay.load_secret(bad)
            self.assertNotIn('idvalue', str(caught.exception))
            self.assertNotIn('hidden-value', str(caught.exception))
            good = Path(folder) / 'good'
            good.write_text(json.dumps({'client_id': 'id1', 'client_secret': 's1'}))
            self.assertEqual(relay.load_secret(good), ('id1', 's1'))

    def test_address_key(self):
        self.assertEqual(relay.address_key('1.2.3.4'), '1.2.3.4')
        self.assertEqual(relay.address_key('2a01:4f9::1'), relay.address_key('2a01:4f9:0:0:ffff::2'))
        self.assertNotEqual(relay.address_key('2a01:4f9::1'), relay.address_key('2a01:4f9:0:1::1'))
        self.assertIsNone(relay.address_key('junk'))


if __name__ == '__main__':
    unittest.main()
