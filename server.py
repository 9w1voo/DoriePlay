"""Dorie Play in the browser: a local server over the same core as the command line.

  python server.py          real services (keys and sign-in exactly as for dorieplay.py)
  python server.py --demo   invented playlist and fake services, to look around without keys

It listens on 127.0.0.1 only. Plans live in the plans/ folder next to the program.
"""
import argparse
import contextlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import types
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import dorieplay
import platforms

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'web'
STATIC = {'/': ('index.html', 'text/html'), '/app.css': ('app.css', 'text/css'),
          '/app.js': ('app.js', 'text/javascript'), '/i18n.js': ('i18n.js', 'text/javascript'),
          '/logo.svg': ('logo.svg', 'image/svg+xml')}
PLAN_NAME = re.compile(r'[A-Za-z0-9_-]{1,60}')
YOUTUBE_SEARCHES_PER_DAY = 100
# Any web page can send requests to 127.0.0.1, and this server can change the user's playlists:
# every API call needs the token that only our own page receives, and the Host must be ours.
SECURITY_HEADERS = {
    'Content-Security-Policy': "default-src 'self'; frame-src https://www.youtube-nocookie.com "
                               "https://open.spotify.com https://w.soundcloud.com; object-src 'none'; "
                               "base-uri 'none'; form-action 'none'",
    'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store'}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def key_file(name, value, redirect):
    """What to write for a pasted key, or ApiError if it does not look like one. Messages never quote it."""
    if name == 'spotify':
        if not re.fullmatch(r'[0-9a-fA-F]{32}', value):
            raise ApiError(400, 'A Spotify Client ID is 32 characters: digits and the letters a to f.')
        return {'client_id': value.lower()}
    try:
        data = json.loads(value)
    except ValueError:
        data = None
    if name == 'youtube':
        installed = data.get('installed') if isinstance(data, dict) else None
        if not (isinstance(installed, dict) and str(installed.get('client_id', '')).endswith('.apps.googleusercontent.com')
                and isinstance(installed.get('client_secret'), str) and installed.get('token_uri')):
            raise ApiError(400, 'Paste the whole JSON file of a Google OAuth client of the "Desktop app" type.')
        return {'installed': installed}
    fields = data if isinstance(data, dict) else {}
    if not all(re.fullmatch(r'[\w-]{8,100}', str(fields.get(key, ''))) for key in ('client_id', 'client_secret')):
        raise ApiError(400, 'Paste the Client ID and the Client secret of your SoundCloud app.')
    return {'client_id': fields['client_id'], 'client_secret': fields['client_secret'], 'redirect_uri': redirect}


class JobOutput:
    """Collects what the core prints while a job runs (it replaces stdout for that time)."""

    echo = True

    def __init__(self, job):
        self.job, self.rest = job, ''

    def write(self, text):
        if self.echo:   # sign-in links and messages stay visible in the terminal too
            sys.__stdout__.write(text)
        *lines, self.rest = (self.rest + text).split('\n')
        for line in filter(None, lines):
            self.job['lines'] = (self.job['lines'] + [line])[-6:]
            if self.job['kind'] == 'apply' and line.startswith('Added:'):
                self.job['done'] += 1
        return len(text)

    def flush(self):
        pass


