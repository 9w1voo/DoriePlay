"""Platform-independent matching: text normalization, scoring and confidence.

A source track is {'title', 'artists', 'duration_ms', ...}.
A candidate on the target platform is
{'id', 'title', 'artist', 'seconds', 'topic', 'url'}:
  title  - text shown for the item (a YouTube video title may also contain the artist);
  artist - the uploader or artist names (YouTube channel, Spotify artists joined);
  topic  - True for YouTube auto-generated "... - Topic" uploads of label audio;
  isrc   - optional international recording code (Spotify, SoundCloud); equal codes = same recording.
"""
import html
import re
import unicodedata
from difflib import SequenceMatcher

# Other versions of a song and videos about a song (reviews, reactions) rather than the song itself.
VARIANTS = ('live', 'remix', 'cover', 'karaoke', 'instrumental', 'sped up', 'speed up', 'slowed',
            'reverb', 'nightcore', 'acoustic', 'snippet', 'reaction', 'review', 'analysis',
            'разбор', 'реакция', 'обзор', 'смотрит', 'оценивает', 'розбір', 'реакція', 'огляд')
CYRILLIC = dict(zip('абвгґдеёєжзиіїйклмнопрстуфхцчшщъыьэюя',
                    ['a', 'b', 'v', 'g', 'g', 'd', 'e', 'e', 'ye', 'zh', 'z', 'i', 'i', 'yi', 'y', 'k', 'l',
                     'm', 'n', 'o', 'p', 'r', 's', 't', 'u', 'f', 'kh', 'ts', 'ch', 'sh', 'shch', '', 'y',
                     '', 'e', 'yu', 'ya']))
UPLOAD_LABELS = ('official music video', 'official video', 'official audio', 'lyric video',
                 'lyrics', 'audio', 'visualizer', 'feat', 'ft')


def normalize(value):
    """Lowercase words only, Cyrillic transliterated so 'Иван Дорн' equals 'Ivan Dorn'."""
    value = unicodedata.normalize('NFKC', html.unescape(value)).casefold()
    value = ''.join(CYRILLIC.get(char, char) for char in value)
    return ' '.join(re.findall(r'\w+', value))


STRONG = 0.88   # a candidate this good is approved automatically when no other recording is close

# Labels that say nothing about which recording it is: "(feat. Drake)", "- Remastered 2011", "(Mono)".
# Versions that ARE another recording (live, remix, radio edit...) are deliberately not here.
_NEUTRAL = (r'(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+(?:version|edition))?(?:\s+\d{4})?'
            r'|(?:single|album|original|explicit|clean)\s+version|mono|stereo|explicit|bonus\s+track')
_FEAT = r'(?:feat|ft|featuring)\b\.?'
NEUTRAL_PART = re.compile(rf'\s*[(\[]\s*(?:{_NEUTRAL})\s*[)\]]|\s+[-–—]+\s+(?:{_NEUTRAL})\s*$', re.IGNORECASE)
FEAT_BRACKET = re.compile(rf'\s*[(\[]\s*{_FEAT}\s[^)\]]*[)\]]', re.IGNORECASE)
FEAT_TAIL = re.compile(rf'\s+{_FEAT}\s.*$', re.IGNORECASE)


def core_title(title, trailing_feat=False):
    """Title without featured artists and neutral labels: 'Sicko Mode (feat. Drake)' -> 'Sicko Mode',
    'Heroes - 2017 Remaster' -> 'Heroes'. trailing_feat also cuts an unbracketed 'Song feat. X';
    use it only for a song title, never for an upload title like 'A feat. B - Song'."""
    core = NEUTRAL_PART.sub('', FEAT_BRACKET.sub('', NEUTRAL_PART.sub('', title)))
    if trailing_feat:
        core = FEAT_TAIL.sub('', core)
    return core.strip() or title


