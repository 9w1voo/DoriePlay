"""SoundCloud via the public API (OAuth 2.1 with PKCE).

SoundCloud runs on Dorie Play's own app: the program carries only the public client_id, and there is no
secret in it. Token requests (sign-in code, refresh) go to relay.py on base.dorieplay.com, which adds the
secret and the redirect URI and passes SoundCloud's answer on. The user needs no key of their own.
The sign-in comes back to http://127.0.0.1:8890/callback on this computer (platforms.signin).

API facts used here (github.com/soundcloud/api, openapi/api.yaml):
  auth   https://secure.soundcloud.com/authorize, token https://secure.soundcloud.com/oauth/token;
         access tokens live ~1 hour, refresh tokens are single-use.
  calls  https://api.soundcloud.com with header "Authorization: OAuth <token>", resources by URN.
  GET /resolve?url=...                       playlist link -> API resource
  GET /playlists/{urn}/tracks                tracks, paged with linked_partitioning/next_href
  GET /tracks?q=...                          search
  POST /playlists, PUT /playlists/{urn}      body {"playlist": {..., "tracks": [{"urn": ...}]}}
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode, urlparse, urlsplit

import paths
from matching import gather, parse_video, queries

from . import signin

NAME = 'soundcloud'
LABEL = 'SoundCloud'
API = 'https://api.soundcloud.com'
AUTHORIZE = 'https://secure.soundcloud.com/authorize'
TOKEN_URL = 'https://secure.soundcloud.com/oauth/token'
ROOT = paths.data_folder()   # key and token live in the data folder
TOKEN_FILE = ROOT / '.soundcloud-token.json'
DEFAULT_REDIRECT = 'http://127.0.0.1:8890/callback'
CLIENT_ID = 'sQE0BOTyBi3sJiClVnkaYde5lxV0l7n8'   # public; the secret lives only on the relay
RELAY = 'https://base.dorieplay.com'
NEEDS_KEY = False   # the user brings no key (Spotify and YouTube still do)
LIMIT_TEXT = 'Too many SoundCloud sign-ins from this computer. Try again within an hour.'
BUSY_TEXT = 'SoundCloud sign-in is busy for everyone right now. Try again in a few minutes.'
UNREACHABLE_TEXT = 'Dorie Play’s sign-in server could not reach SoundCloud. Try again later.'
MAX_PLAYLIST_TRACKS = 500   # SoundCloud's limit per playlist
ALL_ACCESS = 'playable,preview,blocked'
_token = None


def owns(url):
    return bool(re.match(r'(https?://)?([\w-]+\.)*soundcloud\.com/|soundcloud:', url.strip(), re.IGNORECASE))


def playlist_key(url):
    """The link without tracking parameters (?si=..., utm_...); the secret token is part of the path."""
    url = url.strip()
    if url.startswith('soundcloud:'):
        return url
    parsed = urlparse(url if '://' in url else 'https://' + url)
    host = (parsed.hostname or '').lower()
    for prefix in ('www.', 'm.'):
        host = host[len(prefix):] if host.startswith(prefix) else host
    return host + parsed.path.rstrip('/')


def item_url(urn):
    return 'https://w.soundcloud.com/player/?url=' + f'{API}/tracks/{urn.rsplit(":", 1)[-1]}'


def playlist_url(urn):
    try:
        return _request('GET', f'/playlists/{urn}', params={'show_tracks': 'false'}).get('permalink_url') or urn
    except Exception:   # the transfer itself is done; a failed lookup of the link must not look like a failure
        return urn


# --- Auth -----------------------------------------------------------------------------------------

class Refused(PermissionError):
    """SoundCloud itself refused the code or the refresh token (invalid_grant): signing in again helps."""


def relay():
    """Address of the token relay: DORIEPLAY_SC_RELAY or RELAY. https anywhere; http only to this computer."""
    value = (os.environ.get('DORIEPLAY_SC_RELAY') or RELAY).strip()
    parts = urlsplit(value)
    local = parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost')
    if (parts.scheme != 'https' and not local) or not parts.hostname or parts.username is not None \
            or parts.password is not None or parts.query or parts.fragment:
        raise ValueError('DORIEPLAY_SC_RELAY must be an https address (or http to 127.0.0.1 / localhost) '
                         'without a user name, query or fragment.')
    return value[:-1] if value.endswith('/') else value


def _save_token(data):
    data = {**data, 'client_id': CLIENT_ID, 'expires_at': time.time() + int(data.get('expires_in', 3600)) - 60}
    TOKEN_FILE.touch(mode=0o600, exist_ok=True)
    TOKEN_FILE.chmod(0o600)
    TOKEN_FILE.write_text(json.dumps(data), encoding='utf-8')
    return data


def _token_request(fields):
    """Ask the relay for a token. The token file changes only after a good answer."""
    import requests
    try:
        response = requests.post(relay() + '/v1/soundcloud/token', json=fields, timeout=30,
                                 allow_redirects=False, headers={'accept': 'application/json'})
    except requests.RequestException as error:
        raise RuntimeError(UNREACHABLE_TEXT) from error
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise RuntimeError(UNREACHABLE_TEXT)
    error = body.get('error')
    error = error if isinstance(error, str) else None
    if status == 200 and isinstance(body.get('access_token'), str) and isinstance(body.get('refresh_token'), str):
        return _save_token(body)
    if status == 429 and error == 'relay_limit':
        raise RuntimeError(LIMIT_TEXT)
    if status == 503 and error in ('relay_busy', 'soundcloud_busy'):
        raise RuntimeError(BUSY_TEXT)
    if status in (400, 401) and error == 'invalid_grant':
        raise Refused(f'SoundCloud refused the sign-in: {error}')
    if status in (400, 401) and error:
        description = body.get('error_description')
        raise PermissionError(f'SoundCloud sign-in failed: {error}'
                              + (f': {description}' if isinstance(description, str) and description else ''))
    raise RuntimeError(UNREACHABLE_TEXT)


def _login():
    """Authorization Code + PKCE; the browser comes back to the redirect URI (platforms.signin)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(16)
    url = AUTHORIZE + '?' + urlencode({
        'client_id': CLIENT_ID, 'redirect_uri': DEFAULT_REDIRECT, 'response_type': 'code',
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'state': state})
    code = signin.wait_for_code(LABEL, url, DEFAULT_REDIRECT, state, announce=False)
    token = _token_request({'grant_type': 'authorization_code', 'code': code, 'code_verifier': verifier})
    print('Signed in to SoundCloud.')   # only after the exchange worked
    return token