class App:
    """Everything the interface can do. One job (search or transfer) runs at a time."""

    def __init__(self, plans_dir, demo=False):
        self.plans, self.demo = Path(plans_dir), demo
        self.plans.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.job = None

    # --- plans ---------------------------------------------------------------------------------

    def path(self, name):
        if not isinstance(name, str) or not PLAN_NAME.fullmatch(name):
            raise ApiError(400, 'Invalid plan name.')
        return self.plans / f'{name}.json'

    def load(self, name):
        path = self.path(name)
        if not path.exists():
            raise ApiError(404, 'There is no such plan.')
        return dorieplay.load(path)

    def summary(self, name, plan):
        tracks = plan['tracks']
        return {'name': name, 'title': plan.get('name'), 'from': plan.get('from'), 'to': plan.get('to'),
                'total': len(tracks),
                'approved': sum(1 for t in tracks if t.get('approved') and t.get('match_id')),
                'applied': bool(plan.get('target_playlist_id')), 'target_url': plan.get('target_url') or '',
                'updated': self.path(name).stat().st_mtime}

    def summaries(self):
        found = []
        for path in self.plans.glob('*.json'):
            with contextlib.suppress(Exception):   # a broken file must not hide the other plans
                found.append(self.summary(path.stem, dorieplay.load(path)))
        return sorted(found, key=lambda plan: plan['updated'], reverse=True)

    def busy(self, name=None):
        job = self.job
        return bool(job and job['state'] == 'running' and (name is None or job['plan'] == name))

    # --- what the pages ask for ----------------------------------------------------------------

    def state(self):
        return {'demo': self.demo, 'job': self.job, 'plans': self.summaries(),
                'platforms': [{'name': p.NAME, 'label': p.LABEL, **self.connection(p)}
                              for p in platforms.PLATFORMS.values()],
                'youtube_searches_left': self.youtube_searches_left()}

    def connection(self, platform):
        """Whether the key and a saved sign-in are in place; a guess from files, nothing is contacted."""
        if self.demo:
            return {'key': True, 'signed_in': True}
        key = platform.client_id() if platform.NAME == 'spotify' else platform.CLIENT_FILE.exists()
        return {'key': bool(key), 'signed_in': platform.TOKEN_FILE.exists()}

    def save_key(self, name, value, replace=False):
        """The set-up page: check that a pasted key has the right shape and save it next to the program,
        readable only by this user. The value is never sent back, printed or logged; whether the key
        really works shows at the first sign-in. A saved sign-in belongs to the old key (YouTube's
        token even carries the old client inside), so it is removed: the next use signs in again."""
        if self.demo:
            raise ApiError(400, 'The demo does not save keys.')
        if name not in ('spotify', 'youtube', 'soundcloud'):
            raise ApiError(404, 'There is no such service.')
        if name == 'spotify' and os.environ.get('SPOTIPY_CLIENT_ID'):
            raise ApiError(409, 'SPOTIPY_CLIENT_ID is set where the program was started (for example in '
                                '~/.zshrc), and it wins over a key saved here. Remove it there and start '
                                'the program again, or change the Client ID there.')
        if not isinstance(value, str) or not value.strip() or len(value) > 20000:
            raise ApiError(400, 'Paste the key first.')
        module = platforms.get(name)
        data = key_file(name, value.strip(), getattr(module, 'DEFAULT_REDIRECT', ''))
        with self.lock:
            if self.busy():
                raise ApiError(409, 'Wait until the running task is finished.')
            if module.CLIENT_FILE.exists() and not replace:
                raise ApiError(409, 'A key for this service is already saved. Tick "Replace" to change it.')
            descriptor = os.open(module.CLIENT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                json.dump(data, file, indent=2)
            os.chmod(module.CLIENT_FILE, 0o600)
            module.TOKEN_FILE.unlink(missing_ok=True)
            if name == 'soundcloud':
                module._token = None    # its _client is a function, the sign-in is cached in _token
            else:
                module._client = None
            return {'saved': True, **self.connection(module)}

    def youtube_searches_left(self):
        """An estimate from the search cache: searches made since midnight Pacific time, when YouTube
        resets its limit. Searches made by other programs with the same key are not seen."""
        reset = datetime.now(ZoneInfo('America/Los_Angeles')).replace(hour=0, minute=0, second=0).timestamp()
        try:
            cache = json.loads(dorieplay.CACHE.read_text(encoding='utf-8'))
            used = sum(1 for key, entry in cache.items() if key.startswith('youtube|') and entry['at'] >= reset)
        except (OSError, ValueError, KeyError, TypeError):
            used = 0
        return max(0, YOUTUBE_SEARCHES_PER_DAY - used)

    def plan(self, name):
        with self.lock:
            return {**self.summary(name, plan := self.load(name)), 'tracks': plan['tracks']}

    def start_plan(self, url, to=None):
        """Search a playlist. Continues an unfinished plan of the same playlist, or opens the finished one."""
        url = (url or '').strip() if isinstance(url, str) else ''
        if not url or len(url) > 500:
            raise ApiError(400, 'Paste a playlist link.')
        try:
            source = platforms.detect(url)
            target = platforms.get(to) if to else next(p for p in platforms.PLATFORMS.values() if p is not source)
        except ValueError as error:
            raise ApiError(400, str(error)) from error
        if target is source:
            raise ApiError(400, 'Source and target are the same platform.')
        with self.lock:
            if self.busy():
                raise ApiError(409, 'Another task is still running.')
            name = None
            for summary in self.summaries():
                plan = self.load(summary['name'])
                if (plan.get('from') == source.NAME and plan.get('to') == target.NAME and not summary['applied']
                        and dorieplay.same_playlist(source, plan.get('source'), url)):
                    name = summary['name']
                    break
            if name is None:
                name = time.strftime('plan-%Y%m%d-%H%M%S')
            args = types.SimpleNamespace(playlist=url, to=target.NAME, plan=str(self.path(name)),
                                         force=False, no_cache=False)

            def work(job):
                def progress(done, total):
                    job['done'], job['total'] = done, total
                try:
                    dorieplay.make_plan(args, progress)
                except ValueError as error:
                    if 'already complete' in str(error):       # a finished plan is simply opened
                        return
                    if 'playlist changed' not in str(error):
                        raise
                    # The playlist was edited since that plan was made: keep the old plan, start a new one.
                    job['plan'] = time.strftime('plan-%Y%m%d-%H%M%S')
                    args.plan = str(self.path(job['plan']))
                    dorieplay.make_plan(args, progress)
            return self.run('plan', name, work)

    def edit(self, name, index, approved, match_id):
        """The user's decision for one song: take a candidate, or leave the song out."""
        with self.lock:
            if self.busy(name):
                raise ApiError(409, 'Wait until the running task is finished.')
            plan = self.load(name)
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(plan['tracks']):
                raise ApiError(400, 'There is no such song in the plan.')
            track = plan['tracks'][index]
            if approved:
                if match_id not in [c.get('id') for c in track.get('candidates') or []]:
                    raise ApiError(400, 'Choose one of the candidates of this song.')
                track['match_id'] = match_id
            track['approved'], track['decided'] = bool(approved), True
            dorieplay.save(self.path(name), plan)
            return {**self.summary(name, plan), 'track': track}

    def start_apply(self, name):
        with self.lock:
            if self.busy():
                raise ApiError(409, 'Another task is still running.')
            plan = self.load(name)
            approved = sum(1 for t in plan['tracks'] if t.get('approved') and t.get('match_id'))
            if not approved:
                raise ApiError(400, 'No approved songs in this plan yet.')
            path = self.path(name)

            def work(job):
                job['total'] = approved
                job['result'] = result = dorieplay.apply_plan(types.SimpleNamespace(plan=str(path)))
                saved = dorieplay.load(path)
                saved['target_url'] = result['url']
                dorieplay.save(path, saved)
            return self.run('apply', name, work)

    def start_check(self, name):
        """Before an export to YouTube Music: ask YouTube which chosen videos are gone (1 quota unit
        per 50). The answer lives only in the job, never in the plan file (YouTube's rules on API data)."""
        with self.lock:
            if self.busy():
                raise ApiError(409, 'Another task is still running.')
            plan = self.load(name)
            if plan.get('to') != 'youtube':
                raise ApiError(400, 'Only a plan to YouTube can be checked.')
            target = platforms.get('youtube')
            chosen = []
            for index, track in enumerate(plan['tracks']):
                if track.get('approved') and track.get('match_id'):
                    with contextlib.suppress(ValueError):   # an unreadable hand-edited link: apply reports it
                        chosen.append((index, dorieplay.chosen_id(track, target)))
            if not chosen:
                raise ApiError(400, 'No approved songs in this plan yet.')

            def work(job):
                job['total'] = len(chosen)
                gone = target.unavailable([video for _, video in chosen])
                job['done'] = len(chosen)
                job['result'] = {'unavailable': [{'index': i, 'id': v} for i, v in chosen if v in gone]}
                print(f'Checked {len(chosen)} videos, not available: {len(job["result"]["unavailable"])}.')
            return self.run('check', name, work)

    def run(self, kind, name, work):
        job = self.job = {'kind': kind, 'plan': name, 'state': 'running', 'done': 0, 'total': 0,
                          'lines': [], 'error': '', 'result': None}

        def target():
            try:
                with contextlib.redirect_stdout(JobOutput(job)):
                    work(job)
                job['state'] = 'done'
            except Exception as error:   # shown to the user; progress so far is in the plan file
                job['error'], job['state'] = f'{error}', 'error'
        threading.Thread(target=target, daemon=True).start()
        return job


def make_handler(app, port):
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'DoriePlay'

        def reply(self, status, body, kind='application/json'):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', f'{kind}; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            for header, value in SECURITY_HEADERS.items():
                self.send_header(header, value)
            self.end_headers()
            self.wfile.write(data)

        def handle_api(self, method):
            try:
                if self.headers.get('Host') not in hosts:
                    raise ApiError(403, 'Open the address printed by the program.')
                path = self.path.split('?')[0]
                if method == 'GET' and path in STATIC:
                    file, kind = STATIC[path]
                    page = (WEB / file).read_bytes().replace(b'__TOKEN__', app.token.encode())
                    return self.reply(200, page, kind)
                if not path.startswith('/api/'):
                    raise ApiError(404, 'Not found.')
                if not secrets.compare_digest(self.headers.get('X-Dorie-Token') or '', app.token):
                    raise ApiError(403, 'Reload the page.')
                body = {}
                if method == 'POST':
                    length = int(self.headers.get('Content-Length') or 0)
                    if length > 65536:
                        raise ApiError(413, 'Request is too large.')
                    try:
                        body = json.loads(self.rfile.read(length) or b'{}')
                    except ValueError as error:
                        raise ApiError(400, 'Invalid request.') from error
                    if not isinstance(body, dict):
                        raise ApiError(400, 'Invalid request.')
                parts = path[5:].split('/')
                if (method, parts) == ('GET', ['state']):
                    return self.reply(200, app.state())
                if (method, parts) == ('GET', ['job']):
                    return self.reply(200, {'job': app.job})
                if (method, parts) == ('POST', ['plans']):
                    return self.reply(200, app.start_plan(body.get('url'), body.get('to') or None))
                if method == 'GET' and len(parts) == 2 and parts[0] == 'plans':
                    return self.reply(200, app.plan(parts[1]))
                if method == 'POST' and len(parts) == 3 and parts[0] == 'plans' and parts[2] == 'track':
                    return self.reply(200, app.edit(parts[1], body.get('index'), body.get('approved'),
                                                    body.get('match_id')))
                if method == 'POST' and len(parts) == 3 and parts[0] == 'plans' and parts[2] == 'apply':
                    return self.reply(200, app.start_apply(parts[1]))
                if method == 'POST' and len(parts) == 3 and parts[0] == 'plans' and parts[2] == 'check':
                    return self.reply(200, app.start_check(parts[1]))
                if method == 'POST' and len(parts) == 2 and parts[0] == 'keys':
                    return self.reply(200, app.save_key(parts[1], body.get('value'), body.get('replace') is True))
                raise ApiError(404, 'Not found.')
            except ApiError as error:
                self.reply(error.status, {'error': str(error)})
            except Exception as error:
                self.reply(500, {'error': f'{type(error).__name__}: {error}'})

        def do_GET(self):
            self.handle_api('GET')

        def do_POST(self):
            self.handle_api('POST')

        def log_message(self, *args):
            pass

    return Handler


def serve(app, port=0):
    """(server, port). Port 0 lets the system pick a free one (tests)."""
    server = ThreadingHTTPServer(('127.0.0.1', port), BaseHTTPRequestHandler)
    server.RequestHandlerClass = make_handler(app, server.server_address[1])
    return server, server.server_address[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--demo', action='store_true', help='Invented playlist and fake services, no keys needed')
    parser.add_argument('--port', type=int, default=8765, help='Not 8888 or 8890: sign-in uses those')
    parser.add_argument('--no-browser', action='store_true', help='Do not open the page automatically')
    args = parser.parse_args()
    if args.demo:
        import demo
        platforms.PLATFORMS.clear()
        platforms.PLATFORMS.update(demo.platforms_for_demo())
        scratch = Path(tempfile.mkdtemp(prefix='dorieplay-demo-'))
        dorieplay.CACHE = scratch / 'cache.json'   # the demo neither reads nor fills the real cache
        app = App(scratch / 'plans', demo=True)
    else:
        app = App(ROOT / 'plans')
    try:
        server, port = serve(app, args.port)
    except OSError as error:
        parser.exit(1, f'Cannot start on port {args.port} ({error.strerror}). Is Dorie Play already running? '
                       'Try --port 8766.\n')
    url = f'http://127.0.0.1:{port}/'
    print(f'Dorie Play{" (demo)" if args.demo else ""}: {url}\nStop with Ctrl+C.', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
