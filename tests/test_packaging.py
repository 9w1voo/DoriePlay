"""The build recipes agree with the code: version, program name, the Store package's files.

Run from the project folder:  python -m unittest discover tests
"""
import re
import sys
import unittest
from xml.etree import ElementTree
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import version  # noqa: E402


class StorePackage(unittest.TestCase):
    def test_the_msix_manifest_matches_the_build(self):
        manifest = (PROJECT / 'packaging' / 'windows' / 'AppxManifest.xml').read_text(encoding='utf-8')
        ElementTree.fromstring(manifest.encode('utf-8'))   # well-formed XML, or makeappx refuses it
        # the Store wants four numbers and keeps the last one for itself
        self.assertRegex(manifest, rf'<Identity [^>]*Version="{re.escape(version.VERSION)}\.0"')
        spec = (PROJECT / 'packaging' / 'dorieplay.spec').read_text(encoding='utf-8')
        self.assertIn("name='DoriePlay'", spec)
        self.assertIn(r'Executable="DoriePlay\DoriePlay.exe"', manifest)
        self.assertIn('EntryPoint="Windows.FullTrustApplication"', manifest)
        self.assertIn('<rescap:Capability Name="runFullTrust"/>', manifest)
        assets = re.findall(r'"(Assets\\[^"]+\.png)"|>(Assets\\[^<]+\.png)<', manifest)
        self.assertGreaterEqual(len(assets), 3)
        for found in assets:
            path = PROJECT / 'packaging' / 'windows' / ''.join(found).replace('\\', '/')
            self.assertTrue(path.is_file(), path)


if __name__ == '__main__':
    unittest.main()
