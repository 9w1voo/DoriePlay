"""A pasted list of songs as the source: one song per line, "Artist - Title", optionally numbered
("12. ...") and with the length at the end ("3:45"). For playlists a service will not let us read,
e.g. Spotify without the user's own key. Source side only: it is never a target, so it is not in
PLATFORMS; platforms.detect() returns it for text that is not a link."""
import hashlib
import re

from matching import normalize, parse_video

NAME, LABEL = 'text', 'Song list'
NUMBER = re.compile(r'^\d{1,4}[.)]\s+')
LENGTH = re.compile(r'\s*[(\[]?\b(\d{1,2}):([0-5]\d)[)\]]?$')
LINK = re.compile(r'://|^spotify:|^(www\.|open\.)', re.IGNORECASE)


def owns(text):
    """One line counts as a song only when it cannot be a link (links contain "/")."""
    return bool(text.strip()) and '/' not in text


def lines(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]


def read_playlist(text):
    tracks = []
    for number, line in enumerate(lines(text), 1):
        if LINK.search(line):
            # Cmd+C on songs in the Spotify app gives links; turning them into names needs the service's key.
            raise ValueError(f'Line {number} is a link. Paste song names ("Artist - Title"), one per line.')
        line = NUMBER.sub('', line)
        length = LENGTH.search(line)
        if length:
            line = line[:length.start()]
        artists, title = parse_video(line, '')
        tracks.append({'title': title, 'artists': [a for a in artists if a],
                       'duration_ms': (int(length[1]) * 60 + int(length[2])) * 1000 if length else 0,
                       'source_id': 'line-' + hashlib.sha1(normalize(line).encode()).hexdigest()[:12],
                       'source_title': line})
    return 'Song list', tracks


def playlist_key(text):
    """The same list pasted again (other spacing, empty lines) resumes the same plan."""
    return '\n'.join(lines(text))