def queries(track):
    """Search texts from the most to the least specific: all artists, the lead artist,
    the lead artist in Latin letters (catalogues often list 'Ivan Dorn', not 'Иван Дорн'), title only."""
    title = core_title(track['title'], trailing_feat=True)
    artists = [a for a in track['artists'] if a]
    lead = artists[0] if artists else ''
    options = [' '.join(artists + [title]), f'{lead} {title}']
    if any(char in CYRILLIC for char in lead.casefold()):
        options.append(f'{normalize(lead)} {title}')
    options.append(title)
    unique = {}
    for option in options:
        text = ' '.join(option.split())
        if text:
            unique.setdefault(text.casefold(), text)
    return list(unique.values())


def gather(track, texts, fetch):
    """Candidates from fetch(text) for each search text in turn, without duplicates;
    stops as soon as one of them is a strong match, so the usual song costs one request."""
    found, seen = [], set()
    for text in texts:
        for candidate in fetch(text):
            if candidate['id'] not in seen:
                seen.add(candidate['id'])
                found.append(candidate)
        if any(score(track, candidate) >= STRONG for candidate in found):
            break
    return found


def has_phrase(text, phrase):
    """Whole-word match: 'live' must not match 'oliver' or 'alive'."""
    return f' {phrase} ' in f' {text} '


def clean_title(track, candidate):
    """Candidate title without the track's artist names and common upload labels."""
    clean = ' ' + normalize(core_title(candidate['title'])) + ' '
    names = [normalize(' '.join(track['artists']))] + [normalize(a) for a in track['artists']]
    for part in [n for n in names if n] + list(UPLOAD_LABELS):
        clean = clean.replace(f' {part} ', ' ')
    return ' '.join(clean.split())


def same_isrc(a, b):
    return bool(a.get('isrc')) and (a.get('isrc') or '').upper() == (b.get('isrc') or '').upper()


def score(track, candidate):
    """Heuristic 0..1: title similarity, artist overlap and duration, minus version penalties.
    The same ISRC (international code of a recording) means the same recording: 1.0."""
    if same_isrc(track, candidate):
        return 1.0
    title = normalize(candidate['title'])
    artist = normalize(' '.join(track['artists']))
    song = normalize(core_title(track['title'], trailing_feat=True))
    similarity = SequenceMatcher(None, song, clean_title(track, candidate)).ratio()
    context = set((title + ' ' + normalize(candidate['artist'])).split())
    artist_words = set(artist.split())
    artist_match = len(artist_words & context) / max(1, len(artist_words))
    # Uploads often name only the lead artist of a track with several: the lead counts for most of it.
    lead_words = set(normalize(track['artists'][0]).split()) if track['artists'] else set()
    if lead_words:
        lead_match = len(lead_words & context) / len(lead_words)
        artist_match = max(artist_match, 0.7 * lead_match + 0.3 * artist_match)
    seconds = candidate['seconds']
    expected = track['duration_ms'] / 1000
    delta = abs(seconds - expected)
    timing = max(0, 1 - delta / 30)
    # YouTube auto-generated "... - Topic" channels carry the label's studio audio; an exact length
    # match there is strong evidence even when the channel name is not the artist (e.g. "Release - Topic").
    if candidate.get('topic') and delta <= 2:
        artist_match = max(artist_match, 0.8)
    penalty = sum(0.25 for variant in VARIANTS
                  if has_phrase(title, normalize(variant)) and not has_phrase(song, normalize(variant)))
    # Full albums, mixes and short snippets are not the track, whatever their title says.
    if seconds and not 0.6 * expected <= seconds <= 1.6 * expected + 30:
        penalty += 0.5
    return round(max(0, 0.55 * similarity + 0.3 * artist_match + 0.15 * timing - penalty), 3)


def rank(track, candidates):
    """Score and sort candidates; on equal scores prefer label "- Topic" uploads."""
    ranked = [{**c, 'score': score(track, c), 'clean': clean_title(track, c), 'exact': same_isrc(track, c)}
              for c in candidates]
    return sorted(ranked, key=lambda c: (c['score'], bool(c.get('topic'))), reverse=True)


