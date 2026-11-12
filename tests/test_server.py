"""The local web server, driven over real HTTP against the demo (fake) services.

Run from the project folder:  python -m unittest discover tests
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import demo  # noqa: E402
import dorieplay  # noqa: E402
import platforms  # noqa: E402
import server  # noqa: E402

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

    def test_bad_requests_get_a_plain_answer(self):
        self.assertEqual(self.call('/api/plans', {'url': 'https://example.com/x'})[0], 400)
        self.assertEqual(self.call('/api/plans', {'url': LINK, 'to': 'spotify'})[0], 400)
        self.assertEqual(self.call('/api/plans/nothing-here')[0], 404)
        self.assertEqual(self.call('/api/plans/nothing-here/apply', {})[0], 404)


if __name__ == '__main__':
    unittest.main()
