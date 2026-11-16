<img src="web/logo.svg" alt="Dorie Play logo" width="96">

# Dorie Play

Move your playlists between **Spotify**, **YouTube** and **SoundCloud** — in any direction — and
check every song before anything is transferred. Public **Deezer** playlists can be moved out too.

[Українською](README.uk.md)

Dorie Play runs on your own computer. It first only *searches*; you review the matches; only then
it creates a new playlist from the songs you approved (private on YouTube and SoundCloud; Spotify makes
playlists created by apps public, see below). Nothing is transferred blindly,
and nothing is sent to anyone except the music services themselves.

![Review screen: each song next to its match, with the ones that need a decision on top](docs/review.png)

![Start screen: paste a playlist link and choose where to transfer it](docs/start.png)

> Not affiliated with Spotify, YouTube, Google, SoundCloud or Deezer. The screenshots show the built-in demo with an invented playlist.

## What you need (read this first)

Dorie Play runs on your own computer. Spotify and YouTube need **your own** free API keys; SoundCloud needs only your
SoundCloud account. Two small Dorie Play servers help: the song base, which saves YouTube searches (you can turn it off
under Services), and the SoundCloud sign-in server. Neither keeps logs.

| Service | What you need |
|---|---|
| Spotify | A free developer app (Client ID). The app owner needs Spotify Premium. Only playlists you own or collaborate on can be read. |
| YouTube | A Google Cloud project with YouTube Data API v3. About **100 searches a day**, so a long playlist going *to* YouTube takes several days (the program continues where it stopped). |
| SoundCloud | Nothing but your SoundCloud account: you sign in the first time you use it. |
| Deezer | Nothing: public playlists are read without a key. Deezer is a source only, since it no longer gives out the keys needed to create playlists. |

You only need keys for the two services you transfer between. Python 3.10 or newer.

## Try it without keys

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py --demo
```

The demo opens in your browser with an invented playlist and fake services, so you can click
through the whole flow.

## Use it

```sh
python server.py
```

The page opens at `http://127.0.0.1:8765` and is reachable only from your computer.

1. Paste a playlist link and choose where to transfer it. No access to the source playlist (for example,
   Spotify without your own key)? Paste the songs instead, one per line as `Artist - Title`; a length at the
   end (`3:45`) helps matching. Or open your Spotify data file: on spotify.com, Account → Privacy →
   Download your data → Account data; Spotify emails an archive. Unzip it and choose `Playlist1.json` (your
   playlists) or `YourLibrary.json` (liked songs) with **Open a Spotify data file**. The file is read in the
   page and is not sent anywhere.
2. Wait for the search. Confident matches are approved automatically.
3. Review the songs that need a decision: listen, pick a candidate, or leave the song out.
4. Press **Create private playlist** (for Spotify: **Create playlist**) and confirm. Spotify ignores the
   "private" setting from apps: anyone with the link can open the new playlist. To hide it, open it in
   Spotify → ⋯ → **Make private**.

The interface is available in English, Ukrainian, Dutch, Russian and Spanish.

Prefer the terminal? The same three steps:

```sh
python dorieplay.py plan 'PLAYLIST_LINK' --to youtube --plan my.json
python dorieplay.py review --plan my.json
python dorieplay.py apply --plan my.json
```

## Connecting the services

The easiest way: on the start page, under **Services**, press **Set up** next to a service, follow the
steps and paste the key there. The program checks that it looks right and saves it next to itself,
readable only by your user. The steps below do the same by hand.

**Spotify**

1. Create an app at https://developer.spotify.com/dashboard and tick *Web API*.
2. Add the redirect URI `http://127.0.0.1:8888/callback`.
3. Paste the Client ID on the start page, or set it before starting the program (no client secret is needed):

```sh
export SPOTIPY_CLIENT_ID='your_client_id'
```

**YouTube**

1. Create a project at https://console.cloud.google.com/ and enable *YouTube Data API v3*.
2. Google Auth Platform → Audience: External; add yourself as a test user.
3. Clients → Create client → *Desktop app* → download the JSON and save it as
   `google-client.json` in the program folder.

While the Google app is in *Testing*, Google asks you to sign in again about once a week.

**SoundCloud** needs no key: Dorie Play's own SoundCloud app is used, and its secret stays on the sign-in server.

The first time a service is used, a sign-in page opens in your browser. Sign in on the same
computer that runs the program.

## Good to know

- Search answers are kept for 30 days in `.search-cache.json`, so planning the same songs again
  does not spend the daily YouTube limit. Add `--no-cache` to `dorieplay.py plan` to search again.
- Running the transfer twice adds only what is missing; it does not create duplicates.
- Songs a service refuses (for example a deleted video) are skipped and listed at the end.
- Matching is a heuristic (title, artist, length). Live versions, remixes, covers and sped-up edits
  are not approved automatically. Cyrillic names are transliterated, so "Иван Дорн" matches
  "Ivan Dorn".
- Spotify-generated playlists (Daily Mix, Discover Weekly) and YouTube Mixes cannot be read: copy
  the songs into a playlist of your own first.
- A SoundCloud playlist holds at most 500 tracks.

### Song base (optional, transfers to YouTube)

YouTube allows about 100 searches a day for the whole program. The song base answers many songs
without a search: it holds the YouTube videos that Discogs editors linked to releases (Discogs data
dump, CC0; 5.8 million videos in November 2026).

```sh
python songbase.py build discogs_20261101_masters.xml.gz   # monthly dump from https://data.discogs.com/
python songbase.py serve                                    # http://127.0.0.1:8771
DORIEPLAY_SONGBASE=http://127.0.0.1:8771 python server.py
```

Only the title, artists and length of each song are sent to the base. Many videos linked on Discogs have
been deleted or blocked since, so the base's videos are first checked with YouTube (1 quota unit, not a
search). A song is taken from the base only when one of its playable videos is a confident match; such
candidates are marked "from Discogs, not a YouTube search", and otherwise YouTube is searched as before.
The base server writes no logs.

## Your data

Keys, sign-in tokens, plans and the search cache stay in the program folder on your computer.
**Never publish** `google-client.json`, `soundcloud-client.json`, `spotify-client.json`, any `*token*`
file, your plans or your Spotify Client ID. The included `.gitignore` already excludes them.
See also the [privacy policy](docs/privacy.html) (draft).

## Development

```sh
python -m unittest discover tests
```

The tests need no network and no keys. Adding a service means adding one module in `platforms/`
with the functions described in `platforms/__init__.py`.

## License

[PolyForm Noncommercial 1.0.0](LICENSE). In short: you may use, study and change Dorie Play for
personal and other non-commercial purposes, free of charge. Selling it, or using it in a paid
product or service, needs written permission from the author. The source is published so you can
see exactly what runs on your computer; this is "source-available", not an open-source licence.

Pull requests are not accepted at the moment; bug reports and ideas in Issues are welcome.
