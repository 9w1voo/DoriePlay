"""Where Dorie Play keeps keys, sign-ins, plans, the search cache and settings.

Run from the code: the code folder, as always. Packaged app: the system's folder for app data, because an
installed program must never write into its own folder. DORIEPLAY_HOME overrides both (tests, a second copy).
Importing this module creates and reads nothing.
"""
import os
import sys
from pathlib import Path

APP_NAME = 'Dorie Play'


def data_folder():
    if os.environ.get('DORIEPLAY_HOME'):
        return Path(os.environ['DORIEPLAY_HOME']).expanduser()
    if getattr(sys, 'frozen', False):   # set by the packager
        if sys.platform == 'darwin':
            return Path.home() / 'Library' / 'Application Support' / APP_NAME
        if sys.platform == 'win32':
            return Path(os.environ['APPDATA']) / APP_NAME
        return Path.home() / '.local' / 'share' / APP_NAME
    return Path(__file__).resolve().parent


def ensure(folder):
    """Create the folder readable by this user only; an existing folder keeps its permissions."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder
