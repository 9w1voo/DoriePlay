"""YouTube via YouTube Data API v3 (OAuth desktop client in google-client.json)."""
import json
import re
import time
from pathlib import Path

from matching import parse_video, queries

NAME = 'youtube'
LABEL = 'YouTube'
ROOT = Path(__file__).resolve().parent.parent   # key and token live next to the program
CLIENT_FILE = ROOT / 'google-client.json'
TOKEN_FILE = ROOT / '.youtube-token.json'
_client = None


def owns(url):
    url = url.strip()
    return (bool(re.match(r'(https?://)?([\w-]+\.)*(youtube\.com|youtu\.be)/', url, re.IGNORECASE))
            or bool(re.fullmatch(r'(PL|OL|RD)[\w-]+', url)))


def playlist_key(url):
    """Playlist id: the same for 'playlist?list=X' and 'watch?v=...&list=X' links."""
    match = re.search(r'[?&]list=([\w-]+)', url)
    return match.group(1) if match else url.strip()


def item_id(value):
    """Video id from a video link pasted into match_id; anything else is returned unchanged."""
    match = re.search(r'(?:[?&]v=|youtu\.be/|/shorts/)([\w-]{11})', value)
    return match.group(1) if match else value


def _reason(error):
    """YouTube's machine-readable error reason ('quotaExceeded', 'videoNotFound', ...)."""
    try:
        return json.loads(error.content)['error']['errors'][0]['reason']
    except (ValueError, KeyError, IndexError, TypeError):
        return ''


QUOTA_REASONS = ('quotaExceeded', 'dailyLimitExceeded')   # not rateLimitExceeded: that one passes in seconds
QUOTA_MESSAGE = ('YouTube daily limit is used up. Run the same command again after it resets '
                 '(midnight Pacific time, 09:00 in Amsterdam); it continues where it stopped.')


def item_url(item_id):
    return f'https://www.youtube.com/watch?v={item_id}'


def playlist_url(playlist_id):
    return f'https://www.youtube.com/playlist?list={playlist_id}'


def client():
    global _client
    if _client:
        return _client
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from googleapiclient.discovery import build
    scopes = ['https://www.googleapis.com/auth/youtube.force-ssl']
    token = TOKEN_FILE
    creds = Credentials.from_authorized_user_file(str(token), scopes) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Testing-mode refresh tokens expire after 7 days: sign in again.
            creds = None
    if not creds or not creds.valid:
        if not CLIENT_FILE.exists():
            raise ValueError(f'{CLIENT_FILE} is missing. Download the OAuth "Desktop app" client JSON from '
                             'Google Cloud Console and save it under that name (see README).')
        creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), scopes).run_local_server(port=0)
    token.touch(mode=0o600, exist_ok=True)
    token.chmod(0o600)
    token.write_text(creds.to_json(), encoding='utf-8')
    _client = build('youtube', 'v3', credentials=creds)
    return _client


def _seconds(iso):
    match = re.fullmatch(r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso or '')
    return sum(int(n or 0) * m for n, m in zip(match.groups(), (86400, 3600, 60, 1))) if match else 0


def _videos(ids, part='snippet,contentDetails'):
    """videos.list in chunks of 50 (1 quota unit each)."""
    found = {}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        response = client().videos().list(part=part, id=','.join(chunk)).execute()
        found.update({v['id']: v for v in response.get('items', [])})
    return found


def unavailable(ids):
    """The ids among these that cannot be played any more: deleted (YouTube leaves them out of the
    answer), private or rejected. 1 quota unit per 50; the answer is used at once and not stored."""
    found = _videos(list(dict.fromkeys(ids)), part='status')
    return {video for video in ids if video not in found
            or found[video].get('status', {}).get('privacyStatus') == 'private'
            or found[video].get('status', {}).get('uploadStatus') in ('deleted', 'failed', 'rejected')}


# --- Source ---------------------------------------------------------------------------------------

