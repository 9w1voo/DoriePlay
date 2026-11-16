"""The local web server, driven over real HTTP against the demo (fake) services.

Run from the project folder:  python -m unittest discover tests
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import demo  # noqa: E402
import dorieplay  # noqa: E402
import platforms  # noqa: E402
import server  # noqa: E402
import songbase  # noqa: E402
import version  # noqa: E402
from platforms import soundcloud, spotify, youtube  # noqa: E402

LINK = 'https://open.spotify.com/playlist/demo'


class ServerCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        real = dict(platforms.PLATFORMS)
        self.addCleanup(lambda: (platforms.PLATFORMS.clear(), platforms.PLATFORMS.update(real)))
        platforms.PLATFORMS.clear()
        platforms.PLATFORMS.update(demo.platforms_for_demo(delay=0))
        environment = mock.patch.dict(os.environ, {'DORIEPLAY_SONGBASE': ''})   # never the real song base
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(setattr, dorieplay, 'CACHE', dorieplay.CACHE)
        dorieplay.CACHE = Path(folder.name) / 'cache.json'
        self.addCleanup(setattr, dorieplay, 'SETTINGS', dorieplay.SETTINGS)
        dorieplay.SETTINGS = Path(folder.name) / 'settings.json'
        self.addCleanup(setattr, server.JobOutput, 'echo', True)
        server.JobOutput.echo = False
        self.app = server.App(Path(folder.name) / 'plans', demo=True)
        self.http, self.port = server.serve(self.app)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def call(self, path, body=None, token=True, host=None):
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                         data=None if body is None else json.dumps(body).encode(),
                                         method='GET' if body is None else 'POST')
        if token:
            request.add_header('X-Dorie-Token', self.app.token if token is True else token)
        if host:
            request.add_header('Host', host)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as error:
            with error:
                raw, status = error.read(), error.code
        return status, (json.loads(raw) if path.startswith('/api/') else raw.decode())

    def wait(self):
        for _ in range(400):
            job = self.call('/api/state')[1]['job']
            if job['state'] != 'running':
                return job
            time.sleep(0.01)
        self.fail('the job did not finish')

    def planned(self):
        status, job = self.call('/api/plans', {'url': LINK, 'to': 'youtube'})
        self.assertEqual(status, 200)
        self.assertEqual(self.wait()['state'], 'done')
        return job['plan']


class Security(ServerCase):
    def test_page_carries_the_token_and_api_refuses_requests_without_it(self):
        status, page = self.call('/', token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.app.token, page)
        self.assertEqual(self.call('/logo.svg', token=False)[0], 200)
        self.assertEqual(self.call('/api/state', token=False)[0], 403)
        self.assertEqual(self.call('/api/state', token='wrong')[0], 403)
        self.assertEqual(self.call('/api/plans', {'url': LINK}, token=False)[0], 403)
        self.assertEqual(self.app.summaries(), [])

    def test_the_service_marks_are_served_as_is(self):
        for path, kind in (('soundcloud.png', 'image/png'), ('spotify-white.svg', 'image/svg+xml'),
                           ('spotify-black.svg', 'image/svg+xml'), ('logo.svg', 'image/svg+xml')):
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/{path}', timeout=10) as response:
                self.assertEqual(response.status, 200, path)
                self.assertEqual(response.headers['Content-Type'], kind, path)
                self.assertEqual(response.read(), (server.WEB / path).read_bytes(), path)

    def test_images_are_never_touched_by_the_token_substitution(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'x.png').write_bytes(b'x__TOKEN__y')
            with mock.patch.object(server, 'WEB', Path(folder)), \
                    mock.patch.dict(server.STATIC, {'/x.png': ('x.png', 'image/png')}):
                with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/x.png', timeout=10) as response:
                    self.assertEqual(response.read(), b'x__TOKEN__y')
                    self.assertNotIn('charset', response.headers['Content-Type'])

    def test_other_host_names_are_refused(self):
        self.assertEqual(self.call('/api/state', host='evil.example')[0], 403)
        self.assertEqual(self.call('/', token=False, host=f'evil.example:{self.port}')[0], 403)

    def test_plan_names_cannot_leave_the_plans_folder(self):
        for name in ('..%2F..%2Fsecret', '.spotify-token', 'a.b', ''):
            self.assertIn(self.call(f'/api/plans/{name}')[0], (400, 404), name)
        with self.assertRaises(server.ApiError):
            self.app.path('../x')


class Flow(ServerCase):
    def test_soundcloud_shows_no_key_in_the_demo(self):
        shown = {p['name']: p for p in self.call('/api/state')[1]['platforms']}
        self.assertIs(shown['soundcloud']['no_key'], True)
        self.assertIs(shown['spotify']['no_key'], False)

    def test_a_job_survives_a_missing_or_narrow_console(self):
        class Narrow:   # a Windows console in cp1252 cannot print every song title
            def write(self, text):
                raise UnicodeEncodeError('cp1252', '→', 0, 1, 'x')

            def flush(self):
                pass

        server.JobOutput.echo = True
        with mock.patch.object(sys, '__stdout__', None):   # a windowed app on Windows has no console
            self.planned()
        with mock.patch.object(sys, '__stdout__', Narrow()):
            self.planned()

    def test_a_sign_in_line_becomes_a_link_and_the_next_line_clears_it(self):
        job = {'kind': 'plan', 'lines': [], 'done': 0, 'signin': None}
        out = server.JobOutput(job)
        out.echo = False
        out.write('Sign in to Spotify in your browser: https://accounts.spotify.com/auth')
        out.write('orize?a=1')
        self.assertIsNone(job['signin'])   # no end of line yet
        out.write('\n')
        self.assertEqual(job['signin'], {'service': 'Spotify', 'url': 'https://accounts.spotify.com/authorize?a=1'})
        self.assertEqual(job['lines'], [])
        out.write('Signed in to Spotify.\n')
        self.assertIsNone(job['signin'])
        self.assertEqual(job['lines'], ['Signed in to Spotify.'])
        with contextlib.redirect_stdout(out):
            print('Sign in to SoundCloud in your browser: https://secure.soundcloud.com/authorize?b=2')
        self.assertEqual(job['signin']['service'], 'SoundCloud')
        out.write('Sign in to Spotify in your browser: https://evil.example/\n')
        self.assertIsNone(job['signin'])
        self.assertEqual(job['lines'][-1], 'Sign in to Spotify in your browser: https://evil.example/')

    def test_the_song_base_switch_is_saved(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('DORIEPLAY_SONGBASE')
            self.assertEqual(self.call('/api/settings', {'songbase': False}),
                             (200, {'songbase': {'on': False, 'locked': False}}))
            self.assertEqual(json.loads(dorieplay.SETTINGS.read_text(encoding='utf-8')), {'songbase': False})
            self.assertEqual(self.call('/api/state')[1]['songbase'], {'on': False, 'locked': False})
            self.assertEqual(self.call('/api/settings', {'songbase': 'no'})[0], 400)
        self.assertEqual(self.call('/api/settings', {'songbase': True})[0], 409)   # '' from setUp: locked

    def test_state_works_without_a_time_zone_database(self):   # Windows without tzdata
        with mock.patch.object(server, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('America/Los_Angeles')):
            status, state = self.call('/api/state')
        self.assertEqual(status, 200)
        self.assertIsInstance(state['youtube_searches_left'], int)

    def test_a_second_copy_cannot_open_the_same_port(self):
        # Passes on macOS even without ExclusiveBind; it proves the fix only on Windows (CI, plan 2).
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(OSError):
            server.serve(server.App(Path(folder) / 'plans', demo=True), self.port)

    def test_search_review_and_transfer(self):
        name = self.planned()
        plan = self.call(f'/api/plans/{name}')[1]
        self.assertEqual((plan['total'], plan['from'], plan['to']), (len(demo.SONGS), 'spotify', 'youtube'))
        ask = next(i for i, t in enumerate(plan['tracks']) if not t['approved'] and t['candidates'])
        choice = plan['tracks'][ask]['candidates'][1]['id']

        self.assertEqual(self.call(f'/api/plans/{name}/track', {'index': ask, 'approved': True,
                                                                'match_id': 'not-a-candidate'})[0], 400)
        self.assertEqual(self.call(f'/api/plans/{name}/track', {'index': 999, 'approved': False})[0], 400)
        status, saved = self.call(f'/api/plans/{name}/track', {'index': ask, 'approved': True, 'match_id': choice})
        self.assertEqual((status, saved['track']['match_id'], saved['track']['decided']), (200, choice, True))
        self.assertEqual(saved['approved'], plan['approved'] + 1)
        self.assertEqual(self.call(f'/api/plans/{name}/track', {'index': 0, 'approved': False})[1]['approved'],
                         plan['approved'])

        self.assertEqual(self.call(f'/api/plans/{name}/apply', {})[0], 200)
        job = self.wait()
        self.assertEqual((job['state'], job['result']['added'], job['result']['already']),
                         ('done', plan['approved'], 0))
        added = platforms.PLATFORMS['youtube'].playlists['demo-playlist-1']
        self.assertIn(choice, added)
        self.assertEqual(len(added), plan['approved'])

        self.call(f'/api/plans/{name}/apply', {})                      # a second run adds nothing twice
        self.assertEqual(self.wait()['result']['added'], 0)
        self.assertEqual(len(added), plan['approved'])

    def test_same_playlist_opens_the_finished_plan_and_a_transferred_one_starts_anew(self):
        first = self.planned()
        self.assertEqual(self.planned(), first)
        self.assertEqual(len(self.call('/api/state')[1]['plans']), 1)
        saved = dorieplay.load(self.app.path(first))
        saved['target_playlist_id'] = 'x'
        dorieplay.save(self.app.path(first), saved)
        time.sleep(1.05)   # plan names carry the second they were made in
        self.assertNotEqual(self.planned(), first)

    def test_check_names_the_songs_whose_video_is_gone_and_stores_nothing(self):
        name = self.planned()
        plan = self.call(f'/api/plans/{name}')[1]
        chosen = [(i, t['match_id']) for i, t in enumerate(plan['tracks']) if t['approved'] and t['match_id']]
        gone_index, gone_id = chosen[1]
        platforms.PLATFORMS['youtube'].unavailable = lambda ids: {gone_id}
        before = self.app.path(name).read_bytes()

        self.assertEqual(self.call(f'/api/plans/{name}/check', {})[0], 200)
        job = self.wait()
        self.assertEqual((job['kind'], job['state'], job['total']), ('check', 'done', len(chosen)))
        self.assertEqual(job['result']['unavailable'], [{'index': gone_index, 'id': gone_id}])
        self.assertEqual(self.app.path(name).read_bytes(), before)   # YouTube's answer is not written down

        saved = dorieplay.load(self.app.path(name))
        saved['to'] = 'soundcloud'
        dorieplay.save(self.app.path(name), saved)
        self.assertEqual(self.call(f'/api/plans/{name}/check', {})[0], 400)

    def test_a_pasted_list_of_songs_is_searched_like_a_playlist(self):
        songs = '\n'.join(f'Артист {n} - Пісня номер {n}' for n in range(1, 401))   # longer than a link may be
        status, job = self.call('/api/plans', {'url': songs, 'to': 'youtube'})
        self.assertEqual(status, 200)
        self.assertEqual(self.wait()['state'], 'done')
        plan = self.call(f"/api/plans/{job['plan']}")[1]
        self.assertEqual((plan['from'], plan['to'], len(plan['tracks'])), ('text', 'youtube', 400))
        self.assertEqual(plan['tracks'][0]['artists'], ['Артист 1'])
        self.assertTrue(plan['tracks'][0]['approved'])
        self.assertEqual(self.call('/api/plans', {'url': 'Song - A\nhttps://open.spotify.com/track/1', 'to': 'youtube'})[0], 200)
        self.assertIn('Line 2 is a link', self.wait()['error'])
        status, answer = self.call('/api/plans', {'url': 'A - B\n' * 20000, 'to': 'youtube'})
        self.assertEqual(status, 400)
        self.assertIn('too long', answer['error'])

    def test_sign_out_goes_through_the_api(self):
        status, answer = self.call('/api/signout/soundcloud', {})
        self.assertEqual((status, answer['error']), (400, 'The demo has nothing to sign out of.'))
        self.assertEqual(self.call('/api/signout/soundcloud', {}, token=False)[0], 403)

    def test_a_cleared_plan_is_not_reused(self):
        status, job = self.call('/api/plans', {'url': LINK, 'to': 'soundcloud'})
        self.assertEqual(status, 200)
        self.assertEqual(self.wait()['state'], 'done')
        first = job['plan']
        saved = dorieplay.load(self.app.path(first))
        saved['soundcloud_cleared'] = True
        dorieplay.save(self.app.path(first), saved)
        shown = self.call('/api/state')[1]['plans'][0]
        self.assertEqual((shown['cleared'], shown['applied']), (True, False))
        time.sleep(1.05)   # plan names carry the second they were made in
        job = self.call('/api/plans', {'url': LINK, 'to': 'soundcloud'})[1]
        self.assertNotEqual(job['plan'], first)
        self.assertEqual(self.wait()['state'], 'done')

    def test_a_transfer_to_soundcloud_marks_the_plan_cleared(self):
        name = self.call('/api/plans', {'url': LINK, 'to': 'soundcloud'})[1]['plan']
        self.assertEqual(self.wait()['state'], 'done')
        self.assertEqual(self.call(f'/api/plans/{name}/apply', {})[0], 200)
        self.assertEqual(self.wait()['state'], 'done')
        self.assertIs(self.call('/api/state')[1]['plans'][0]['cleared'], True)

    def test_bad_requests_get_a_plain_answer(self):
        self.assertEqual(self.call('/api/plans', {'url': 'https://example.com/x'})[0], 400)
        self.assertEqual(self.call('/api/plans', {'url': LINK, 'to': 'spotify'})[0], 400)
        self.assertEqual(self.call('/api/plans/nothing-here')[0], 404)
        self.assertEqual(self.call('/api/plans/nothing-here/apply', {})[0], 404)


class Version(ServerCase):
    def requests_stub(self, get):
        stub = types.ModuleType('requests')
        stub.get = get
        patch = mock.patch.dict(sys.modules, {'requests': stub})
        patch.start()
        self.addCleanup(patch.stop)

    def test_only_a_higher_version_counts(self):
        self.assertTrue(version.newer('1.0.1', '1.0.0'))
        self.assertTrue(version.newer('1.10.0', '1.9.9'))
        self.assertFalse(version.newer('1.0.0', '1.0.0'))
        self.assertFalse(version.newer('0.9.9', '1.0.0'))
        self.assertFalse(version.newer(None))

    def test_the_latest_version_is_read_from_the_release_tag(self):
        answers = []

        def get(url, timeout, headers):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return types.SimpleNamespace(json=lambda: answer, raise_for_status=lambda: None)

        self.requests_stub(get)
        answers[:] = [{'tag_name': 'v1.2.0'}, {'tag_name': 'banana'}, OSError('offline'), ['not', 'a', 'dict']]
        self.assertEqual(version.latest(), '1.2.0')
        self.assertIsNone(version.latest())
        self.assertIsNone(version.latest())
        self.assertIsNone(version.latest())

    def test_the_demo_never_asks_and_the_page_gets_the_version(self):
        self.requests_stub(lambda *args, **kwargs: self.fail('the demo asked GitHub'))
        version.check(self.app)
        state = self.call('/api/state')[1]
        self.assertEqual((state['version'], state['update']), ('1.0.0', None))


class DemoApp(unittest.TestCase):
    def test_the_demo_switches_the_song_base_off(self):
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('DORIEPLAY_SONGBASE', None)
        real = dict(platforms.PLATFORMS)
        self.addCleanup(lambda: (platforms.PLATFORMS.clear(), platforms.PLATFORMS.update(real)))
        for name in ('CACHE', 'SETTINGS'):
            self.addCleanup(setattr, dorieplay, name, getattr(dorieplay, name))
        self.addCleanup(setattr, server.JobOutput, 'echo', True)
        server.JobOutput.echo = False
        with mock.patch.object(songbase.urllib.request, 'urlopen') as ask:
            app = server.make_app(True)
            self.addCleanup(shutil.rmtree, app.plans.parent, True)
            self.assertEqual(os.environ['DORIEPLAY_SONGBASE'], '')
            self.assertEqual(dorieplay.songbase_url(), '')
            platforms.PLATFORMS.update(demo.platforms_for_demo(delay=0))
            app.start_plan(LINK, 'youtube')
            for _ in range(200):
                if not app.busy():
                    break
                time.sleep(0.05)
            self.assertEqual(app.job['state'], 'done')
            ask.assert_not_called()


class Keys(unittest.TestCase):
    """The set-up page saves keys next to the program; here, in a temporary folder instead."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        patches = [mock.patch.dict(os.environ), mock.patch.object(spotify, 'ROOT', self.dir),
                   mock.patch.object(spotify, '_client', None), mock.patch.object(youtube, '_client', None),
                   mock.patch.object(soundcloud, '_token', None)]
        for module, key, token in ((spotify, 'spotify-client.json', '.spotify-token'),
                                   (youtube, 'google-client.json', '.youtube-token.json')):
            patches.append(mock.patch.object(module, 'CLIENT_FILE', self.dir / key))
            patches.append(mock.patch.object(module, 'TOKEN_FILE', self.dir / token))
        patches.append(mock.patch.object(soundcloud, 'TOKEN_FILE', self.dir / '.soundcloud-token.json'))
        patches.append(mock.patch.object(dorieplay, 'CACHE', self.dir / 'cache.json'))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop('SPOTIPY_CLIENT_ID', None)
        self.app = server.App(self.dir / 'plans')

    def refused(self, status, *args):
        with self.assertRaises(server.ApiError) as caught:
            self.app.save_key(*args)
        self.assertEqual(caught.exception.status, status)
        return str(caught.exception)

    def test_spotify_id_is_saved_private_and_not_overwritten_unasked(self):
        self.assertFalse(self.app.connection(spotify)['key'])
        self.assertTrue(self.app.save_key('spotify', ' ' + 'AB12' * 8 + ' ')['key'])
        self.assertEqual(json.loads(spotify.CLIENT_FILE.read_text()), {'client_id': 'ab12' * 8})
        if os.name != 'nt':   # Windows keeps no such modes
            self.assertEqual(spotify.CLIENT_FILE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(spotify.client_id(), 'ab12' * 8)
        self.refused(409, 'spotify', 'cd34' * 8)
        self.app.save_key('spotify', 'cd34' * 8, True)
        self.assertEqual(spotify.client_id(), 'cd34' * 8)

    def test_a_spotify_key_that_the_environment_would_hide_is_refused(self):
        os.environ['SPOTIPY_CLIENT_ID'] = 'from-the-environment'
        self.assertIn('SPOTIPY_CLIENT_ID', self.refused(409, 'spotify', 'ab12' * 8, True))
        self.assertFalse(spotify.CLIENT_FILE.exists())
        self.assertEqual(spotify.client_id(), 'from-the-environment')

    def test_a_new_key_drops_the_sign_in_of_the_old_one(self):
        youtube.TOKEN_FILE.write_text('{"client_id": "old"}')
        youtube._client = object()
        installed = {'client_id': 'new.apps.googleusercontent.com', 'client_secret': 's', 'token_uri': 'https://x'}
        state = self.app.save_key('youtube', json.dumps({'installed': installed}))
        self.assertEqual((state['key'], state['signed_in'], youtube._client), (True, False, None))
        self.assertFalse(youtube.TOKEN_FILE.exists())

    def test_wrong_shapes_are_refused_and_never_quoted(self):
        secret = 'my-secret-value-123'
        for name, value in (('spotify', secret), ('youtube', json.dumps({'web': {'client_id': secret}})),
                            ('youtube', secret)):
            self.assertNotIn(secret, self.refused(400, name, value), name)
        self.assertFalse(any(m.CLIENT_FILE.exists() for m in (spotify, youtube)))
        self.refused(400, 'spotify', '')
        self.refused(404, 'dropbox', 'ab12' * 8)
        demo_app = server.App(self.dir / 'demo-plans', demo=True)
        with self.assertRaises(server.ApiError):
            demo_app.save_key('spotify', 'ab12' * 8)
        self.assertFalse(spotify.CLIENT_FILE.exists())

    def test_soundcloud_needs_no_key(self):
        self.assertEqual(soundcloud.TOKEN_FILE.parent, self.dir)
        self.assertEqual(self.app.connection(soundcloud), {'key': True, 'no_key': True, 'signed_in': False})

    def test_google_desktop_client_is_saved_and_soundcloud_takes_no_key(self):
        installed = {'client_id': '123-abc.apps.googleusercontent.com', 'client_secret': 'GOCSPX-x',
                     'token_uri': 'https://oauth2.googleapis.com/token'}
        self.app.save_key('youtube', json.dumps({'installed': installed}))
        self.assertEqual(json.loads(youtube.CLIENT_FILE.read_text()), {'installed': installed})
        self.assertIn('SoundCloud needs no key.', self.refused(404, 'soundcloud', '{}'))
        self.assertTrue(self.app.connection(youtube)['key'])

    def test_sign_out_revokes_forgets_and_clears_saved_plans(self):
        track = {'title': 'T', 'artists': ['A'], 'duration_ms': 1000, 'source_id': 's1', 'approved': True,
                 'match_id': 'm1', 'candidates': [{'id': 'm1', 'title': 'T', 'artist': 'A', 'url': 'u'}]}
        self.app.plans.mkdir(parents=True, exist_ok=True)
        (self.app.plans / 'broken.json').write_text('{not json')
        calls = []

        def stub(answer):
            def disconnect():
                calls.append(1)
                soundcloud.TOKEN_FILE.unlink(missing_ok=True)
                return answer
            return disconnect

        for answer in (True, False):
            dorieplay.CACHE.write_text(json.dumps({'soundcloud|x|1|': {'at': time.time(), 'candidates': []}}))
            dorieplay.save(self.app.path('sc'), {'name': 'N', 'source': LINK, 'from': 'spotify', 'to': 'soundcloud',
                                                 'tracks': [json.loads(json.dumps(track))]})
            dorieplay.save(self.app.path('yt'), {'name': 'O', 'source': LINK, 'from': 'spotify', 'to': 'youtube',
                                                 'tracks': [json.loads(json.dumps(track))]})
            soundcloud.TOKEN_FILE.write_text('{"access_token": "dummy"}')
            untouched = self.app.path('yt').read_bytes()
            calls.clear()
            with mock.patch.object(soundcloud, 'disconnect', stub(answer)):
                state = self.app.sign_out('soundcloud')
            self.assertEqual((state['signed_out'], state['revoked'], state['signed_in']), (True, answer, False))
            self.assertEqual(len(calls), 1)
            cleared = dorieplay.load(self.app.path('sc'))
            self.assertIs(cleared['soundcloud_cleared'], True)
            self.assertEqual(cleared['tracks'][0]['candidates'], [{'id': 'm1'}])
            self.assertEqual(self.app.path('yt').read_bytes(), untouched)
            self.assertNotIn('soundcloud|', dorieplay.CACHE.read_text())
        calls.clear()
        self.app.job = {'state': 'running', 'plan': 'x'}
        with mock.patch.object(soundcloud, 'disconnect', stub(True)):
            with self.assertRaises(server.ApiError) as caught:
                self.app.sign_out('soundcloud')
            self.assertEqual((caught.exception.status, str(caught.exception)),
                             (409, 'Wait until the running task is finished.'))
            self.app.job = None
            with self.assertRaises(server.ApiError) as caught:
                self.app.sign_out('spotify')
            self.assertEqual((caught.exception.status, str(caught.exception)),
                             (404, 'There is no such service.'))
            with self.assertRaises(server.ApiError) as caught:
                server.App(self.dir / 'demo-plans', demo=True).sign_out('soundcloud')
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(calls, [])

    def test_sign_out_leaves_cleared_plans_alone_and_survives_a_cache_error(self):
        self.app.plans.mkdir(parents=True, exist_ok=True)
        dorieplay.save(self.app.path('done'), {'name': 'N', 'source': LINK, 'from': 'spotify', 'to': 'soundcloud',
                                               'soundcloud_cleared': True, 'tracks': []})
        os.utime(self.app.path('done'), (1_000_000, 1_000_000))
        with mock.patch.object(soundcloud, 'disconnect', lambda: True), \
                mock.patch.object(dorieplay, 'read_cache', side_effect=OSError('disk')):
            self.assertIs(self.app.sign_out('soundcloud')['signed_out'], True)
        self.assertEqual(self.app.path('done').stat().st_mtime, 1_000_000)


if __name__ == '__main__':
    unittest.main()
