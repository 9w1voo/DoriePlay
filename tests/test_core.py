"""Core behaviour that decides what ends up in a playlist, checked against a fake platform.

Run from the project folder:  python -m unittest discover tests
No network and no keys are needed.
"""
import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dorieplay  # noqa: E402
import platforms  # noqa: E402
from platforms import soundcloud, spotify, youtube  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