def read_playlist(url):
    """(name, tracks) of a YouTube playlist; deleted and private videos are skipped."""
    playlist_id = playlist_key(url)
    if not re.fullmatch(r'[\w-]+', playlist_id):
        raise ValueError('Invalid YouTube playlist URL or ID.')
    if playlist_id.startswith('RD'):
        raise ValueError('This is a YouTube Mix generated for you; the API cannot read it. '
                         'Save the videos to your own playlist first.')
    yt = client()
    info = yt.playlists().list(part='snippet', id=playlist_id).execute().get('items', [])
    if not info:
        raise ValueError(f'YouTube playlist {playlist_id} not found or not accessible.')
    ids = []
    request = yt.playlistItems().list(part='contentDetails', playlistId=playlist_id, maxResults=50)
    while request:
        response = request.execute()
        ids += [i['contentDetails']['videoId'] for i in response.get('items', [])]
        request = yt.playlistItems().list_next(request, response)
    videos = _videos(list(dict.fromkeys(ids)))
    tracks = []
    for video_id in ids:
        video = videos.get(video_id)
        if not video:
            continue  # deleted or private
        artists, title = parse_video(video['snippet']['title'], video['snippet']['channelTitle'])
        tracks.append({'title': title, 'artists': artists,
                       'duration_ms': _seconds(video['contentDetails']['duration']) * 1000,
                       'source_id': video_id, 'source_title': video['snippet']['title']})
    return info[0]['snippet']['title'], tracks


# --- Target ---------------------------------------------------------------------------------------

def search(track):
    """Up to 15 candidate videos from ONE search: YouTube allows about 100 searches a day per API
    project and a search costs the same for 5 or 50 results, so ask for more instead of asking again.
    Label "... - Topic" uploads are often below the fifth place."""
    from googleapiclient.errors import HttpError
    texts = queries(track)
    if not texts:
        return []
    try:
        found = client().search().list(part='snippet', q=texts[0], type='video', maxResults=15).execute()
    except HttpError as error:
        if _reason(error) in QUOTA_REASONS:
            raise RuntimeError(QUOTA_MESSAGE) from error
        raise
    ids = [x['id']['videoId'] for x in found.get('items', [])]
    videos = _videos(ids) if ids else {}
    return [{'id': v['id'], 'title': v['snippet']['title'], 'artist': v['snippet']['channelTitle'],
             'seconds': _seconds(v['contentDetails']['duration']),
             'topic': v['snippet']['channelTitle'].endswith(' - Topic'), 'url': item_url(v['id'])}
            for v in (videos.get(i) for i in ids) if v]


def create_playlist(name):
    result = client().playlists().insert(part='snippet,status', body={
        'snippet': {'title': name, 'description': 'Transferred with Dorie Play'},
        'status': {'privacyStatus': 'private'}}).execute()
    return result['id']


def playlist_counts(playlist_id):
    """How many times each video is already in the playlist; waits out YouTube's delay for new playlists."""
    from googleapiclient.errors import HttpError
    for attempt in range(4):
        try:
            existing = {}
            request = client().playlistItems().list(part='contentDetails', playlistId=playlist_id, maxResults=50)
            while request:
                response = request.execute()
                for item in response.get('items', []):
                    video_id = item['contentDetails']['videoId']
                    existing[video_id] = existing.get(video_id, 0) + 1
                request = client().playlistItems().list_next(request, response)
            return existing
        except HttpError as error:
            if error.resp.status != 404:
                raise
            if attempt == 3:
                raise ValueError(f'YouTube playlist {playlist_id} not found. If you deleted it, remove '
                                 '"target_playlist_id" from the plan file and run apply again.') from error
            time.sleep(5 * (attempt + 1))


def add_items(playlist_id, items):
    """items: list of (video_id, label). One insert each (50 quota units).
    YouTube rejects inserts that come too fast with 409 "aborted" (nothing is added then),
    so pause between inserts and retry those with a growing delay.
    Returns [(label, reason)] for videos that no longer exist; the other songs are still added."""
    from googleapiclient.errors import HttpError
    skipped = []
    for video_id, label in items:
        if not re.fullmatch(r'[\w-]{11}', video_id):
            skipped.append((label, f'"{video_id}" is not a YouTube video id'))
            continue
        request = client().playlistItems().insert(part='snippet', body={'snippet': {
            'playlistId': playlist_id, 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}})
        added = False
        for attempt in range(5):
            try:
                request.execute()
                added = True
                break
            except HttpError as error:
                reason = _reason(error)
                if reason == 'videoNotFound':
                    skipped.append((label, 'the video was deleted or made private'))
                    break
                if reason in QUOTA_REASONS:
                    raise RuntimeError(QUOTA_MESSAGE) from error
                if error.resp.status != 409 or attempt == 4:
                    raise RuntimeError(f'Could not add "{label}" ({video_id}): {error}') from error
                time.sleep(2 ** (attempt + 1))
        if added:
            print('Added:', label)
        time.sleep(1)
    return skipped
