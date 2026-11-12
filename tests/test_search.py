"""How songs are searched and scored. Run from the project folder:  python -m unittest discover tests"""
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
from matching import core_title, gather, is_confident, queries, rank  # noqa: E402
from platforms import spotify, youtube  # noqa: E402


def song(title, artists, seconds, **extra):
    return {'title': title, 'artists': artists, 'duration_ms': seconds * 1000, 'source_id': title, **extra}


def found(title, artist, seconds, topic=False, **extra):
    return {'id': f'{artist}/{title}/{seconds}', 'title': title, 'artist': artist, 'seconds': seconds,
            'topic': topic, 'url': '', **extra}


def best(track, *candidates):
    ranked = rank(track, list(candidates))
    return ranked[0], is_confident(ranked)


class Titles(unittest.TestCase):
    def test_neutral_labels_and_featured_artists_are_dropped(self):
        self.assertEqual(core_title('Sicko Mode (feat. Drake)'), 'Sicko Mode')
        self.assertEqual(core_title('Heroes - 2017 Remaster'), 'Heroes')
        self.assertEqual(core_title('Bohemian Rhapsody - Remastered 2011'), 'Bohemian Rhapsody')
        self.assertEqual(core_title('Song (feat. A) [Mono]'), 'Song')
        self.assertEqual(core_title('Song feat. A', trailing_feat=True), 'Song')

    def test_real_versions_and_upload_titles_are_kept(self):
        for title in ('Arsenal (Live)', 'One More Time - Radio Edit', 'Song (Acoustic)', 'Song - Remix',
                      'Mono', 'Remastered'):
            self.assertEqual(core_title(title), title)
        self.assertEqual(core_title('A feat. B - Song'), 'A feat. B - Song')


class Scoring(unittest.TestCase):
    def test_featured_artist_in_the_source_title_does_not_block_approval(self):
        track = song('Sicko Mode (feat. Drake)', ['Travis Scott', 'Drake', 'Swae Lee'], 312)
        audio = found('SICKO MODE', 'Travis Scott - Topic', 313, topic=True)
        top, sure = best(track, audio,
                         found('Travis Scott - SICKO MODE ft. Drake (Official Video)', 'Travis Scott', 314))
        self.assertTrue(sure)   # label audio and official video of the same length: one recording
        self.assertEqual(top['clean'], 'sicko mode')
        # A video cut of another length is a different recording: the user decides, as before.
        top, sure = best(track, audio,
                         found('Travis Scott - SICKO MODE ft. Drake (Official Video)', 'Travis Scott', 323))
        self.assertFalse(sure)
        self.assertEqual(top['artist'], 'Travis Scott - Topic')

    def test_upload_naming_only_the_lead_artist_is_approved(self):
        _, sure = best(song('Прониклась мной', ['T-Fest', 'Иван Дорн', 'Скриптонит'], 200),
                       found('T-Fest - Прониклась мной (Official Audio)', 'T-Fest', 200))
        self.assertTrue(sure)

    def test_remaster_of_the_same_length_is_approved(self):
        top, sure = best(song('Bohemian Rhapsody', ['Queen'], 355),
                         found('Bohemian Rhapsody - Remastered 2011', 'Queen', 355),
                         found('Bohemian Rhapsody - Live Aid', 'Queen', 150))
        self.assertTrue(sure)
        self.assertIn('Remastered', top['title'])

    def test_other_versions_and_other_artists_are_still_not_approved(self):
        self.assertFalse(best(song('Arsenal', ['Slipknot'], 200), found('Arsenal (Live)', 'Slipknot', 200))[1])
        self.assertFalse(best(song('Numb', ['Linkin Park'], 187), found('Numb', 'Some Cover Band', 187))[1])
        self.assertFalse(best(song('Hello', ['Adele'], 295), found('Hello', 'Adele', 295),
                              found('Hello', 'Adele', 299))[1])   # two recordings, almost equally good
        top, sure = best(song('One More Time', ['Daft Punk'], 320), found('One More Time', 'Daft Punk', 320),
                         found('One More Time - Radio Edit', 'Daft Punk', 225))
        self.assertTrue(sure)
        self.assertEqual(top['seconds'], 320)

    def test_same_isrc_is_approved_even_next_to_a_close_reupload(self):
        track = song('Хочу перемен', ['Кино'], 292, isrc='RUB421401394')
        reupload = found('Кино — Хочу перемен', 'fan uploads', 287)
        top, sure = best(track, found('Хочу перемен', 'Кино', 293, isrc='rub421401394'), reupload)
        self.assertTrue(sure)
        self.assertEqual(top['seconds'], 293)
        self.assertFalse(best(track, found('Хочу перемен', 'Кино', 293), reupload)[1])   # no code: the user decides

    def test_latin_source_matches_a_cyrillic_catalogue(self):
        top, sure = best(song('Stydno', ['Ivan Dorn'], 215), found('Стыцамэн', 'Иван Дорн', 215),
                         found('Стыдно', 'Иван Дорн', 215))
        self.assertTrue(sure)
        self.assertEqual(top['title'], 'Стыдно')


