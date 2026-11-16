"""Dorie Play's version, and whether a newer one is out (for copies downloaded from the site)."""
import re
import sys

VERSION = '1.0.0'
# Windows copies come only from the Microsoft Store, which updates them: no check and no link there
CHANNEL = 'store' if sys.platform == 'win32' and getattr(sys, 'frozen', False) else 'site'
LATEST = 'https://api.github.com/repos/9w1voo/DoriePlay/releases/latest'


def latest():
    """The newest release's number from GitHub, or None: offline, no release yet, or an unexpected answer."""
    try:
        import requests
        response = requests.get(LATEST, timeout=5, headers={'Accept': 'application/vnd.github+json'})
        response.raise_for_status()
        match = re.fullmatch(r'v(\d+\.\d+\.\d+)', response.json().get('tag_name') or '')
    except Exception:   # an update notice is never worth an error
        return None
    return match.group(1) if match else None


def newer(found, current=VERSION):
    if not found:
        return False
    return tuple(map(int, found.split('.'))) > tuple(map(int, current.split('.')))


def check(app):
    """Called by the window at start, in a background thread; sets app.update when a newer version is out."""
    if CHANNEL != 'site' or app.demo:
        return
    found = latest()
    if newer(found):
        app.update = {'version': found}
