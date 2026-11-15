"""The three service modules against stand-ins for their APIs: what is asked, and how refusals,
retries and limits are handled. No network, no keys; the real key and token files are never touched.

Run from the project folder:  python -m unittest discover tests
"""
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import platforms  # noqa: E402
from platforms import deezer, soundcloud, spotify, youtube  # noqa: E402


class SpotifyException(Exception):
    def __init__(self, http_status, code=-1, msg=''):
        super().__init__(msg)
        self.http_status = http_status


class HttpError(Exception):
    def __init__(self, resp, content):
        super().__init__(content)
        self.resp, self.content = resp, content


def http_error(status, reason=''):
    return HttpError(types.SimpleNamespace(status=status), json.dumps({'error': {'errors': [{'reason': reason}]}}))


def stub_modules():
    """The libraries' error classes, so the tests need neither spotipy nor google-api-python-client."""
    spotipy_errors = types.ModuleType('spotipy.exceptions')
    spotipy_errors.SpotifyException = SpotifyException
    google_errors = types.ModuleType('googleapiclient.errors')
    google_errors.HttpError = HttpError
    return {'spotipy': types.ModuleType('spotipy'), 'spotipy.exceptions': spotipy_errors,
            'googleapiclient': types.ModuleType('googleapiclient'), 'googleapiclient.errors': google_errors,
            'requests': types.ModuleType('requests')}


class PlatformCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        here = Path(folder.name)
        patches = [mock.patch.dict(sys.modules, stub_modules()),
                   # Keys and tokens point into an empty temporary folder: a mistake cannot touch the real ones.
                   mock.patch.object(spotify, 'ROOT', here),
                   mock.patch.object(spotify, 'CLIENT_FILE', here / 'spotify-client.json'),
                   mock.patch.object(spotify, 'TOKEN_FILE', here / '.spotify-token'),
                   mock.patch.object(youtube, 'CLIENT_FILE', here / 'google-client.json'),
                   mock.patch.object(youtube, 'TOKEN_FILE', here / '.youtube-token.json'),
                   mock.patch.object(soundcloud, 'CLIENT_FILE', here / 'soundcloud-client.json'),
                   mock.patch.object(soundcloud, 'TOKEN_FILE', here / '.soundcloud-token.json'),
                   mock.patch.object(soundcloud, '_token', None),
                   mock.patch.object(youtube.time, 'sleep', lambda seconds: self.slept.append(seconds)),
                   mock.patch('sys.stdout', new_callable=io.StringIO)]   # the "Added: ..." lines
        self.slept = []
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


# --- Spotify ---------------------------------------------------------------------------------------

class SpotifyApi:
    def __init__(self, pages=None, reject=()):
        self.pages, self.reject, self.gets, self.posts = pages or {}, set(reject), [], []

    def playlist(self, playlist_id, fields):
        return {'name': 'Evening mix'}

    def _get(self, url, **params):
        self.gets.append(url)
        if url == 'playlists/forbidden/items':
            raise SpotifyException(403)
        return self.pages[url]

    def next(self, page):
        return self.pages[page['next']]

    def _post(self, url, payload):
        self.posts.append((url, payload))
        if url.endswith('/items') and any(uri.split(':')[-1] in self.reject for uri in payload['uris']):
            raise SpotifyException(400)
        return {'id': 'new-playlist'}


def spotify_row(track_id, name, field='item', **extra):
    return {field: {'id': track_id, 'name': name, 'type': 'track', 'duration_ms': 200000,
                    'artists': [{'name': 'Artist'}], 'external_ids': {'isrc': 'ISRC' + track_id[:3]}, **extra}}