def is_confident(candidates):
    """Best match is strong and no different recording scores close to it."""
    if not candidates or candidates[0]['score'] < STRONG:
        return False
    best = candidates[0]
    if best.get('exact'):   # the source's own ISRC: this IS the recording, re-uploads next to it don't matter
        return True
    for other in candidates[1:]:
        same_recording = (same_isrc(best, other) or
                          other['clean'] == best['clean'] and abs(other['seconds'] - best['seconds']) <= 3)
        if other['score'] > best['score'] - 0.08 and not same_recording:
            return False
    return True


# --- Turning a YouTube video into a track -------------------------------------------------------

# Bracketed parts or trailing "| ..." parts that describe the upload, not the recording.
JUNK_WORDS = {'official', 'video', 'audio', 'lyrics', 'lyric', 'visualizer', 'visualiser', 'hd', 'hq',
              '4k', 'mv', 'clip', 'klip', 'tekst', 'prod', 'premiere', 'premera', 'music', 'unofficial',
              'subtitles', 'eng', 'sub', 'explicit', 'clean', 'album', 'version'}
VERSION_WORDS = {'live', 'remix', 'acoustic', 'cover', 'instrumental', 'slowed', 'sped', 'speed',
                 'reverb', 'nightcore', 'karaoke', 'remastered', 'remaster', 'edit', 'mix'}
DASH = re.compile(r'\s+[-–—]+\s+')
ARTIST_SPLIT = re.compile(r'\s*,\s*|\s+&\s+|\s+(?:feat|ft)\.?\s+|\s+x\s+(?=[A-ZА-ЯЁІЇЄ])', re.IGNORECASE)
FEAT = re.compile(r'\s*[(\[]?\s*(?:feat|ft)\.?\s+([^()\[\]]+?)\s*[)\]]?\s*$', re.IGNORECASE)


def _is_junk(text):
    words = set(normalize(text).split())
    return bool(words & JUNK_WORDS) and not words & VERSION_WORDS


def parse_video(title, channel):
    """Best guess of (artists, song title) from a YouTube video title and channel name.

    'Земфира - Topic' + 'Мачо'            -> (['Земфира'], 'Мачо')
    'T-Fest, Иван Дорн - Прониклась мной (Official Audio)' -> (['T-Fest', 'Иван Дорн'], 'Прониклась мной')
    'Slipknot - Arsenal (Live)'           -> (['Slipknot'], 'Arsenal (Live)')
    """
    title = html.unescape(title).strip()
    title = re.sub(r'\s*#\S+', '', title)                          # hashtags
    title = re.sub(r'\.(avi|mp4|mp3|mkv|flv|wmv)$', '', title, flags=re.IGNORECASE)
    parts = [p.strip() for p in title.split('|')]                  # "Song | Lyrics" / "Song |Текст|"
    title = parts[0] + ''.join(f' | {p}' for p in parts[1:] if p and not _is_junk(p))
    title = re.sub(r'\s*([(\[【][^)\]】]*[)\]】])',                   # "(Official Video)", "[HD]"
                   lambda m: '' if _is_junk(m.group(1)[1:-1]) else ' ' + m.group(1), title).strip()
    title = re.sub(r'(?i)(?<!\w)(official (music )?video|official audio|lyric video|lyrics?|текст)(?!\w)[/ ]*',
                   '', title).strip()
    if channel.endswith(' - Topic'):
        artists, song = [channel[:-len(' - Topic')].strip()], title
    elif DASH.search(title):
        artist_part, song = DASH.split(title, maxsplit=1)
        artists = [a.strip() for a in ARTIST_SPLIT.split(artist_part) if a.strip()]
    else:
        artists, song = [re.sub(r'\s*(VEVO|Official)$', '', channel, flags=re.IGNORECASE).strip()], title
    feat = FEAT.search(song)
    if feat:
        artists += [a.strip() for a in ARTIST_SPLIT.split(feat.group(1)) if a.strip()]
        song = song[:feat.start()]
    song = re.sub(r'\s*\(\s*\)|\s*\[\s*\]', '', song)
    return artists or [channel], ' '.join(song.split()) or title
