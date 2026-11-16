"""The Apple Music developer token service: signs a short-lived ES256 token with the key that stays on the server.
Run from the project folder:  python -m unittest discover tests   (no network; needs the system openssl)"""
import base64
import contextlib
import http.client
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import amtoken  # noqa: E402

TEAM, KEY = 'ABCDE12345', 'KEY1234567'


def b64d(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def der_int(value):
    raw = value.lstrip(b'\0') or b'\0'
    raw = (b'\0' if raw[0] & 0x80 else b'') + raw
    return b'\x02' + bytes([len(raw)]) + raw


def raw_to_der(raw):
    body = der_int(raw[:32]) + der_int(raw[32:])
    return b'\x30' + bytes([len(body)]) + body


class TestAmToken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('openssl'):
            raise unittest.SkipTest('openssl is not installed')
        cls.folder = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.folder, True)
        sec1, cls.p8 = (str(Path(cls.folder, name)) for name in ('key.pem', 'key.p8'))
        cls.pub = str(Path(cls.folder, 'pub.pem'))
        run = lambda *args: subprocess.run(['openssl', *args], check=True, capture_output=True)
        run('ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', sec1)
        run('pkcs8', '-topk8', '-nocrypt', '-in', sec1, '-out', cls.p8)
        run('ec', '-in', sec1, '-pubout', '-out', cls.pub)

    def start(self, key=None, **options):
        httpd = amtoken.serve(TEAM, KEY, key or self.p8, port=0, **options)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        import threading
        threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
        return httpd.server_address[1]

    def call(self, port, method='GET', path=amtoken.PATH, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, (json.loads(body) if body else None), response

    def test_der_signatures_become_64_raw_bytes(self):
        r = b'\x80' + b'\x11' * 31          # high bit set: DER adds a 00, 33 bytes
        s = b'\x22' * 31                    # short: 31 bytes
        der = b'\x30\x40' + b'\x02\x21\x00' + r + b'\x02\x1f' + s
        raw = amtoken.der_to_raw(der)
        self.assertEqual(len(raw), 64)
        self.assertEqual(raw[:32], r)
        self.assertEqual(raw[32:], b'\0' + s)

    def test_a_minted_token_is_a_valid_es256_jwt(self):
        token, exp = amtoken.mint(TEAM, KEY, self.p8, now=1_000_000)
        parts = token.split('.')
        self.assertEqual(len(parts), 3)
        self.assertNotIn('=', token)
        self.assertEqual(json.loads(b64d(parts[0])), {'alg': 'ES256', 'kid': KEY})
        self.assertEqual(json.loads(b64d(parts[1])), {'iss': TEAM, 'iat': 1_000_000, 'exp': 1_043_200})
        self.assertEqual(exp, 1_043_200)
        signature = b64d(parts[2])
        self.assertEqual(len(signature), 64)
        sig_file = Path(self.folder, 'sig.der')
        sig_file.write_bytes(raw_to_der(signature))
        done = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', self.pub, '-signature', str(sig_file)],
                              input=f'{parts[0]}.{parts[1]}'.encode(), capture_output=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_everyone_gets_the_same_token_until_it_is_half_used(self):
        with mock.patch.object(amtoken.time, 'time') as clock, \
                mock.patch.object(amtoken, 'mint', wraps=amtoken.mint) as mint:
            clock.return_value = 1_000_000
            port = self.start()
            first = self.call(port)[1]
            self.assertEqual(self.call(port)[1], first)
            self.assertEqual(first['expires_at'], 1_000_000 + amtoken.LIFETIME)
            clock.return_value = 1_000_000 + 21600 + 1
            second = self.call(port)[1]
            self.assertNotEqual(second['token'], first['token'])
            self.assertEqual(mint.call_count, 2)

    def test_browser_requests_and_other_paths_are_refused(self):
        port = self.start()
        status, body, response = self.call(port)
        self.assertEqual(status, 200)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        for headers in ({'Origin': 'https://evil.example'}, {'Origin': ''}, {'Sec-Fetch-Mode': 'cors'}):
            status, body, response = self.call(port, headers=headers)
            self.assertEqual((status, body), (403, {'error': 'forbidden'}), headers)
            self.assertFalse([h for h, _ in response.getheaders() if h.lower().startswith('access-control')])
        self.assertEqual(self.call(port, 'POST')[0], 404)
        self.assertEqual(self.call(port, path='/v1/other')[0], 404)
        self.assertEqual(self.call(port, path=amtoken.PATH + '?x=1')[0], 404)

    def test_limits_per_address_and_network(self):
        port = self.start(per_address=2, table=3)

        def ask(forwarded):
            return self.call(port, headers={'X-Forwarded-For': forwarded})[:2]

        self.assertEqual(ask('1.1.1.1')[0], 200)
        self.assertEqual(ask('1.1.1.1')[0], 200)
        self.assertEqual(ask('1.1.1.1'), (429, {'error': 'token_limit'}))
        self.assertEqual(ask('2a01:4f9::1')[0], 200)
        self.assertEqual(ask('2a01:4f9::2')[0], 200)
        self.assertEqual(ask('2a01:4f9::3')[0], 429)
        self.assertEqual(ask('9.9.9.9, 3.3.3.3')[0], 200)   # counted as 3.3.3.3
        self.assertEqual(ask('3.3.3.3')[0], 200)
        self.assertEqual(ask('9.9.9.9')[0], 429)            # table (3 addresses) is full: a new one is refused

    def test_a_broken_key_answers_503_and_prints_nothing(self):
        empty = Path(self.folder, 'empty.p8')
        empty.write_bytes(b'')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            port = self.start(key=str(empty))
            status, body, _ = self.call(port)
            again = self.call(port)[0]
        self.assertEqual((status, body, again), (503, {'error': 'token_unavailable'}, 503))
        self.assertNotIn(str(empty), json.dumps(body))
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))

    def test_ids_are_checked_without_echoing_them(self):
        err = io.StringIO()
        with mock.patch.dict('os.environ', {'AM_TEAM_ID': 'bad', 'AM_KEY_ID': KEY}), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            amtoken.main(['--key', self.p8])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn('bad', err.getvalue())


if __name__ == '__main__':
    unittest.main()
