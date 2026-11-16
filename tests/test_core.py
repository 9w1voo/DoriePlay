"""Core behaviour that decides what ends up in a playlist, checked against a fake platform.

Run from the project folder:  python -m unittest discover tests
No network and no keys are needed.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dorieplay  # noqa: E402
import paths  # noqa: E402
import platforms  # noqa: E402
from platforms import soundcloud, spotify, text, youtube  # noqa: E402


def track(source_id, **extra):
    return {'title': 'Song', 'artists': ['Artist'], 'duration_ms': 1000, 'source_id': source_id, **extra}


def approved(match_id, **extra):
    return track('s', approved=True, match_id=match_id, candidates=[], **extra)


class Fake:
    """A target platform that remembers what it was asked to do."""
    NAME, LABEL = 'fake', 'Fake'

    def __init__(self, existing=None, refuse=()):
        self.existing, self.refuse, self.created, self.added = existing or {}, refuse, [], []

    def create_playlist(self, name):
        self.created.append(name)
        return 'PL1'

    def playlist_counts(self, playlist_id):
        return dict(self.existing)

    def add_items(self, playlist_id, items):
        self.added += [item for item, _ in items if item not in self.refuse]
        return [(label, 'refused') for item, label in items if item in self.refuse]

    def playlist_url(self, playlist_id):
        return 'https://fake/' + playlist_id

    item_id = staticmethod(youtube.item_id)


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.plan = str(Path(folder.name) / 'x.json')
        self.addCleanup(platforms.PLATFORMS.pop, 'fake', None)
        cache = mock.patch.object(dorieplay, 'CACHE', Path(folder.name) / 'cache.json')
        cache.start()
        self.addCleanup(cache.stop)

    def quiet(self, function, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = function(*args)
        return result, out.getvalue()

    def args(self, **values):
        return types.SimpleNamespace(plan=self.plan, force=False, **values)


class Detect(Case):
    def test_platform_comes_from_the_host_not_from_words_in_the_link(self):
        self.assertIs(platforms.detect('https://soundcloud.com/dj/sets/spotify-hits'), soundcloud)
        self.assertIs(platforms.detect('https://www.youtube.com/playlist?list=PLspotifyABC'), youtube)
        self.assertIs(platforms.detect('https://open.spotify.com/intl-de/playlist/5abc?si=x'), spotify)
        self.assertIs(platforms.detect('https://on.soundcloud.com/AbC'), soundcloud)

    def test_a_list_of_songs_is_text_and_an_unknown_link_is_still_refused(self):
        self.assertIs(platforms.detect('Кино - Кукушка\nZemfira - Мачо'), text)
        self.assertIs(platforms.detect('https://open.spotify.com/track/1\nKino - Gruppa krovi'), text)
        self.assertIs(platforms.detect('Zemfira - Мачо'), text)
        with self.assertRaises(ValueError):
            platforms.detect('example.com/playlist/1')


class TextList(unittest.TestCase):
    def test_lines_become_songs(self):
        name, tracks = text.read_playlist('# my list\n\n1. Кино - Кукушка 6:38\n# a comment\n'
                                          'T-Fest, Иван Дорн - Прониклась мной (feat. Скриптонит)\nСтыдно\n')
        self.assertEqual(name, 'my list')
        self.assertEqual(text.read_playlist('A - B\n# not a name')[0], 'Song list')
        self.assertEqual([(t['artists'], t['title'], t['duration_ms']) for t in tracks], [
            (['Кино'], 'Кукушка', 398000),
            (['T-Fest', 'Иван Дорн', 'Скриптонит'], 'Прониклась мной', 0),
            ([], 'Стыдно', 0)])
        self.assertEqual(len({t['source_id'] for t in tracks}), 3)

    def test_the_same_list_pasted_again_is_the_same_playlist(self):
        self.assertEqual(text.playlist_key('A - B\n\nC - D'), text.playlist_key('  A - B\nC - D\n'))
        self.assertEqual(text.read_playlist('A - B')[1][0]['source_id'], text.read_playlist(' A  -  B ')[1][0]['source_id'])

    def test_song_links_are_refused_with_a_reason(self):
        with self.assertRaisesRegex(ValueError, 'Line 2 is a link'):
            text.read_playlist('Kino - Kukushka\nhttps://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC')


class Resume(Case):
    SPOTIFY = 'https://open.spotify.com/playlist/5abc'
    TRACKS = [track(str(n)) for n in range(3)]

    def saved(self, done, source, **extra):
        dorieplay.save(self.plan, {'name': 'n', 'source': source, 'from': 'spotify', 'to': 'youtube',
                                   'tracks': [{**t, 'approved': True, 'match_id': 'v', 'candidates': []}
                                              for t in self.TRACKS[:done]], **extra})

    def start(self, link, source=spotify, target=youtube, tracks=None):
        plan, _ = self.quiet(dorieplay.start_plan, self.args(playlist=link), source, target, 'n',
                             self.TRACKS if tracks is None else tracks)
        return plan

    def test_same_playlist_with_another_si_continues(self):
        self.saved(1, self.SPOTIFY + '?si=AAA')
        self.assertEqual(len(self.start(self.SPOTIFY + '?si=BBB')['tracks']), 1)

    def test_other_form_of_the_link_continues_when_searched_songs_match(self):
        dorieplay.save(self.plan, {'name': 'n', 'source': 'https://on.soundcloud.com/XyZ', 'from': 'soundcloud',
                                   'to': 'spotify', 'tracks': [{**self.TRACKS[0], 'approved': True,
                                                                 'match_id': 'v', 'candidates': []}]})
        plan = self.start('https://soundcloud.com/user/sets/name', source=soundcloud, target=spotify)
        self.assertEqual(len(plan['tracks']), 1)

    def test_empty_plan_of_another_playlist_is_refused(self):
        self.saved(0, self.SPOTIFY)
        with self.assertRaisesRegex(ValueError, 'another transfer'):
            self.start('https://open.spotify.com/playlist/9zzz')

    def test_changed_playlist_other_target_complete_and_applied_are_refused(self):
        self.saved(1, self.SPOTIFY)
        with self.assertRaisesRegex(ValueError, 'another transfer'):
            self.start(self.SPOTIFY, tracks=self.TRACKS[1:])
        with self.assertRaisesRegex(ValueError, 'another transfer'):
            self.start(self.SPOTIFY, target=soundcloud)
        self.saved(3, self.SPOTIFY)
        with self.assertRaisesRegex(ValueError, 'already complete'):
            self.start(self.SPOTIFY)
        self.saved(1, self.SPOTIFY, target_playlist_id='PL')
        with self.assertRaisesRegex(ValueError, 'already applied'):
            self.start(self.SPOTIFY)


class Apply(Case):
    def run_apply(self, fake, tracks, **extra):
        platforms.PLATFORMS['fake'] = fake
        dorieplay.save(self.plan, {'name': 'n', 'source': 'u', 'from': 'spotify', 'to': 'fake',
                                   'tracks': tracks, **extra})
        return self.quiet(dorieplay.apply_plan, self.args())[1]

    def test_rerun_adds_only_what_is_missing_and_keeps_source_duplicates(self):
        fake = Fake(existing={'aaaaaaaaaaa': 1})
        out = self.run_apply(fake, [approved('aaaaaaaaaaa'), approved('aaaaaaaaaaa'), approved('bbbbbbbbbbb')],
                             target_playlist_id='PL1')
        self.assertEqual(fake.added, ['aaaaaaaaaaa', 'bbbbbbbbbbb'])
        self.assertEqual(fake.created, [])
        self.assertIn('2 added, 1 were already there', out)

    def test_refused_song_is_reported_and_the_rest_is_added(self):
        fake = Fake(refuse={'deaddeaddea'})
        out = self.run_apply(fake, [approved('aaaaaaaaaaa'), approved('deaddeaddea'), approved('bbbbbbbbbbb')])
        self.assertEqual(fake.added, ['aaaaaaaaaaa', 'bbbbbbbbbbb'])
        self.assertIn('1 could not be added', out)
        self.assertEqual(dorieplay.load(self.plan)['target_playlist_id'], 'PL1')

    def test_link_in_match_id_becomes_an_id(self):
        fake = Fake()
        by_candidate = approved('https://x/perma')
        by_candidate['candidates'] = [{'id': 'cand1cand11', 'url': 'https://x/perma'}]
        self.run_apply(fake, [approved('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1'), by_candidate])
        self.assertEqual(fake.added, ['dQw4w9WgXcQ', 'cand1cand11'])

    def test_bad_plan_is_refused_before_a_playlist_is_created(self):
        fake = Fake()
        with self.assertRaisesRegex(ValueError, 'cannot read'):
            self.run_apply(fake, [approved('https://unknown.example/x')])
        fake.MAX_PLAYLIST_TRACKS = 1
        with self.assertRaisesRegex(ValueError, 'at most 1'):
            self.run_apply(fake, [approved('aaaaaaaaaaa'), approved('bbbbbbbbbbb')])
        self.assertEqual(fake.created, [])


class Clear(Case):
    def sc(self, **kwargs):
        fake = Fake(**kwargs)
        fake.NAME = 'soundcloud'
        patch = mock.patch.dict(platforms.PLATFORMS, {'soundcloud': fake})
        patch.start()
        self.addCleanup(patch.stop)
        return fake

    def candidate(self, cid):
        return {'id': cid, 'title': 'T', 'artist': 'A', 'seconds': 1, 'topic': False,
                'url': f'https://soundcloud.com/a/{cid}', 'score': 0.9, 'clean': True, 'uploader': 'u'}

    def put(self, source, target, tracks):
        dorieplay.save(self.plan, {'name': 'n', 'source': 'u', 'from': source, 'to': target, 'tracks': tracks})

    def apply(self):
        return self.quiet(dorieplay.apply_plan, self.args())[0]

    def test_a_clean_transfer_to_soundcloud_keeps_only_ids(self):
        fake = self.sc()
        one = {**approved('https://soundcloud.com/a/c1'), 'candidates': [self.candidate('c1'), self.candidate('c9')]}
        two = {**approved('c2'), 'candidates': [self.candidate('c2')]}
        three = {**track('s3'), 'approved': False, 'match_id': 'c3', 'candidates': [self.candidate('c3')]}
        self.put('spotify', 'soundcloud', [one, two, three])
        self.assertEqual(self.apply()['skipped'], [])
        plan = dorieplay.load(self.plan)
        first, second, third = plan['tracks']
        self.assertEqual(first['candidates'], [{'id': 'c1'}])
        self.assertEqual(first['match_id'], 'c1')
        self.assertEqual(second['candidates'], [{'id': 'c2'}])
        self.assertEqual(third['candidates'], [])
        self.assertIs(plan['soundcloud_cleared'], True)
        self.assertEqual(first['title'], 'Song')
        self.assertNotIn('soundcloud.com', Path(self.plan).read_text(encoding='utf-8'))
        fake.existing = {'c1': 1, 'c2': 1}
        fake.added.clear()
        result = self.apply()
        self.assertEqual(fake.added, [])
        self.assertEqual(result['skipped'], [])

    def test_from_soundcloud_titles_go_and_ids_stay(self):
        self.sc()
        platforms.PLATFORMS['fake'] = Fake()
        full = {'uploader': 'uploader-x', 'source_url': 'https://soundcloud.com/a/b', 'isrc': 'X', 'source_title': 'S'}
        self.put('soundcloud', 'fake', [{**approved('aaaaaaaaaaa'), **full, 'source_id': 'src1'}])
        self.apply()
        found = dorieplay.load(self.plan)['tracks'][0]
        for key in ('title', 'artists', 'source_title', 'isrc', 'uploader', 'source_url'):
            self.assertNotIn(key, found)
        text = Path(self.plan).read_text(encoding='utf-8')
        self.assertNotIn('soundcloud.com', text)
        self.assertNotIn('uploader-x', text)
        self.assertEqual((found['source_id'], found['duration_ms'], found['approved'], found['match_id']),
                         ('src1', 1000, True, 'aaaaaaaaaaa'))
        self.assertEqual(dorieplay.describe(found), found['source_id'])

    def output(self):
        out = self.quiet(dorieplay.apply_plan, self.args())[1]
        return out if isinstance(out, str) else str(out)

    def test_clean_transfer_warns_about_undecided_songs(self):
        self.sc()
        done = {**approved('c1'), 'candidates': [self.candidate('c1')]}
        waiting = {**track('s2'), 'approved': False, 'match_id': None, 'candidates': [self.candidate('c2')]}
        self.put('spotify', 'soundcloud', [done, waiting])
        out = self.output()
        self.assertIn('Songs that still needed a decision: 1.', out)
        self.assertIn('can no longer be chosen', out)
        self.assertNotIn('new plan', out)   # a new plan would make a second playlist

    def test_from_soundcloud_the_undecided_keep_their_candidates(self):
        self.sc()
        platforms.PLATFORMS['fake'] = Fake()
        done = {**approved('aaaaaaaaaaa'), 'candidates': [self.candidate('aaaaaaaaaaa')]}
        waiting = {**track('s2'), 'approved': False, 'match_id': None, 'candidates': [self.candidate('bbbbbbbbbbb')]}
        self.put('soundcloud', 'fake', [done, waiting])
        out = self.output()
        self.assertIn('Songs that still need a decision: 1.', out)
        self.assertIn('their candidates are still in the plan', out)
        self.assertNotIn('can no longer be chosen', out)
        self.assertEqual(len(dorieplay.load(self.plan)['tracks'][1]['candidates']), 1)

    def test_no_warning_when_all_are_decided_or_none_wait(self):
        self.sc()
        done = {**approved('c1'), 'candidates': [self.candidate('c1')]}
        passed = {**track('s2'), 'approved': False, 'decided': True, 'match_id': None,
                  'candidates': [self.candidate('c2')]}
        self.put('spotify', 'soundcloud', [done, passed])
        self.assertNotIn('still need', self.output())

    def test_no_warning_while_songs_are_skipped(self):
        self.sc(refuse={'bad'})
        good = {**approved('good'), 'candidates': [self.candidate('good')]}
        bad = {**approved('bad'), 'candidates': [self.candidate('bad')]}
        waiting = {**track('s3'), 'approved': False, 'match_id': None, 'candidates': [self.candidate('c3')]}
        self.put('spotify', 'soundcloud', [good, bad, waiting])
        self.assertNotIn('still need', self.output())

    def test_nothing_is_cleared_while_songs_are_left_out(self):
        fake = self.sc(refuse={'bad'})
        good = {**approved('good'), 'candidates': [self.candidate('good')]}
        bad = {**approved('bad'), 'candidates': [self.candidate('bad')]}
        self.put('spotify', 'soundcloud', [good, bad])
        self.assertEqual(len(self.apply()['skipped']), 1)
        plan = dorieplay.load(self.plan)
        self.assertNotIn('soundcloud_cleared', plan)
        self.assertEqual(plan['tracks'][0]['title'], 'Song')
        self.assertEqual(len(plan['tracks'][1]['candidates']), 1)
        plan['tracks'][1]['match_id'] = 'other'
        dorieplay.save(self.plan, plan)
        fake.existing = {'good': 1}
        self.assertEqual(self.apply()['skipped'], [])
        self.assertIs(dorieplay.load(self.plan)['soundcloud_cleared'], True)

    def test_review_survives_a_cleared_plan(self):
        self.sc()
        platforms.PLATFORMS['fake'] = Fake()
        gone = {'source_id': 'src9', 'duration_ms': 2000, 'approved': False, 'match_id': None, 'candidates': []}
        trimmed = {**gone, 'source_id': 'src8', 'candidates': [{'id': 'x'}]}
        self.put('soundcloud', 'fake', [{**approved('aaaaaaaaaaa'), 'source_id': 'src1'}, gone, trimmed])
        self.apply()
        _, out = self.quiet(dorieplay.review_plan, self.args())
        self.assertIn('src9', out)
        self.assertIn('SoundCloud details were removed after the transfer, as SoundCloud’s rules require.', out)
        self.assertNotIn('nothing found', out)
        self.assertNotIn('None', out)
        self.assertNotIn('To take a song', out)
        self.assertIn('—', out)

    def test_urls_with_spaces_and_unapproved_urls_become_ids(self):
        fake = self.sc()
        spaced = {**approved('https://soundcloud.com/a/c1 \n'), 'candidates': [self.candidate('c1')]}
        pending = {**track('s2'), 'approved': False, 'match_id': 'https://soundcloud.com/a/c2',
                   'candidates': [self.candidate('c2')]}
        self.put('spotify', 'soundcloud', [spaced, pending])
        self.apply()
        found = dorieplay.load(self.plan)['tracks']
        self.assertEqual([t['match_id'] for t in found], ['c1', 'c2'])
        self.assertNotIn('soundcloud.com', Path(self.plan).read_text(encoding='utf-8'))
        fake.existing, fake.added = {'c1': 1}, []
        self.assertEqual(self.apply()['skipped'], [])
        self.assertEqual(fake.added, [])

    def test_a_cleared_plan_is_not_resumed_without_force(self):
        source = types.SimpleNamespace(NAME='spotify', LABEL='Spotify',
                                       read_playlist=lambda url: ('n', [track('1')]))
        target = self.sc()
        target.search = lambda t: []
        self.put('spotify', 'soundcloud', [])
        plan = dorieplay.load(self.plan)
        plan['soundcloud_cleared'] = True
        dorieplay.save(self.plan, plan)
        args = lambda force: types.SimpleNamespace(plan=self.plan, playlist='u', to='soundcloud', no_cache=True, force=force)
        with mock.patch.object(platforms, 'detect', return_value=source):
            with self.assertRaisesRegex(ValueError, 'lost its SoundCloud details'):
                self.quiet(dorieplay.make_plan, args(False))
            self.quiet(dorieplay.make_plan, args(True))
        self.assertNotIn('soundcloud_cleared', dorieplay.load(self.plan))

    def test_other_transfers_are_untouched(self):
        platforms.PLATFORMS['fake'] = Fake()
        one = {**approved('aaaaaaaaaaa'), 'candidates': [{'id': 'aaaaaaaaaaa', 'url': 'https://y/1', 'title': 'T'}]}
        self.put('spotify', 'fake', [one])
        before = json.loads(Path(self.plan).read_text(encoding='utf-8'))
        self.apply()
        after = json.loads(Path(self.plan).read_text(encoding='utf-8'))
        after.pop('target_playlist_id')
        self.assertEqual(before, after)


class PlanFile(Case):
    def test_broken_json_gets_a_readable_error(self):
        Path(self.plan).write_text('{"name": “x”}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'line 1'):
            dorieplay.load(self.plan)

    def test_plan_of_the_first_version_is_converted(self):
        dorieplay.save(self.plan, {'name': 'o', 'source': 'u', 'youtube_playlist_id': 'PLold', 'tracks': [{
            'title': 'a', 'artists': ['b'], 'duration_ms': 1, 'spotify_id': 's1', 'video_id': 'dQw4w9WgXcQ',
            'approved': True, 'candidates': [{'video_id': 'dQw4w9WgXcQ', 'title': 't', 'channel': 'X - Topic',
                                              'seconds': 1, 'score': 1, 'clean': 't'}]}]})
        plan = dorieplay.load(self.plan)
        self.assertEqual((plan['from'], plan['to'], plan['target_playlist_id']), ('spotify', 'youtube', 'PLold'))
        self.assertEqual(plan['tracks'][0]['match_id'], 'dQw4w9WgXcQ')
        self.assertTrue(plan['tracks'][0]['candidates'][0]['topic'])

    def test_review_survives_a_hand_edited_plan(self):
        dorieplay.save(self.plan, {'name': 'n', 'source': 'u', 'from': 'spotify', 'to': 'youtube', 'tracks': [
            {'title': 'a', 'artists': ['b'], 'duration_ms': None, 'source_id': '1', 'approved': False}]})
        self.assertIn('nothing found', self.quiet(dorieplay.review_plan, self.args())[1])


PROJECT = Path(__file__).resolve().parent.parent
# Every data path, printed as its folder by a fresh Python that has DORIEPLAY_HOME set.
DATA_PATHS = """
import json, dorieplay, server
from platforms import soundcloud, spotify, youtube
files = [spotify.CLIENT_FILE, spotify.TOKEN_FILE, youtube.CLIENT_FILE, youtube.TOKEN_FILE,
         soundcloud.TOKEN_FILE, dorieplay.CACHE, server.PLANS, dorieplay.SETTINGS]
