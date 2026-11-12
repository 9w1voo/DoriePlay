"""SoundCloud via the public API (OAuth 2.1 with PKCE).

Credentials come from soundcloud-client.json in the project folder:
  {"client_id": "...", "client_secret": "...", "redirect_uri": "http://127.0.0.1:8890/callback"}
SoundCloud issues them only to Artist Pro subscribers (soundcloud.com/you/apps).
The redirect URI must be registered on that page exactly as written in the file.

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
import html
import json
import re
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from matching import gather, parse_video, queries

NAME = 'soundcloud'
LABEL = 'SoundCloud'
API = 'https://api.soundcloud.com'
AUTHORIZE = 'https://secure.soundcloud.com/authorize'
TOKEN_URL = 'https://secure.soundcloud.com/oauth/token'
ROOT = Path(__file__).resolve().parent.parent   # key and token live next to the program
CLIENT_FILE = ROOT / 'soundcloud-client.json'
TOKEN_FILE = ROOT / '.soundcloud-token.json'
DEFAULT_REDIRECT = 'http://127.0.0.1:8890/callback'
MAX_PLAYLIST_TRACKS = 500   # SoundCloud's limit per playlist
LOGIN_TIMEOUT = 300         # seconds to wait for the browser sign-in
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

def _client():
    if not CLIENT_FILE.exists():
        raise ValueError('Create soundcloud-client.json with client_id, client_secret and redirect_uri '
                         '(see README). SoundCloud gives API keys to Artist Pro subscribers.')
    data = json.loads(CLIENT_FILE.read_text(encoding='utf-8'))
    data.setdefault('redirect_uri', DEFAULT_REDIRECT)
    return data


def _save_token(data):
    data = {**data, 'expires_at': time.time() + int(data.get('expires_in', 3600)) - 60}
    TOKEN_FILE.touch(mode=0o600, exist_ok=True)
    TOKEN_FILE.chmod(0o600)
    TOKEN_FILE.write_text(json.dumps(data), encoding='utf-8')
    return data


def _token_request(fields):
    import requests
    app = _client()
    fields = {**fields, 'client_id': app['client_id']}
    if app.get('client_secret'):
        fields['client_secret'] = app['client_secret']
    response = requests.post(TOKEN_URL, data=fields, headers={'accept': 'application/json; charset=utf-8'},
                             timeout=30)
    if not response.ok:
        raise PermissionError(f'SoundCloud token request failed: {response.status_code} {response.text[:200]}')
    return _save_token(response.json())


def _login():
    """Authorization Code + PKCE through a one-shot local web server on the redirect URI."""
    app = _client()
    redirect = urlparse(app['redirect_uri'])
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(16)
    url = AUTHORIZE + '?' + urlencode({
        'client_id': app['client_id'], 'redirect_uri': app['redirect_uri'], 'response_type': 'code',
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'state': state})
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path != (redirect.path or '/'):
                self.send_response(404), self.end_headers()
                return
            result.update({k: v[0] for k, v in query.items()})
            ok = 'code' in result and result.get('state') == state
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            message = 'SoundCloud connected. You can close this tab.' if ok else 'SoundCloud sign-in failed.'
            self.wfile.write(f'<p>{html.escape(message)}</p>'.encode())

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer((redirect.hostname, redirect.port or 80), Callback)
    except OSError as error:
        raise ValueError(f'Cannot wait for the SoundCloud sign-in on {redirect.hostname}:{redirect.port or 80} '
                         f'({error.strerror}). Another run is probably still waiting for a sign-in: '
                         'stop it and try again.') from error
    server.timeout = 1
    print('Please visit this URL to authorize SoundCloud:', url)
    webbrowser.open(url)
    deadline = time.time() + LOGIN_TIMEOUT
    while 'code' not in result and 'error' not in result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if 'code' not in result and 'error' not in result:
        raise PermissionError(f'SoundCloud sign-in was not finished within {LOGIN_TIMEOUT // 60} minutes. '
                              'Open the link above in a browser on this computer and run the command again.')
    if result.get('state') != state or 'code' not in result:
        raise PermissionError(f"SoundCloud sign-in failed: {result.get('error_description') or result.get('error') or 'state mismatch'}")
    return _token_request({'grant_type': 'authorization_code', 'code': result['code'],
                           'redirect_uri': app['redirect_uri'], 'code_verifier': verifier})


def _access_token(force_refresh=False):
    global _token
    if _token is None and TOKEN_FILE.exists():
        _token = json.loads(TOKEN_FILE.read_text(encoding='utf-8'))
    if _token and not force_refresh and time.time() < _token.get('expires_at', 0):
        return _token['access_token']
    if _token and _token.get('refresh_token'):
        try:  # refresh tokens are single-use: the new one is saved right away
            _token = _token_request({'grant_type': 'refresh_token', 'refresh_token': _token['refresh_token']})
            return _token['access_token']
        except PermissionError:
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