class Queries(unittest.TestCase):
    def test_from_specific_to_general_without_repeats(self):
        self.assertEqual(queries(song('Sicko Mode (feat. Drake)', ['Travis Scott', 'Drake'], 1)),
                         ['Travis Scott Drake Sicko Mode', 'Travis Scott Sicko Mode', 'Sicko Mode'])
        self.assertEqual(queries(song('Numb', ['Linkin Park'], 1)), ['Linkin Park Numb', 'Numb'])
        self.assertEqual(queries(song('Стыдно', ['Иван Дорн'], 1)),
                         ['Иван Дорн Стыдно', 'ivan dorn Стыдно', 'Стыдно'])
        self.assertEqual(queries(song('', [], 1)), [])

    def test_gather_stops_at_a_strong_match_and_drops_duplicates(self):
        track, asked = song('Numb', ['Linkin Park'], 187), []
        weak, strong = found('Numb (Live)', 'Linkin Park', 400), found('Numb', 'Linkin Park', 187)

        def fetch(text):
            asked.append(text)
            return {'a': [weak], 'b': [weak, strong], 'c': [found('x', 'y', 1)]}[text]

        self.assertEqual(gather(track, ['a', 'b', 'c'], fetch), [weak, strong])
        self.assertEqual(asked, ['a', 'b'])