class Spotify(PlatformCase):
    def use(self, api):
        patch = mock.patch.object(spotify, '_client', api)
        patch.start()
        self.addCleanup(patch.stop)
        return api

    def test_reads_items_pages_with_the_track_fallback_and_skips_local_files(self):
        api = self.use(SpotifyApi({
            'playlists/abc/items': {'items': [spotify_row('t1' * 11, 'One'), {'item': None},
                                              spotify_row('t2' * 11, 'Two', field='track')], 'next': 'page2'},
            'page2': {'items': [spotify_row('t3' * 11, 'Local', is_local=True), spotify_row('t4' * 11, 'Four')]}}))
        name, tracks = spotify.read_playlist('https://open.spotify.com/playlist/abc?si=xyz')
        self.assertEqual(name, 'Evening mix')
        self.assertEqual([t['title'] for t in tracks], ['One', 'Two', 'Four'])
        self.assertEqual(tracks[0]['isrc'], 'ISRCt1t')
        self.assertEqual(api.gets, ['playlists/abc/items'])

    def test_generated_and_foreign_playlists_get_a_plain_explanation(self):
        api = self.use(SpotifyApi())
        with self.assertRaisesRegex(ValueError, 'generated by Spotify'):
            spotify.read_playlist('https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M')
        self.assertEqual(api.gets, [])   # refused before asking Spotify
        with self.assertRaisesRegex(ValueError, 'own or collaborate'):
            spotify.read_playlist('spotify:playlist:forbidden')

    def test_creates_a_private_playlist_through_me_playlists(self):
        api = self.use(SpotifyApi())
        self.assertEqual(spotify.create_playlist('Mix'), 'new-playlist')
        url, payload = api.posts[0]
        self.assertEqual((url, payload['public']), ('me/playlists', False))

    def test_one_refused_id_does_not_stop_the_others(self):
        good, bad, other = 'a' * 22, 'b' * 22, 'c' * 22
        api = self.use(SpotifyApi(reject={bad}))
        skipped = spotify.add_items('pl', [(good, 'Good'), ('not-an-id', 'Typo'), (bad, 'Bad'), (other, 'Other')])
        self.assertEqual([label for label, _ in skipped], ['Typo', 'Bad'])
        added = [p['uris'] for url, p in api.posts if len(p['uris']) == 1 and p['uris'][0].split(':')[-1] != bad]
        self.assertEqual(added, [[f'spotify:track:{good}'], [f'spotify:track:{other}']])
        self.assertTrue(all(url == 'playlists/pl/items' for url, _ in api.posts))


# --- YouTube ---------------------------------------------------------------------------------------

class Request:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def execute(self):
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class YouTubeApi:
    def __init__(self, inserts=None, listing=None, videos=None):
        self.script, self.listing, self.known = inserts or {}, listing, videos or {}
        self.inserted = []

    def playlistItems(self):
        return self

    def playlists(self):
        return types.SimpleNamespace(list=lambda **p: Request({'items': [{'snippet': {'title': 'List'}}]}))

    def videos(self):
        return types.SimpleNamespace(list=lambda **p: Request(
            {'items': [self.known[i] for i in p['id'].split(',') if i in self.known]}))

    def insert(self, part, body):
        video = body['snippet']['resourceId']['videoId']
        self.inserted.append(video)
        return self.script.setdefault(video, Request({}))

    def list(self, **params):
        return self.listing

    def list_next(self, request, response):
        return None


def video(video_id, title, channel='Artist'):
    return {'id': video_id, 'snippet': {'title': title, 'channelTitle': channel},
            'contentDetails': {'duration': 'PT3M20S'}}


