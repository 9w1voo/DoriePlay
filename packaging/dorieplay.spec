# PyInstaller recipe for Dorie Play: a folder build ("onedir") with the window (window.py) as the program.
#   python -m PyInstaller packaging/dorieplay.spec     ->  dist/Dorie Play.app (Mac) or dist/DoriePlay/ (Windows)
# Windows wraps that folder into an MSIX for the Microsoft Store (packaging/windows/build.ps1).
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
import version  # noqa: E402

mac, win = sys.platform == 'darwin', sys.platform == 'win32'
other_system = (['webview.platforms.winforms', 'webview.platforms.edgechromium', 'webview.platforms.mshtml', 'clr']
                if mac else ['webview.platforms.cocoa'])
icon = str(ROOT / 'packaging' / ('windows/icon.ico' if win else 'mac/icon.icns'))

a = Analysis([str(ROOT / 'window.py')], pathex=[str(ROOT)],
             datas=[(str(ROOT / 'web'), 'web'), (str(ROOT / 'LICENSE'), '.')],
             hiddenimports=['demo'],   # imported inside server.make_app
             excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'qtpy', 'gi', 'cefpython3',
                       'webview.platforms.qt', 'webview.platforms.gtk', 'webview.platforms.cef',
                       'webview.platforms.android', *other_system])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [('X utf8', None, 'OPTION')], exclude_binaries=True, name='DoriePlay',
          console=False, argv_emulation=False, upx=False, icon=icon)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name='DoriePlay')
if mac:
    app = BUNDLE(coll, name='Dorie Play.app', icon=icon, bundle_identifier='com.dorieplay.app', version=version.VERSION,
                 info_plist={'CFBundleShortVersionString': version.VERSION, 'CFBundleVersion': version.VERSION,
                             'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '11.0',
                             'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True}})