print(json.dumps({'data': [str(f.parent) for f in files],
                  'web': str(server.WEB)}))
"""


class DataFolder(unittest.TestCase):
    def test_dorieplay_home_wins_then_the_frozen_app_folder_then_the_code_folder(self):
        with mock.patch.dict(os.environ, {'DORIEPLAY_HOME': '~/x'}):
            self.assertEqual(paths.data_folder(), Path('~/x').expanduser())
        frozen = mock.patch.object(sys, 'frozen', True, create=True)
        home = mock.patch.object(Path, 'home', lambda: Path('/h'))
        with mock.patch.dict(os.environ, {'DORIEPLAY_HOME': ''}), frozen, home:
            with mock.patch.object(sys, 'platform', 'darwin'):
                self.assertEqual(paths.data_folder(), Path('/h/Library/Application Support/Dorie Play'))
            with mock.patch.object(sys, 'platform', 'linux'):
                self.assertEqual(paths.data_folder(), Path('/h/.local/share/Dorie Play'))
            with mock.patch.object(sys, 'platform', 'win32'), \
                    mock.patch.dict(os.environ, {'APPDATA': 'C:/Users/i/AppData/Roaming'}):
                self.assertEqual(paths.data_folder(), Path('C:/Users/i/AppData/Roaming') / 'Dorie Play')
        with mock.patch.dict(os.environ, {'DORIEPLAY_HOME': ''}):
            self.assertEqual(paths.data_folder(), Path(paths.__file__).resolve().parent)

    def test_every_data_file_follows_dorieplay_home(self):
        with tempfile.TemporaryDirectory() as home:
            done = subprocess.run([sys.executable, '-c', DATA_PATHS], cwd=PROJECT, timeout=60, capture_output=True,
                                  text=True, env={**os.environ, 'DORIEPLAY_HOME': home, 'DORIEPLAY_SONGBASE': ''})
            self.assertEqual(done.returncode, 0, done.stderr)
            found = json.loads(done.stdout)
            self.assertEqual(found['data'], [home] * len(found['data']))
            self.assertEqual(len(found['data']), 8)
            self.assertEqual(found['web'], str(PROJECT / 'web'))

    def test_ensure_makes_a_private_folder_and_leaves_an_existing_one_alone(self):
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch) / 'a' / 'Dorie Play'
            self.assertEqual(paths.ensure(folder), folder)
            self.assertTrue(folder.is_dir())
            if os.name != 'nt':   # Windows has no such modes
                self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            existing = Path(scratch) / 'code'
            existing.mkdir()
            existing.chmod(0o755)
            paths.ensure(existing)
            if os.name != 'nt':
                self.assertEqual(existing.stat().st_mode & 0o777, 0o755)


if __name__ == '__main__':
    unittest.main()