def _access_token(force_refresh=False):
    global _token
    if _token is None and TOKEN_FILE.exists():
        try:
            _token = json.loads(TOKEN_FILE.read_text(encoding='utf-8'))
        except ValueError:
            _token = {}
    if not isinstance(_token, dict) or (_token and _token.get('client_id') != CLIENT_ID):
        # a token of another app (an older own key) is never sent to the relay
        _token = None
        TOKEN_FILE.unlink(missing_ok=True)
    elif _token and not force_refresh and time.time() < _token.get('expires_at', 0):
        return _token['access_token']
    if _token and _token.get('refresh_token'):
        try:  # refresh tokens are single-use: the new one is saved right away
            _token = _token_request({'grant_type': 'refresh_token', 'refresh_token': _token['refresh_token']})
            return _token['access_token']
        except Refused:
            pass
    _token = _login()
    return _token['access_token']


def _request(method, path, params=None, body=None, retry_auth=True):
    """API call with token refresh on 401 and back-off on 429."""
    import requests
    url = path if path.startswith('http') else API + path
    target = urlparse(url)
    host = target.hostname or ''
    if target.scheme != 'https' or not (host == 'soundcloud.com' or host.endswith('.soundcloud.com')):
        # Links from API answers (next page, redirect) are followed with the token: only to SoundCloud itself.
        raise RuntimeError(f'SoundCloud answered with a link to an unexpected address: {url[:100]}')
    for attempt in range(6):
        response = requests.request(method, url, params=params, json=body, allow_redirects=False, timeout=60,
                                    headers={'Authorization': f'OAuth {_access_token()}',
                                             'accept': 'application/json; charset=utf-8'})
        if response.status_code == 401 and retry_auth:
            _access_token(force_refresh=True)
            retry_auth = False
            continue
        if response.status_code == 429 and attempt < 5:
            wait = response.headers.get('Retry-After') or ''   # seconds; may also be an HTTP date
            time.sleep(min(int(wait), 300) if wait.isdigit() else 2 ** (attempt + 1))
            continue
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get('Location')
            if not location:
                try:
                    location = response.json().get('location')
                except ValueError:
                    location = None
            return {'location': location}
        if not response.ok:
            raise RuntimeError(f'SoundCloud {method} {path} failed: {response.status_code} {response.text[:200]}')
        return response.json() if response.content else {}
    raise RuntimeError('SoundCloud keeps answering 429 (too many requests). Try again later.')


def _pages(path, **params):
    page = _request('GET', path, params={**params, 'linked_partitioning': 'true'})
    while True:
        yield from page.get('collection', [])
        if not page.get('next_href'):
            return
        page = _request('GET', page['next_href'])


