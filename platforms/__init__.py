"""Each platform module provides:
  NAME, LABEL, owns(url), item_url(id), playlist_url(id)
  read_playlist(url) -> (name, tracks)            source side
  search(track) -> candidates                      target side (see matching.py for shapes)
  create_playlist(name) -> id
  playlist_counts(id) -> {item_id: count}
  add_items(id, [(item_id, label), ...]) -> [(label, reason)] for items the platform refused
  playlist_key(url) -> stable id of a playlist link (resume works when the link is copied again)
  item_id(text)     optional: id from an item link pasted into "match_id"
  MAX_PLAYLIST_TRACKS  optional: playlist size limit, checked before a playlist is created
Adding a platform = adding a module here and to PLATFORMS.
"""
from . import soundcloud, spotify, youtube

PLATFORMS = {module.NAME: module for module in (spotify, youtube, soundcloud)}


def get(name):
    if name not in PLATFORMS:
        raise ValueError(f'Unknown platform {name!r}. Available: {", ".join(PLATFORMS)}.')
    return PLATFORMS[name]


def detect(url):
    for module in PLATFORMS.values():
        if module.owns(url):
            return module
    raise ValueError('Cannot tell the platform from this link. Use a Spotify, YouTube or SoundCloud playlist URL.')
