"""The song base: YouTube videos that Discogs editors linked to releases (Discogs data dump, CC0), so a
song can be matched without spending a YouTube search (100 a day for the whole app).

  python songbase.py build discogs_YYYYMMDD_masters.xml.gz [--out songbase.sqlite]
  python songbase.py serve [--db songbase.sqlite] [--port 8771]

Dump: https://data.discogs.com/ (data/<year>/discogs_<date>_masters.xml.gz, ~0.6 GB, monthly).
Dorie Play asks a running base only when DORIEPLAY_SONGBASE is set to its address, and only for
transfers to YouTube. It sends the song's title, artists and length, nothing else. These candidates are
not YouTube search results (YouTube policy III.C.5): they are tagged source "discogs", shown with that
label, and used only when one of them is a confident match; otherwise YouTube is searched as before.
Stdlib only. Not checked: whether the linked videos still exist ("Check the videos" on the plan does).
"""
import argparse
import gzip
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from matching import is_confident, normalize, rank

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'songbase.sqlite'
VIDEO = re.compile(r'(?:[?&]v=|youtu\.be/|/embed/)([\w-]{11})')
NUMBERED = re.compile(r' \(\d+\)$')          # Discogs tells same-named artists apart: "Name (2)"
CATCH_ALL = {'', 'various', 'unknown artist', 'no artist'}   # compilations: the video says nothing about the artist
KEEP = 10


# --- Building ----------------------------------------------------------------------------------------

def build(dump, out=DEFAULT_DB):
    """Stream the masters dump into SQLite: one row per (artist, video). Keys use matching.normalize,
    so a change there needs a rebuild."""
    out = Path(out)
    tmp = out.with_name(out.name + '-building')   # matches "*.sqlite-*" in .gitignore
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.execute('CREATE TABLE video (artist_key TEXT, artist TEXT, id TEXT, seconds INT, title TEXT)')
    rows, batch = 0, []
    with gzip.open(dump, 'rb') as raw:
        context = ET.iterparse(raw, events=('start', 'end'))
        _, root = next(context)
        for event, node in context:
            if event != 'end' or node.tag != 'master':
                continue
            artists = [NUMBERED.sub('', (a.findtext('name') or '').strip()) for a in node.findall('artists/artist')]
            artists = [(normalize(a), a) for a in artists if normalize(a) not in CATCH_ALL]
            for video in node.findall('videos/video') if artists else []:
                match = VIDEO.search(video.get('src') or '')
                if not match:
                    continue
                seconds = int(video.get('duration') or 0)
                title = (video.findtext('title') or '').strip()[:300]
                batch += [(key, name, match.group(1), seconds, title) for key, name in artists]
            root.clear()   # keeps memory flat over the 3 GB of XML
            if len(batch) > 50000:
                db.executemany('INSERT INTO video VALUES (?,?,?,?,?)', batch)
                rows, batch = rows + len(batch), []
    db.executemany('INSERT INTO video VALUES (?,?,?,?,?)', batch)
    rows += len(batch)
    db.execute('CREATE INDEX by_artist ON video(artist_key)')
    db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
    db.execute('INSERT INTO meta VALUES (?, ?)', ('built_from', Path(dump).name))
    db.commit()
    db.close()
    tmp.replace(out)
    return rows


# --- Serving -----------------------------------------------------------------------------------------

def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) else ''


def match(db, track):
    """Best candidates for one song: every video of its artists, ranked by Dorie Play's own scoring."""
    artists = [_text(a, 200) for a in (track.get('artists') or [])[:10] if _text(a, 200)]
    song = {'title': _text(track.get('title')), 'artists': artists,
            'duration_ms': track.get('duration_ms') if isinstance(track.get('duration_ms'), int) else 0}
    found = {}
    for key in {normalize(a) for a in artists} - CATCH_ALL:
        for artist, video, seconds, title in db.execute(
                'SELECT artist, id, seconds, title FROM video WHERE artist_key = ? LIMIT 5000', (key,)):
            found.setdefault(video, {'id': video, 'title': title, 'artist': artist, 'seconds': seconds,
                                     'topic': False, 'url': f'https://www.youtube.com/watch?v={video}',
                                     'source': 'discogs'})
    ranked = rank(song, list(found.values())) if found else []
    return [{k: c[k] for k in ('id', 'title', 'artist', 'seconds', 'topic', 'url', 'source')} for c in ranked[:KEEP]]


