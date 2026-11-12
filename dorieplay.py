"""Dorie Play: move playlists between music platforms.

  plan    read a playlist and search each song on the other platform (changes nothing)
  review  list songs that need a decision, with links to every candidate
  apply   create a private playlist from the approved matches
"""
import argparse
import json
import time
from pathlib import Path

import platforms
from matching import is_confident, normalize, rank

CACHE = Path(__file__).resolve().parent / '.search-cache.json'
CACHE_DAYS = 30   # YouTube's API policy: stored API data is refreshed or deleted within 30 days
KEEP = 10         # candidates kept per song in the plan (best first)


def save(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def load(path):
    """Read a plan; plans made by the Spotify → YouTube-only version are converted on the fly."""
    try:
        plan = json.loads(Path(path).read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise ValueError(f'{path} is not valid JSON: line {error.lineno}, column {error.colno} ({error.msg}). '
                         'Typical causes: curly quotes “ ” from TextEdit, a missing comma or quote. '
                         'Fix that line in VS Code or nano.') from error
    if not isinstance(plan, dict) or not isinstance(plan.get('tracks'), list):
        raise ValueError(f'{path} is not a Dorie Play plan (no "tracks" list).')
    if 'from' not in plan:
        plan['from'], plan['to'] = 'spotify', 'youtube'
        if 'youtube_playlist_id' in plan:
            plan['target_playlist_id'] = plan.pop('youtube_playlist_id')
        for track in plan.get('tracks', []):
            track['source_id'] = track.pop('spotify_id', track.get('source_id'))
            track['match_id'] = track.pop('video_id', track.get('match_id'))
            converted = []
            for c in track.get('candidates', []):
                video_id, channel = c.pop('video_id', None), c.pop('channel', '')
                converted.append({**c, 'id': video_id, 'artist': channel, 'topic': channel.endswith(' - Topic'),
                                  'url': platforms.youtube.item_url(video_id)})
            track['candidates'] = converted
    return plan


def describe(track):
    return f"{', '.join(track.get('artists') or [])} — {track.get('title')}"


def same_playlist(source, a, b):
    """The same playlist even when the link was copied again (Spotify adds a new ?si=... each time)."""
    key = getattr(source, 'playlist_key', lambda url: url)
    return key(a or '') == key(b or '')


def chosen_id(track, target):
    """match_id as the user typed it: an id, or (forgivingly) a link to a candidate."""
    value = str(track['match_id']).strip()
    for candidate in track.get('candidates') or []:
        if value in (candidate.get('id'), candidate.get('url')):
            return candidate['id']
    if hasattr(target, 'item_id'):
        value = target.item_id(value)
    if '://' in value:
        raise ValueError(f'"match_id" of "{describe(track)}" is a link the program cannot read. '
                         'Put the "id" of the chosen candidate there instead.')
    return value


def cached_search(target, track, use_cache=True):
    """target.search(track), remembered on disk: planning the same song again (a new plan, --force,
    a re-run after a scoring change) does not spend the daily YouTube searches a second time."""
    key = '|'.join([target.NAME, normalize(' '.join(track['artists']) + ' ' + track['title']),
                    str((track.get('duration_ms') or 0) // 1000), track.get('isrc') or ''])
    now = time.time()
    try:
        cache = json.loads(CACHE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and now - v.get('at', 0) < CACHE_DAYS * 86400}
    if use_cache and key in cache:
        return cache[key]['candidates']
    cache[key] = {'at': now, 'candidates': target.search(track)}
    save(CACHE, cache)   # shortcut: the whole file is rewritten per song; fine for thousands of songs
    return cache[key]['candidates']


def start_plan(args, source, target, name, tracks):
    """New plan, or resume an interrupted one; refuse to overwrite a reviewed or applied one."""
    path = Path(args.plan)
    fresh = {'name': name, 'source': args.playlist, 'from': source.NAME, 'to': target.NAME, 'tracks': []}
    if not path.exists() or args.force:
        return fresh
    plan = load(path)
    done = [t['source_id'] for t in plan.get('tracks', [])]
    if plan.get('target_playlist_id'):
        raise ValueError(f'{path} was already applied. Use --plan other.json or --force to start over.')
    # Songs already searched that match the start of the playlist prove it is the same one, whatever
    # form the link has (on.soundcloud.com/XyZ vs soundcloud.com/user/sets/name). The link itself
    # only decides for a plan that has no songs yet.
    same_link = same_playlist(source, plan.get('source'), args.playlist)
    if (plan.get('from') != source.NAME or plan.get('to') != target.NAME
            or done != [t['source_id'] for t in tracks[:len(done)]]
            or not (done or same_link)):
        raise ValueError(f'{path} belongs to another transfer or the playlist changed. '
                         'Use --plan other.json or --force to start over.')
    if len(done) == len(tracks):
        raise ValueError(f'{path} is already complete. Use --force to search again (your edits will be lost).')
    print(f'Resuming {path}: {len(done)} of {len(tracks)} songs already searched.')
    return plan


def make_plan(args, progress=None):
    """progress(done, total) is called after the playlist is read and after every song (web UI)."""
    source = platforms.detect(args.playlist)
    target = platforms.get(args.to) if args.to else next(p for p in platforms.PLATFORMS.values() if p is not source)
    if target is source:
        raise ValueError('Source and target are the same platform.')
    name, tracks = source.read_playlist(args.playlist)
    if not tracks:
        raise ValueError('The playlist has no songs that can be transferred.')
    print(f'{source.LABEL} → {target.LABEL}: "{name}", {len(tracks)} songs')
    plan = start_plan(args, source, target, name, tracks)
    save(args.plan, plan)
    if progress:
        progress(len(plan['tracks']), len(tracks))
    for track in tracks[len(plan['tracks']):]:
        candidates = rank(track, cached_search(target, track, not args.no_cache))[:KEEP]
        best = candidates[0] if candidates else None
        confident = is_confident(candidates)
        plan['tracks'].append({**track, 'approved': confident, 'match_id': best['id'] if best else None,
                               'candidates': candidates})
        save(args.plan, plan)
        print(f"{describe(track)}: {best['score'] if best else 'not found'} / {'approved' if confident else 'review'}")
        if progress:
            progress(len(plan['tracks']), len(tracks))
    approved = sum(t['approved'] for t in plan['tracks'])
    print(f'Plan saved: {args.plan}. {approved} of {len(plan["tracks"])} approved automatically.\n'
          f'Check the rest with: python dorieplay.py review --plan {args.plan}')


def review_plan(args):
    plan = load(args.plan)
    pending = [(i, t) for i, t in enumerate(plan['tracks'], 1) if not t.get('approved')]
    print(f'{plan["name"]}: {len(plan["tracks"]) - len(pending)} approved, {len(pending)} need a decision.')
    for number, track in pending:
        print(f"\n#{number} {describe(track)} ({(track.get('duration_ms') or 0) // 1000}s)")
        if track.get('source_title'):
            print(f"   source: {track['source_title']}")
        for c in track.get('candidates') or []:
            mark = '*' if c.get('id') == track.get('match_id') else ' '
            print(f"  {mark}{c.get('score') or 0:.2f} {c.get('seconds')}s [{c.get('artist')}] {c.get('title')}\n"
                  f"     {c.get('url')}")
        if not track.get('candidates'):
            print('   nothing found')
    if pending:
        print('\nTo take a song: in the plan file set its "match_id" to the chosen id and "approved" to true.')


def apply_plan(args):
    plan = load(args.plan)
    target = platforms.get(plan['to'])
    approved = [t for t in plan['tracks'] if t.get('approved') and t.get('match_id')]
    if not approved:
        raise ValueError('No approved songs in plan.')
    # Everything that can be wrong in a hand-edited plan is checked before a playlist is created.
    chosen = [(chosen_id(track, target), describe(track)) for track in approved]
    limit = getattr(target, 'MAX_PLAYLIST_TRACKS', None)
    if limit and len(chosen) > limit:
        raise ValueError(f'{target.LABEL} playlists hold at most {limit} songs; the plan has {len(chosen)} approved.')
    created_now = not plan.get('target_playlist_id')
    if created_now:
        plan['target_playlist_id'] = target.create_playlist(plan['name'])
        save(args.plan, plan)
    playlist_id = plan['target_playlist_id']
    # Reconcile with what is already there, so a rerun after an error continues without duplicates.
    existing = {} if created_now else target.playlist_counts(playlist_id)
    seen, missing = {}, []
    for item, label in chosen:
        seen[item] = seen.get(item, 0) + 1
        if seen[item] > existing.get(item, 0):
            missing.append((item, label))
    # add_items returns the songs the platform refused, so one dead link does not block the rest.
    skipped = target.add_items(playlist_id, missing) or []
    save(args.plan, plan)
    print(f'{len(missing) - len(skipped)} added, {len(approved) - len(missing)} were already there.')
    if skipped:
        print(f'{len(skipped)} could not be added:')
        for label, reason in skipped:
            print(f'  {label}: {reason}')
        print('Choose another candidate for them in the plan file (or set "approved" to false) and run apply again.')
    url = target.playlist_url(playlist_id)
    print(url)
    return {'added': len(missing) - len(skipped), 'already': len(approved) - len(missing),
            'skipped': [list(entry) for entry in skipped], 'url': url}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='Search only; saves editable candidates')
    plan.add_argument('playlist', help='Spotify, YouTube or SoundCloud playlist link')
    plan.add_argument('--to', choices=sorted(platforms.PLATFORMS), help='Target platform (default: the other one)')
    plan.add_argument('--plan', default='plan.json')
    plan.add_argument('--force', action='store_true', help='Overwrite an existing plan file')
    plan.add_argument('--no-cache', action='store_true',
                      help='Search again instead of reusing answers saved in the last 30 days')
    review = commands.add_parser('review', help='Show songs that need a decision')
    review.add_argument('--plan', default='plan.json')
    apply = commands.add_parser('apply', help='Create a private playlist from approved matches')
    apply.add_argument('--plan', default='plan.json')
    args = parser.parse_args()
    try:
        {'plan': make_plan, 'review': review_plan, 'apply': apply_plan}[args.command](args)
    except KeyboardInterrupt:
        parser.exit(130, '\nStopped. Progress is saved in the plan file; the same command continues.\n')
    except Exception as error:
        note = '\nProgress is saved in the plan file; check it before retrying.' \
            if args.command != 'review' and Path(args.plan).exists() else ''
        parser.exit(1, f'{type(error).__name__}: {error}{note}\n')


if __name__ == '__main__':
    main()
