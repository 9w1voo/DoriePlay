"""The song base: built from a Discogs dump, answering over HTTP, and used by Dorie Play before YouTube.
Run from the project folder:  python -m unittest discover tests   (no network: a local server only)"""
import contextlib
import gzip
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dorieplay  # noqa: E402
import platforms  # noqa: E402
import songbase  # noqa: E402

DUMP = """<masters>
<master id="1"><artists><artist><id>1</id><name>Земфира</name></artist></artists><title>Земфира</title><videos>
 <video src="https://www.youtube.com/watch?v=aaaaaaaaaaa" duration="245" embed="true"><title>Земфира — Мачо (Official Video)</title></video>
 <video src="https://www.youtube.com/watch?v=bbbbbbbbbbb" duration="260" embed="true"><title>Земфира — Мачо (Live)</title></video>
 <video src="https://vimeo.com/123" duration="10"><title>not YouTube</title></video></videos></master>
<master id="2"><artists><artist><name>Various</name></artist></artists><title>Hits</title><videos>
 <video src="https://youtu.be/ccccccccccc" duration="200"><title>Someone - Song</title></video></videos></master>
<master id="3"><artists><artist><name>Kino (2)</name></artist></artists><title>Nothing linked</title><videos/></master>
</masters>"""
MACHO = {'title': 'Мачо', 'artists': ['Zemfira'], 'duration_ms': 245000}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        dump = self.folder / 'discogs_20261101_masters.xml.gz'
        dump.write_bytes(gzip.compress(DUMP.encode()))
        self.db = self.folder / 'songbase.sqlite'
        self.rows = songbase.build(dump, self.db)
        songbase._down.clear()
        environment = mock.patch.dict(os.environ, {'DORIEPLAY_SONGBASE': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def start_server(self):
        httpd = songbase.serve(self.db, port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f'http://127.0.0.1:{httpd.server_address[1]}'

    def post(self, url, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method='POST'), timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())


class Build(Base):
    def test_only_youtube_videos_of_named_artists_are_kept(self):
        self.assertEqual(self.rows, 2)   # Various is a compilation, Vimeo is not YouTube
        with contextlib.closing(songbase.sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT DISTINCT artist_key, artist FROM video').fetchall(), [('zemfira', 'Земфира')])
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key = 'built_from'").fetchone()[0],
                             'discogs_20261101_masters.xml.gz')
        self.assertEqual([p.name for p in self.folder.iterdir() if 'building' in p.name], [])


class Server(Base):
    def test_answers_ranked_candidates_and_logs_nothing(self):
        url = self.start_server()
        # Python's "unclosed database" warning (a request thread's connection collected) is not a log line.
        with contextlib.redirect_stderr(io.StringIO()) as log, warnings.catch_warnings():
            warnings.simplefilter('ignore', ResourceWarning)
            status, answer = self.post(url + '/v1/match', MACHO)
            self.assertEqual(status, 200)
            self.assertEqual([(c['id'], c['source']) for c in answer['candidates']],
                             [('aaaaaaaaaaa', 'discogs'), ('bbbbbbbbbbb', 'discogs')])
            self.assertEqual(self.post(url + '/v1/match', b'{"artists": []}')[0], 400)
            self.assertEqual(self.post(url + '/v1/match', b'x' * 5000)[0], 413)
            self.assertEqual(self.post(url + '/other', MACHO)[0], 404)
            with urllib.request.urlopen(url + '/v1/health', timeout=5) as response:
                self.assertEqual(json.loads(response.read())['videos'], 2)
        self.assertEqual(log.getvalue(), '')   # no addresses or song names written anywhere

    def test_a_broken_request_writes_nothing(self):
        url = self.start_server()
        with contextlib.redirect_stderr(io.StringIO()) as log, warnings.catch_warnings():
            warnings.simplefilter('ignore', ResourceWarning)
            request = urllib.request.Request(url + '/v1/match', data=b'{}', method='POST')
            request.add_header('Content-Length', 'abc')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            with caught.exception:
                self.assertEqual(caught.exception.code, 400)
            with mock.patch.object(songbase, 'match', side_effect=RuntimeError):
                with self.assertRaises(OSError):
                    urllib.request.urlopen(urllib.request.Request(
                        url + '/v1/match', data=json.dumps(MACHO).encode(), method='POST'), timeout=5)
            with urllib.request.urlopen(url + '/v1/health', timeout=5) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(log.getvalue(), '')   # not even a traceback with the client address

    def test_each_address_is_limited_and_the_proxy_address_counts(self):
        httpd = songbase.serve(self.db, port=0, limit=2)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        url = f'http://127.0.0.1:{httpd.server_address[1]}/v1/match'

        def ask(forwarded=None):
            request = urllib.request.Request(url, data=json.dumps(MACHO).encode(), method='POST')
            if forwarded:
                request.add_header('X-Forwarded-For', forwarded)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                with error:
                    return error.code

        self.assertEqual([ask('1.1.1.1'), ask('1.1.1.1'), ask('1.1.1.1')], [200, 200, 429])
        self.assertEqual(ask('6.6.6.6, 2.2.2.2'), 200)   # a client-made first entry does not dodge the limit
        self.assertEqual(ask('1.1.1.1, 2.2.2.2'), 200)
        self.assertEqual(ask('9.9.9.9, 2.2.2.2'), 429)
        self.assertEqual([ask(), ask(), ask()], [200, 200, 429])   # direct, on this machine


class Client(Base):
    def test_a_confident_answer_skips_the_search_and_the_reply_is_checked(self):
        url = self.start_server()
        found = songbase.lookup(MACHO, url)
        self.assertEqual(songbase.confident(MACHO, found)[0]['id'], 'aaaaaaaaaaa')
        self.assertEqual(songbase.confident({'title': 'Другая', 'artists': ['Zemfira'], 'duration_ms': 1000}, found), [])
        bad = io.BytesIO(json.dumps({'candidates': [
            {'id': 'short', 'seconds': 1}, {'id': 'ddddddddddd', 'seconds': '9'},
            {'id': 'eeeeeeeeeee', 'seconds': 200, 'title': 'x' * 999, 'url': 'https://evil.example/'}]}).encode())
        with mock.patch.object(songbase.urllib.request, 'urlopen', return_value=contextlib.nullcontext(bad)):
            self.assertEqual([(c['id'], len(c['title']), c['url']) for c in songbase.lookup(MACHO, url)],
                             [('eeeeeeeeeee', 300, 'https://www.youtube.com/watch?v=eeeeeeeeeee')])

    def test_the_live_base_is_on_unless_switched_off(self):
        settings = self.folder / 'settings.json'
        with mock.patch.object(dorieplay, 'SETTINGS', settings):
            with mock.patch.dict(os.environ):
                os.environ.pop('DORIEPLAY_SONGBASE', None)
                self.assertEqual(dorieplay.songbase_url(), 'https://base.dorieplay.com')
                settings.write_text(json.dumps({'songbase': False}), encoding='utf-8')
                self.assertEqual(dorieplay.songbase_url(), '')
                for broken in ('not json', '[false]'):
                    settings.write_text(broken, encoding='utf-8')
                    self.assertEqual(dorieplay.songbase_url(), dorieplay.SONGBASE)
            settings.write_text(json.dumps({'songbase': True}), encoding='utf-8')
            for value in ('', 'http://127.0.0.1:9'):   # the variable wins over the file
                with mock.patch.dict(os.environ, {'DORIEPLAY_SONGBASE': value}):
                    self.assertEqual(dorieplay.songbase_url(), value)

    def test_off_by_default_and_a_dead_base_is_asked_once(self):
        with mock.patch.object(songbase.urllib.request, 'urlopen') as call:
            self.assertEqual(songbase.lookup(MACHO), [])
            call.assert_not_called()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(songbase.lookup(MACHO, 'http://127.0.0.1:9'), [])
            with mock.patch.object(songbase.urllib.request, 'urlopen') as call:
                self.assertEqual(songbase.lookup(MACHO, 'http://127.0.0.1:9'), [])
                call.assert_not_called()
        self.assertEqual(out.getvalue().count('did not answer'), 1)


class Plan(Base):
    def plan(self, dead=()):
        """Plans two pasted songs to a fake YouTube on which the ids in `dead` no longer play."""
        searched, checked = [], []
        fake = types.SimpleNamespace(NAME='youtube', LABEL='YouTube', search=lambda t: searched.append(t['title']) or [],
                                     unavailable=lambda ids: checked.append(ids) or set(dead) & set(ids))
        cache = mock.patch.object(dorieplay, 'CACHE', self.folder / 'cache.json')
        cache.start()
        self.addCleanup(cache.stop)
        args = types.SimpleNamespace(playlist='Zemfira - Мачо 4:05\nNobody - Unknown song 3:00', to='youtube',
                                     plan=str(self.folder / 'plan.json'), force=False, no_cache=False)
        with mock.patch.dict(platforms.PLATFORMS, {'youtube': fake}), \
                mock.patch.dict(os.environ, {'DORIEPLAY_SONGBASE': self.start_server()}), \
                contextlib.redirect_stdout(io.StringIO()):
            dorieplay.make_plan(args)
        return dorieplay.load(args.plan)['tracks'], searched, checked

    def test_songs_from_the_base_cost_no_youtube_search(self):
        tracks, searched, checked = self.plan()
        self.assertEqual(searched, ['Unknown song'])
        self.assertEqual(checked, [['aaaaaaaaaaa', 'bbbbbbbbbbb']])   # only songs the base knows are checked
        self.assertEqual((tracks[0]['approved'], tracks[0]['match_id'], tracks[0]['candidates'][0]['source']),
                         (True, 'aaaaaaaaaaa', 'discogs'))
        self.assertFalse(tracks[1]['approved'])

    def test_switching_the_base_off_stops_it_for_the_rest_of_the_search(self):
        asked = []

        def lookup(track, url=None):   # the person unticks the switch while the first song is looked up
            asked.append(url)
            dorieplay.save_settings({'songbase': False})
            return []

        fake = types.SimpleNamespace(NAME='youtube', LABEL='YouTube', search=lambda t: [], unavailable=lambda ids: set())
        args = types.SimpleNamespace(playlist='Zemfira - Мачо 4:05\nNobody - Unknown song 3:00', to='youtube',
                                     plan=str(self.folder / 'plan.json'), force=False, no_cache=False)
        with mock.patch.dict(platforms.PLATFORMS, {'youtube': fake}), mock.patch.dict(os.environ), \
                mock.patch.object(dorieplay, 'CACHE', self.folder / 'cache.json'), \
                mock.patch.object(dorieplay, 'SETTINGS', self.folder / 'settings.json'), \
                mock.patch.object(songbase, 'lookup', lookup), contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop('DORIEPLAY_SONGBASE', None)
            dorieplay.make_plan(args)
        self.assertEqual(asked, [dorieplay.SONGBASE, ''])

    def test_a_dead_base_video_falls_back_to_a_youtube_search(self):
        tracks, searched, _ = self.plan(dead={'aaaaaaaaaaa'})
        self.assertEqual(searched, ['Мачо', 'Unknown song'])
        self.assertNotIn('discogs', [c.get('source') for t in tracks for c in t['candidates']])