class PlatformSearch(unittest.TestCase):
    def test_spotify_asks_once_when_the_isrc_finds_the_recording(self):
        asked = []
        hit = {'id': 'T1', 'name': 'Numb', 'artists': [{'name': 'Linkin Park'}], 'duration_ms': 187000,
               'external_ids': {'isrc': 'USWB10300477'}}

        def search(q, type, limit):
            asked.append(q)
            return {'tracks': {'items': [hit, None]}}

        self.addCleanup(setattr, spotify, '_client', spotify._client)
        spotify._client = types.SimpleNamespace(search=search)
        result = spotify.search(song('Numb', ['Linkin Park'], 187, isrc='uswb10300477'))
        self.assertEqual(asked, ['isrc:uswb10300477'])
        self.assertEqual([c['id'] for c in result], ['T1'])

    def test_spotify_widens_the_search_while_nothing_is_strong(self):
        asked = []
        self.addCleanup(setattr, spotify, '_client', spotify._client)
        spotify._client = types.SimpleNamespace(
            search=lambda q, type, limit: asked.append(q) or {'tracks': {'items': []}})
        self.assertEqual(spotify.search(song('Стыдно', ['Иван Дорн', 'Гость'], 215)), [])
        self.assertEqual(asked, ['Иван Дорн Гость Стыдно', 'Иван Дорн Стыдно', 'ivan dorn Стыдно', 'Стыдно'])

    def test_youtube_spends_one_search_and_asks_for_fifteen(self):
        calls = []

        class Api:
            def search(self):
                return self

            def videos(self):
                return self

            def list(self, **params):
                calls.append(params)
                items = ([{'id': {'videoId': 'v1'}}] if 'q' in params else
                         [{'id': 'v1', 'snippet': {'title': 'Numb', 'channelTitle': 'Linkin Park - Topic'},
                           'contentDetails': {'duration': 'PT3M7S'}}])
                return types.SimpleNamespace(execute=lambda: {'items': items})

        errors = types.ModuleType('googleapiclient.errors')
        errors.HttpError = type('HttpError', (Exception,), {})
        for name, module in (('googleapiclient', types.ModuleType('googleapiclient')),
                             ('googleapiclient.errors', errors)):
            if name not in sys.modules:
                sys.modules[name] = module
                self.addCleanup(sys.modules.pop, name, None)
        self.addCleanup(setattr, youtube, '_client', youtube._client)
        youtube._client = Api()
        result = youtube.search(song('Numb (feat. Nobody)', ['Linkin Park'], 187))
        searches = [c for c in calls if 'q' in c]
        self.assertEqual(len(searches), 1)
        self.assertEqual((searches[0]['q'], searches[0]['maxResults']), ('Linkin Park Numb', 15))
        self.assertEqual((result[0]['seconds'], result[0]['topic']), (187, True))


class Cache(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(setattr, dorieplay, 'CACHE', dorieplay.CACHE)
        dorieplay.CACHE = Path(folder.name) / 'cache.json'
        self.plan = str(Path(folder.name) / 'x.json')
        self.searched = []
        tracks = [song('Numb', ['Linkin Park'], 187), song('Hello', ['Adele'], 295)]
        source = types.SimpleNamespace(NAME='fakesource', LABEL='Source', owns=lambda url: url.startswith('fake:'),
                                       read_playlist=lambda url: ('List', tracks))
        target = types.SimpleNamespace(NAME='fake', LABEL='Fake', search=self.search)
        for module in (source, target):
            platforms.PLATFORMS[module.NAME] = module
            self.addCleanup(platforms.PLATFORMS.pop, module.NAME, None)

    def search(self, track):
        self.searched.append(track['title'])
        return [found(track['title'], track['artists'][0], track['duration_ms'] // 1000)]

    def make_plan(self, **options):
        args = types.SimpleNamespace(playlist='fake:1', to='fake', plan=self.plan,
                                     **{'force': False, 'no_cache': False, **options})
        with contextlib.redirect_stdout(io.StringIO()):
            dorieplay.make_plan(args)
        return dorieplay.load(self.plan)

    def test_planning_again_reuses_saved_answers(self):
        first = self.make_plan()
        second = self.make_plan(force=True)
        self.assertEqual(self.searched, ['Numb', 'Hello'])
        self.assertEqual(first['tracks'], second['tracks'])
        self.assertTrue(all(t['approved'] for t in second['tracks']))

    def test_no_cache_and_old_answers_search_again(self):
        self.make_plan()
        self.make_plan(force=True, no_cache=True)
        self.assertEqual(len(self.searched), 4)
        saved = dorieplay.json.loads(dorieplay.CACHE.read_text(encoding='utf-8'))
        for entry in saved.values():
            entry['at'] -= (dorieplay.CACHE_DAYS + 1) * 86400
        dorieplay.save(dorieplay.CACHE, saved)
        self.make_plan(force=True)
        self.assertEqual(len(self.searched), 6)

    def test_broken_cache_file_is_ignored(self):
        dorieplay.CACHE.write_text('not json', encoding='utf-8')
        self.make_plan()
        self.assertEqual(self.searched, ['Numb', 'Hello'])


if __name__ == '__main__':
    unittest.main()
