"""Invented playlist and fake services for `python server.py --demo`.

Nothing here talks to the network: the web interface can be tried without keys, and tests use it too.
The songs and artists are made up; each one shows a different situation of the review screen.
"""
import time

from platforms import soundcloud, spotify, youtube

PLAYLIST = 'Демо: вечірній мікс'
# title, artists, seconds, what the search finds
SONGS = [
    ('Нічне місто', ['Оля Верес'], 214, 'exact'),
    ('Paper Lanterns', ['The Harbour Lights'], 197, 'exact'),
    ('Тепла зима (feat. Марко Лис)', ['Данило Рай', 'Марко Лис'], 231, 'exact'),
    ('Glass Garden - 2019 Remaster', ['Mira Solen'], 262, 'remaster'),
    ('Перший сніг', ['Гурт «Вітряки»'], 188, 'two_cuts'),
    ('Slow Orbit', ['Kite Theory', 'June Abara', 'Lo Marek'], 305, 'lead_only'),
    ('Кава і дощ', ['Соломія Грай'], 176, 'live_only'),
    ('Half Past Never', ['The Harbour Lights'], 243, 'two_cuts'),
    ('Маяк', ['Оля Верес'], 201, 'exact'),
    ('Tin Roof Radio', ['Old Station Band'], 219, 'cover_only'),
    ('Сьома ранку', ['Данило Рай'], 164, 'missing'),
    ('Copper Wires', ['Mira Solen'], 228, 'exact'),
]
KINDS = {title: kind for title, _, _, kind in SONGS}


class Fake:
    """Stands in for one service: reads the demo playlist, 'finds' songs, keeps playlists in memory."""

    def __init__(self, real, delay=0.25):
        self.NAME, self.LABEL, self.owns = real.NAME, real.LABEL, real.owns
        self.delay, self.playlists = delay, {}

    def playlist_key(self, url):
        return url.strip()

    def item_url(self, item_id):
        return ''

    def playlist_url(self, playlist_id):
        return ''

    def read_playlist(self, url):
        time.sleep(self.delay)
        return PLAYLIST, [{'title': title, 'artists': artists, 'duration_ms': seconds * 1000,
                           'source_id': f'demo-source-{number}'}
                          for number, (title, artists, seconds, _) in enumerate(SONGS)]

    def search(self, track):
        time.sleep(self.delay)
        title, artists, seconds = track['title'], track['artists'], track['duration_ms'] // 1000
        lead, plain = artists[0], title.split(' (feat')[0].split(' - ')[0]

        def hit(suffix, name, artist, length, topic=False):
            return {'id': f"demo-{self.NAME}-{track['source_id'][12:]}-{suffix}", 'title': name,
                    'artist': artist, 'seconds': length, 'topic': topic, 'url': ''}

        return {
            'exact': [hit('a', plain, lead, seconds), hit('b', f'{plain} (Live)', lead, seconds + 41),
                      hit('c', f'{plain} (Karaoke)', 'Sing Along Studio', seconds)],
            'remaster': [hit('a', f'{plain} (Remastered 2019)', lead, seconds),
                         hit('b', f'{plain} (Acoustic)', lead, seconds - 20)],
            'two_cuts': [hit('a', plain, f'{lead} - Topic', seconds, topic=True),
                         hit('b', f'{lead} - {plain} (Official Video)', lead, seconds + 9),
                         hit('c', f'{plain} (Sped Up)', 'nightdrive edits', seconds - 30)],
            'lead_only': [hit('a', f'{lead} - {plain} (Official Audio)', lead, seconds),
                          hit('b', f'{plain} (Remix)', 'DJ Parallel', seconds + 15)],
            'live_only': [hit('a', f'{plain} (Live)', lead, seconds + 25),
                          hit('b', f'{plain} — огляд альбому', 'Музичний канал', 600)],
            'cover_only': [hit('a', f'{plain} (Cover)', 'Evening Covers', seconds + 2),
                           hit('b', plain, 'Tin Roof Tribute', seconds + 4)],
            'missing': [],
        }[KINDS.get(title, 'exact')]

    def create_playlist(self, name):
        playlist_id = f'demo-playlist-{len(self.playlists) + 1}'
        self.playlists[playlist_id] = []
        return playlist_id

    def playlist_counts(self, playlist_id):
        counts = {}
        for item in self.playlists.get(playlist_id, []):
            counts[item] = counts.get(item, 0) + 1
        return counts

    def add_items(self, playlist_id, items):
        for item, label in items:
            time.sleep(self.delay / 2)
            self.playlists.setdefault(playlist_id, []).append(item)
            print('Added:', label)
        return []


def platforms_for_demo(delay=0.25):
    """{name: fake} for the three real services, in the same order as platforms.PLATFORMS."""
    return {real.NAME: Fake(real, delay) for real in (spotify, youtube, soundcloud)}
