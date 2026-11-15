"""Deezer as a source: public playlists through Deezer's open API (api.deezer.com), with no key and no
sign-in. Checked on 15 Nov 2026: a public playlist comes with each song's ISRC and length, so a transfer
to Spotify finds the exact recording. Source side only: creating playlists needs a registered Deezer app
and Deezer no longer registers new ones, so it is not in PLATFORMS; platforms.detect() returns it for
deezer.com links. Private playlists cannot be read. Not checked: Deezer's terms for this open API."""
import re
import time
from urllib.parse import urljoin, urlparse

NAME, LABEL = 'deezer', 'Deezer'
API = 'https://api.deezer.com/'
HOST = re.compile(r'(https?://)?([\w-]+\.)*deezer\.com/', re.IGNORECASE)
PLAYLIST = re.compile(r'deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?playlist/(\d+)', re.IGNORECASE)
NOT_FOUND = 'Deezer playlist not found, or it is private. Make it public in Deezer and paste the link again.'


def owns(url):
    """By host, like the other services: "soundcloud.com/x/sets/deezer-hits" is not a Deezer link."""
    return bool(HOST.match(url.strip()))


def playlist_key(url):
    """Playlist id: the same for every copy of the link (language part, ?utm_...)."""
    match = PLAYLIST.search(url)
    return match.group(1) if match else url.strip()


def _redirect(url):
    """Where a Deezer share link (link.deezer.com/s/...) points, or None. Read, not followed: checked first."""
    import requests   # not urllib: python.org Python on macOS has no certificates until set up, requests has its own
    response = requests.get(url, allow_redirects=False, timeout=15)
    location = response.headers.get('Location') if response.status_code in (301, 302, 303, 307, 308) else None
    return urljoin(url, location) if location else None


def playlist_id(url):
    url = url.strip() if '://' in url else 'https://' + url.strip()
    for _ in range(3):   # share links go link.deezer.com/s/... -> www.deezer.com/.../playlist/123
        match = PLAYLIST.search(url)
        if match:
            return match.group(1)
        if not owns(url) or (urlparse(url).hostname or '').lower() != 'link.deezer.com':
            break
        url = _redirect(url) or ''
    raise ValueError('This Deezer link is not a playlist. Open the playlist in Deezer and copy its link '
                     '(Share → Copy link).')


def _get(url):
    """One API answer; waits on Deezer's limit (50 requests per 5 seconds)."""
    import requests
    for attempt in range(5):
        response = requests.get(url, allow_redirects=False, timeout=30)
        try:
            data = response.json()
        except ValueError:
            data = None
        error = data.get('error') if isinstance(data, dict) else {'message': f'HTTP {response.status_code}'}
        if not error:
            return data
        if error.get('code') == 4 and attempt < 4:   # "Quota limit exceeded"
            time.sleep(5)
            continue
        if error.get('code') == 800:   # "no data"
            raise ValueError(NOT_FOUND)
        raise RuntimeError(f"Deezer refused the request: {error.get('message') or error}")
    raise RuntimeError('Deezer keeps saying the request limit is reached. Try again in a minute.')


def read_playlist(url):
    """(name, tracks) of a public Deezer playlist."""
    number = playlist_id(url)
    name = _get(f'{API}playlist/{number}').get('title') or 'Deezer playlist'
    tracks, page = [], f'{API}playlist/{number}/tracks?limit=100'
    for _ in range(200):   # 20,000 songs; Deezer playlists hold far fewer
        data = _get(page)
        for item in data.get('data') or []:
            if item.get('type', 'track') != 'track' or not item.get('title') or not item.get('id'):
                continue
            artist = (item.get('artist') or {}).get('name')
            tracks.append({'title': item['title'], 'artists': [artist] if artist else [],
                           'duration_ms': int(item.get('duration') or 0) * 1000, 'source_id': str(item['id']),
                           **({'isrc': item['isrc']} if item.get('isrc') else {})})
        page = data.get('next')
        if not (isinstance(page, str) and page.startswith(API)):   # only ever follow Deezer's own API
            break
    return name, tracks