class YouTube(PlatformCase):
    def use(self, api):
        patch = mock.patch.object(youtube, '_client', api)
        patch.start()
        self.addCleanup(patch.stop)
        return api

    def test_busy_answers_are_retried_and_deleted_videos_are_skipped(self):
        busy, gone = 'busyVideo01', 'goneVideo01'
        api = self.use(YouTubeApi(inserts={busy: Request(http_error(409), http_error(409), {}),
                                           gone: Request(http_error(404, 'videoNotFound'))}))
        skipped = youtube.add_items('pl', [(busy, 'Busy'), ('short', 'Typo'), (gone, 'Gone'), ('okVideo0001', 'Ok')])
        self.assertEqual(skipped, [('Typo', '"short" is not a YouTube video id'),
                                   ('Gone', 'the video was deleted or made private')])
        self.assertEqual(api.inserted, [busy, gone, 'okVideo0001'])
        self.assertEqual(self.slept, [2, 4, 1, 1, 1])   # back-off on 409, then a pause after every song

    def test_used_up_quota_stops_with_the_reset_time(self):
        self.use(YouTubeApi(inserts={'quotaVideo1': Request(http_error(403, 'quotaExceeded'))}))
        with self.assertRaisesRegex(RuntimeError, '09:00 in Amsterdam'):
            youtube.add_items('pl', [('quotaVideo1', 'Song')])

    def test_counting_a_fresh_playlist_waits_out_the_first_404(self):
        listing = Request(http_error(404), {'items': [{'contentDetails': {'videoId': 'v1'}},
                                                      {'contentDetails': {'videoId': 'v1'}}]})
        self.use(YouTubeApi(listing=listing))
        self.assertEqual(youtube.playlist_counts('pl'), {'v1': 2})
        self.assertEqual(self.slept, [5])

    def test_reading_skips_deleted_videos_and_refuses_mixes(self):
        listing = Request({'items': [{'contentDetails': {'videoId': v}} for v in ('a1', 'deleted', 'b2')]})
        self.use(YouTubeApi(listing=listing, videos={'a1': video('a1', 'Okean Elzy - Obiimy'),
                                                     'b2': video('b2', 'Second song', 'Band - Topic')}))
        name, tracks = youtube.read_playlist('https://www.youtube.com/playlist?list=PLabc')
        self.assertEqual((name, [t['source_id'] for t in tracks]), ('List', ['a1', 'b2']))
        self.assertEqual(tracks[0]['duration_ms'], 200000)
        with self.assertRaisesRegex(ValueError, 'Mix'):
            youtube.read_playlist('https://www.youtube.com/watch?v=x&list=RDabc')


# --- SoundCloud ------------------------------------------------------------------------------------

class SoundCloudApi:
    """GET of a playlist's tracks and PUT of its whole track list; `lose` tracks vanish on each PUT."""

    def __init__(self, tracks=(), lose=0):
        self.tracks, self.lose, self.puts = list(tracks), lose, []

    def __call__(self, method, path, params=None, body=None, retry_auth=True):
        if method == 'GET':
            return {'collection': [{'urn': urn} for urn in self.tracks]}
        wanted = [t['urn'] for t in body['playlist']['tracks']]
        self.puts.append(wanted)
        self.tracks = wanted[:len(wanted) - self.lose]
        return {}


def urns(start, count):
    return [f'soundcloud:tracks:{n}' for n in range(start, start + count)]


class SoundCloud(PlatformCase):
    def use(self, api):
        patch = mock.patch.object(soundcloud, '_request', api)
        patch.start()
        self.addCleanup(patch.stop)
        return api

    def test_sends_the_current_list_plus_new_tracks_in_batches_of_100(self):
        api = self.use(SoundCloudApi(tracks=urns(1, 2)))
        new = urns(100, 150)
        skipped = soundcloud.add_items('soundcloud:playlists:9', [(u, u) for u in new] + [('bad', 'Typo')])
        self.assertEqual([label for label, _ in skipped], ['Typo'])
        self.assertEqual([len(p) for p in api.puts], [102, 152])
        self.assertEqual(api.puts[1], urns(1, 2) + new)   # order kept, nothing lost between batches

    def test_a_count_that_does_not_add_up_stops_with_an_error(self):
        self.use(SoundCloudApi(tracks=urns(1, 2), lose=1))
        with self.assertRaisesRegex(RuntimeError, 'expected 3'):
            soundcloud.add_items('soundcloud:playlists:9', [(urns(50, 1)[0], 'Song')])

    def test_the_500_track_limit_is_checked_before_anything_is_sent(self):
        api = self.use(SoundCloudApi(tracks=urns(1, 499)))
        with self.assertRaisesRegex(ValueError, 'at most 500'):
            soundcloud.add_items('soundcloud:playlists:9', [(u, u) for u in urns(1000, 2)])
        self.assertEqual(api.puts, [])

    def test_the_token_is_never_sent_outside_soundcloud(self):
        for url in ('https://evil.example/x', 'http://api.soundcloud.com/x', 'https://soundcloud.com.evil.example/'):
            with self.assertRaisesRegex(RuntimeError, 'unexpected address'):
                soundcloud._request('GET', url)