# --- Source ---------------------------------------------------------------------------------------

def _playlist(url):
    """Playlist object (without tracks) for a soundcloud.com / on.soundcloud.com link or a URN."""
    if url.startswith('soundcloud:'):
        return _request('GET', f'/playlists/{url}', params={'show_tracks': 'false'})
    found = _request('GET', '/resolve', params={'url': url})
    if 'location' in found:
        if not found['location']:
            raise ValueError('SoundCloud could not resolve this link.')
        found = _request('GET', found['location'], params={'show_tracks': 'false'})
    if found.get('kind') not in ('playlist', 'system-playlist'):
        raise ValueError(f"This SoundCloud link is a {found.get('kind', 'unknown item')}, not a playlist. "
                         'Use a link to a playlist (soundcloud.com/<user>/sets/<name>).')
    return found


def read_playlist(url):
    """(name, tracks). Artist and title come from the uploader's fields and the track title."""
    playlist = _playlist(url)
    tracks = []
    for item in _pages(f"/playlists/{playlist['urn']}/tracks", access=ALL_ACCESS, limit=200):
        if not item or not item.get('urn'):
            continue
        uploader = item.get('metadata_artist') or (item.get('user') or {}).get('username') or ''
        artists, title = parse_video(item.get('title') or '', uploader)
        tracks.append({'title': title, 'artists': artists, 'duration_ms': item.get('duration') or 0,
                       'source_id': item['urn'], 'source_title': item.get('title'), 'isrc': item.get('isrc')})
    return playlist.get('title') or 'SoundCloud playlist', tracks


# --- Target ---------------------------------------------------------------------------------------

def _candidate(item):
    return {'id': item['urn'], 'title': item.get('title') or '',
            'artist': item.get('metadata_artist') or (item.get('user') or {}).get('username') or '',
            'seconds': round((item.get('duration') or 0) / 1000), 'topic': False,
            'url': item.get('permalink_url') or item_url(item['urn']), 'isrc': item.get('isrc')}


def search(track):
    """Up to 10 tracks per request that can be played (fully or as a preview);
    less specific requests are made only while no strong match has been found."""
    def fetch(text):
        found = _request('GET', '/tracks', params={'q': text, 'limit': 10, 'access': 'playable,preview',
                                                   'linked_partitioning': 'true'})
        return [_candidate(i) for i in found.get('collection', []) if i and i.get('urn')]
    return gather(track, queries(track), fetch)


def create_playlist(name):
    created = _request('POST', '/playlists', body={'playlist': {
        'title': name, 'description': 'Transferred with Dorie Play', 'sharing': 'private', 'tracks': []}})
    return created['urn']


def _track_urns(playlist_urn):
    return [t['urn'] for t in _pages(f'/playlists/{playlist_urn}/tracks', access=ALL_ACCESS, limit=200)
            if t and t.get('urn')]


def playlist_counts(playlist_urn):
    existing = {}
    for urn in _track_urns(playlist_urn):
        existing[urn] = existing.get(urn, 0) + 1
    return existing


def add_items(playlist_urn, items):
    """items: list of (track_urn, label). SoundCloud replaces the whole track list on update,
    so the current list plus the new tracks is sent, 100 new tracks at a time.
    Returns [(label, reason)] for ids that are not SoundCloud tracks; the other songs are still added."""
    skipped = [(label, f'"{urn}" is not a SoundCloud track id (soundcloud:tracks:...)')
               for urn, label in items if not re.fullmatch(r'soundcloud:[\w-]+:[\w-]+', urn)]
    items = [(urn, label) for urn, label in items if re.fullmatch(r'soundcloud:[\w-]+:[\w-]+', urn)]
    if not items:
        return skipped
    current = _track_urns(playlist_urn)
    if len(current) + len(items) > MAX_PLAYLIST_TRACKS:
        raise ValueError(f'SoundCloud playlists hold at most {MAX_PLAYLIST_TRACKS} tracks; '
                         f'this would make {len(current) + len(items)}.')
    for start in range(0, len(items), 100):
        batch = items[start:start + 100]
        wanted = current + [urn for urn, _ in batch]
        _request('PUT', f'/playlists/{playlist_urn}', body={'playlist': {'tracks': [{'urn': u} for u in wanted]}})
        current = _track_urns(playlist_urn)
        if len(current) != len(wanted):
            raise RuntimeError(f'SoundCloud playlist has {len(current)} tracks after the update, expected '
                               f'{len(wanted)}. Check the playlist before running apply again.')
        for _, label in batch:
            print('Added:', label)
    return skipped
