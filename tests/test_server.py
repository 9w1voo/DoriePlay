"""The local web server, driven over real HTTP against the demo (fake) services.

Run from the project folder:  python -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import demo  # noqa: E402
import dorieplay  # noqa: E402
import platforms  # noqa: E402
import server  # noqa: E402
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
        self.addCleanup(setattr, dorieplay, 'CACHE', dorieplay.CACHE)
        dorieplay.CACHE = Path(folder.name) / 'cache.json'
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

    def test_other_host_names_are_refused(self):
        self.assertEqual(self.call('/api/state', host='evil.example')[0], 403)
        self.assertEqual(self.call('/', token=False, host=f'evil.example:{self.port}')[0], 403)

    def test_plan_names_cannot_leave_the_plans_folder(self):
        for name in ('..%2F..%2Fsecret', '.spotify-token', 'a.b', ''):
            self.assertIn(self.call(f'/api/plans/{name}')[0], (400, 404), name)
        with self.assertRaises(server.ApiError):
            self.app.path('../x')


class Flow(ServerCase):
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

    def test_bad_requests_get_a_plain_answer(self):
        self.assertEqual(self.call('/api/plans', {'url': 'https://example.com/x'})[0], 400)
        self.assertEqual(self.call('/api/plans', {'url': LINK, 'to': 'spotify'})[0], 400)
        self.assertEqual(self.call('/api/plans/nothing-here')[0], 404)
        self.assertEqual(self.call('/api/plans/nothing-here/apply', {})[0], 404)


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
                                   (youtube, 'google-client.json', '.youtube-token.json'),
                                   (soundcloud, 'soundcloud-client.json', '.soundcloud-token.json')):
            patches.append(mock.patch.object(module, 'CLIENT_FILE', self.dir / key))
            patches.append(mock.patch.object(module, 'TOKEN_FILE', self.dir / token))
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
        soundcloud.TOKEN_FILE.write_text('{}')
        soundcloud._token = {'access_token': 'old'}
        youtube._client = object()
        installed = {'client_id': 'new.apps.googleusercontent.com', 'client_secret': 's', 'token_uri': 'https://x'}
        state = self.app.save_key('youtube', json.dumps({'installed': installed}))
        self.assertEqual((state['key'], state['signed_in'], youtube._client), (True, False, None))
        self.assertFalse(youtube.TOKEN_FILE.exists())
        self.app.save_key('soundcloud', json.dumps({'client_id': 'abcdEFGH1234', 'client_secret': 'secret-5678-x'}))
        self.assertEqual((soundcloud.TOKEN_FILE.exists(), soundcloud._token), (False, None))
        self.assertTrue(callable(soundcloud._client))   # the function is left alone

    def test_wrong_shapes_are_refused_and_never_quoted(self):
        secret = 'my-secret-value-123'
        for name, value in (('spotify', secret), ('youtube', json.dumps({'web': {'client_id': secret}})),
                            ('youtube', secret), ('soundcloud', json.dumps({'client_id': secret}))):
            self.assertNotIn(secret, self.refused(400, name, value), name)
        self.assertFalse(any(m.CLIENT_FILE.exists() for m in (spotify, youtube, soundcloud)))
        self.refused(400, 'spotify', '')
        self.refused(404, 'dropbox', 'ab12' * 8)
        demo_app = server.App(self.dir / 'demo-plans', demo=True)
        with self.assertRaises(server.ApiError):
            demo_app.save_key('spotify', 'ab12' * 8)
        self.assertFalse(spotify.CLIENT_FILE.exists())

    def test_google_desktop_client_and_soundcloud_app_are_saved(self):
        installed = {'client_id': '123-abc.apps.googleusercontent.com', 'client_secret': 'GOCSPX-x',
                     'token_uri': 'https://oauth2.googleapis.com/token'}
        self.app.save_key('youtube', json.dumps({'installed': installed}))
        self.assertEqual(json.loads(youtube.CLIENT_FILE.read_text()), {'installed': installed})
        self.app.save_key('soundcloud', json.dumps({'client_id': 'abcdEFGH1234', 'client_secret': 'secret-5678-x'}))
        self.assertEqual(json.loads(soundcloud.CLIENT_FILE.read_text())['redirect_uri'], soundcloud.DEFAULT_REDIRECT)
        self.assertTrue(all(self.app.connection(m)['key'] for m in (youtube, soundcloud)))


if __name__ == '__main__':
    unittest.main()