# --- Deezer (source only, open API) ----------------------------------------------------------------

class Deezer(unittest.TestCase):
    def answers(self, pages):
        """urlopen stand-in: each URL gets its list of JSON answers in turn; asked URLs are recorded."""
        self.asked = []

        def get(url, allow_redirects, timeout):
            self.asked.append(url)
            data = pages[url].pop(0)
            return types.SimpleNamespace(status_code=200, headers={}, json=lambda: data)
        for patch in (mock.patch.dict(sys.modules, {'requests': types.SimpleNamespace(get=get)}),
                      mock.patch.object(deezer.time, 'sleep', lambda seconds: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_reads_every_page_with_isrc_and_follows_only_deezer_api_links(self):
        api = 'https://api.deezer.com/playlist/123'
        self.answers({
            api: [{'title': 'Road trip'}],
            api + '/tracks?limit=100': [{'error': {'code': 4, 'message': 'Quota limit exceeded'}},   # waits, asks again
                                        {'data': [{'id': 1, 'type': 'track', 'title': 'Hey Jude (Remastered 2015)',
                                                   'duration': 429, 'isrc': 'GBUM71505902', 'artist': {'name': 'The Beatles'}}],
                                         'next': api + '/tracks?limit=100&index=100'}],
            api + '/tracks?limit=100&index=100': [{'data': [
                {'id': 2, 'title': 'мачо', 'duration': 260, 'artist': {'name': 'Земфира'}},
                {'id': 3, 'type': 'episode', 'title': 'A podcast'}], 'next': 'https://evil.example/next'}]})
        name, tracks = deezer.read_playlist('https://www.deezer.com/en/playlist/123?utm_source=x')
        self.assertEqual(name, 'Road trip')
        self.assertEqual(tracks, [
            {'title': 'Hey Jude (Remastered 2015)', 'artists': ['The Beatles'], 'duration_ms': 429000,
             'source_id': '1', 'isrc': 'GBUM71505902'},
            {'title': 'мачо', 'artists': ['Земфира'], 'duration_ms': 260000, 'source_id': '2'}])
        self.assertNotIn('https://evil.example/next', self.asked)

    def test_private_or_missing_playlist_gets_a_clear_error(self):
        self.answers({'https://api.deezer.com/playlist/9': [{'error': {'type': 'DataException', 'code': 800}}]})
        with self.assertRaisesRegex(ValueError, 'private'):
            deezer.read_playlist('deezer.com/playlist/9')

    def test_links(self):
        self.assertEqual(deezer.playlist_key('https://www.deezer.com/nl/playlist/908622995?utm_campaign=x'), '908622995')
        self.assertIs(platforms.detect('https://www.deezer.com/playlist/1'), deezer)
        self.assertIs(platforms.detect('https://soundcloud.com/x/sets/deezer-hits'), soundcloud)   # by host
        with mock.patch.object(deezer, '_redirect', lambda url: 'https://www.deezer.com/en/playlist/42?host=1'):
            self.assertEqual(deezer.playlist_id('https://link.deezer.com/s/30abc'), '42')
        with mock.patch.object(deezer, '_redirect', lambda url: 'https://evil.example/playlist/42'):
            with self.assertRaisesRegex(ValueError, 'not a playlist'):
                deezer.playlist_id('https://link.deezer.com/s/30abc')
        with self.assertRaisesRegex(ValueError, 'not a playlist'):
            deezer.playlist_id('https://www.deezer.com/en/album/302127')


if __name__ == '__main__':
    unittest.main()
