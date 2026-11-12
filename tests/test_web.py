"""The interface texts: every language has every text, with the same {placeholders} as English.

Run from the project folder:  python -m unittest discover tests
"""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / 'web'
TEXT = re.compile(r"(\w+): '((?:[^'\\]|\\.)*)'")


def languages():
    source = (WEB / 'i18n.js').read_text(encoding='utf-8')
    blocks = re.findall(r'\n  (\w+): \{\n(.*?)\n  \},', source, flags=re.DOTALL)
    # the plural forms of "song" ({one: ..., other: ...}) are not texts of their own
    return {code: (dict(TEXT.findall(re.sub(r'songs: \{[^}]*\},', '', body))), body) for code, body in blocks}


class Texts(unittest.TestCase):
    def test_english_is_first_and_all_five_languages_are_there(self):
        self.assertEqual(list(languages()), ['en', 'uk', 'nl', 'ru', 'es'])

    def test_every_language_has_every_text_with_the_same_placeholders(self):
        found = languages()
        english = found['en'][0]
        self.assertGreater(len(english), 80)
        for code, (texts, body) in found.items():
            self.assertEqual(sorted(texts), sorted(english), code)
            for key, text in texts.items():
                self.assertTrue(text.strip(), f'{code}.{key} is empty')
                self.assertEqual(sorted(re.findall(r'\{\w+\}', text)), sorted(re.findall(r'\{\w+\}', english[key])),
                                 f'{code}.{key}')
            forms = re.search(r'songs: \{([^}]*)\}', body)
            self.assertIsNotNone(forms, code)
            self.assertIn('other:', forms.group(1), code)

    def test_every_text_the_page_asks_for_exists(self):
        english = languages()['en'][0]
        page = (WEB / 'app.js').read_text(encoding='utf-8')
        asked = set(re.findall(r"\bt\('(\w+)'", page))
        asked |= set(re.findall(r"'((?:f|s|theme)[A-Z]\w*)'", page))   # filter, status and look names
        asked |= {'addBody', 'createBody'}
        self.assertGreater(len(asked), 70)
        self.assertEqual(sorted(asked - set(english)), [])
        self.assertEqual(sorted(set(english) - asked - {'_name', 'look'}), [])

    def test_no_interface_text_is_left_outside_the_dictionary(self):
        page = (WEB / 'app.js').read_text(encoding='utf-8')
        code = [line for line in page.split('\n') if not line.strip().startswith(('//', '/*', '*'))]
        code = [re.sub(r'//.*$', '', line) for line in code]
        self.assertEqual([line for line in code if re.search('[А-Яа-яІіЇїЄєҐґ]', line)], [])


if __name__ == '__main__':
    unittest.main()