def serve(db_path=DEFAULT_DB, port=8771, host='127.0.0.1'):
    """POST /v1/match {title, artists, duration_ms} -> {candidates}; GET /v1/health -> {videos, built_from}.
    No request is logged (no IP addresses, no song names).
    shortcut: no rate limit and plain HTTP; on a public server put it behind HTTPS with a limit per address."""
    local = threading.local()

    def database():
        if not hasattr(local, 'db'):
            local.db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, check_same_thread=False)
        return local.db

    class Handler(BaseHTTPRequestHandler):
        server_version = 'DorieSongBase'

        def log_message(self, *args):   # privacy: nothing about requests is written anywhere
            pass

        def answer(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != '/v1/health':
                return self.answer(404, {'error': 'Not found.'})
            db = database()
            meta = dict(db.execute('SELECT key, value FROM meta'))
            self.answer(200, {'videos': db.execute('SELECT count(*) FROM video').fetchone()[0], **meta})

        def do_POST(self):
            if self.path != '/v1/match':
                return self.answer(404, {'error': 'Not found.'})
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 < length <= 4096:
                return self.answer(413, {'error': 'Request is too large.'})
            try:
                track = json.loads(self.rfile.read(length))
                assert isinstance(track, dict) and _text(track.get('title'))
            except (ValueError, AssertionError):
                return self.answer(400, {'error': 'Send {"title", "artists", "duration_ms"}.'})
            self.answer(200, {'candidates': match(database(), track)})

    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


# --- Asking a base from Dorie Play -------------------------------------------------------------------

_down = {}   # address -> when it last failed: not asked for 5 minutes, so a dead base costs one timeout, not one per song


def lookup(track, url=None):
    """Candidates from the song base at DORIEPLAY_SONGBASE (read at call time), or [] when it is not
    set or not reachable. Only title, artists and length are sent. The answer is not trusted."""
    url = (url if url is not None else os.environ.get('DORIEPLAY_SONGBASE', '')).rstrip('/')
    if not url or time.time() - _down.get(url, 0) < 300 or not (track.get('title') or '').strip():
        return []
    body = json.dumps({'title': track.get('title') or '', 'artists': track.get('artists') or [],
                       'duration_ms': int(track.get('duration_ms') or 0)}).encode()
    request = urllib.request.Request(url + '/v1/match', data=body, method='POST',
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            candidates = json.loads(response.read(1_000_000)).get('candidates')
    except (OSError, ValueError, AttributeError) as error:
        _down[url] = time.time()
        print(f'Song base at {url} did not answer ({error.__class__.__name__}); searching YouTube instead.')
        return []
    clean = []
    for c in candidates if isinstance(candidates, list) else []:
        if (isinstance(c, dict) and isinstance(c.get('id'), str) and re.fullmatch(r'[\w-]{11}', c['id'])
                and isinstance(c.get('seconds'), int)):
            clean.append({'id': c['id'], 'title': _text(c.get('title')), 'artist': _text(c.get('artist'), 200),
                          'seconds': c['seconds'], 'topic': False,
                          'url': f"https://www.youtube.com/watch?v={c['id']}", 'source': 'discogs'})
    return clean[:KEEP]


def confident(track, candidates):
    """Ranked candidates when the base alone is sure enough to skip the YouTube search, else []."""
    ranked = rank(track, candidates) if candidates else []
    return ranked if is_confident(ranked) else []


def main(argv=None):
    parser = argparse.ArgumentParser(description='Dorie Play song base (Discogs videos).')
    commands = parser.add_subparsers(dest='command', required=True)
    make = commands.add_parser('build', help='Build the base from a Discogs masters dump')
    make.add_argument('dump')
    make.add_argument('--out', default=str(DEFAULT_DB))
    run = commands.add_parser('serve', help='Answer Dorie Play on 127.0.0.1')
    run.add_argument('--db', default=str(DEFAULT_DB))
    run.add_argument('--port', type=int, default=8771)
    args = parser.parse_args(argv)
    if args.command == 'build':
        print(f'{build(args.dump, args.out)} rows written to {args.out}')
    else:
        if not Path(args.db).exists():
            sys.exit(f'No base at {args.db}. Build it first: python songbase.py build <dump>')
        httpd = serve(args.db, args.port)
        print(f'Song base on http://127.0.0.1:{args.port} (Ctrl+C stops it)')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
