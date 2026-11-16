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


def mascot_poses():
    """The poses in web/mascot.js (written by design/mascot/poses.py --web): name -> (ms, hop, frames)."""
    source = (WEB / 'mascot.js').read_text(encoding='utf-8')
    return {name: ([int(x) for x in ms.split(',') if x.strip()], hop == 'true', re.findall(r"'([^']*)'", frames))
            for name, ms, hop, frames in re.findall(
                r"^ +(\w+): \{ ms: \[([\d, ]*)\], hop: (true|false), frames: \[(.*)\] \},$", source, re.M)}


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


class Mascot(unittest.TestCase):
    def test_every_frame_is_24_by_23_in_the_logo_colours(self):
        poses = mascot_poses()
        self.assertEqual(sorted(poses), sorted(['stand', 'jump', 'wave', 'point_up', 'thumbs_up', 'shrug',
                                                'think', 'listen', 'celebrate', 'yawn']))
        for name, (ms, hop, frames) in poses.items():
            self.assertEqual(len(ms), len(frames), name)
            for frame in frames:
                rows = [re.sub(r'(\d+)(\D)', lambda m: m[2] * int(m[1]), row) for row in frame.split('|')]
                self.assertEqual(len(rows), 23, name)
                self.assertEqual({len(row) for row in rows}, {24}, name)
                self.assertLessEqual(set(''.join(rows)), set('.KVOWSHGP'), name)
        self.assertEqual(sorted(n for n, (ms, hop, f) in poses.items() if hop), ['celebrate', 'jump'])

    def test_the_mascot_never_writes_markup_or_style_attributes(self):
        for name in ('mascot.js', 'app.js'):
            source = (WEB / name).read_text(encoding='utf-8')
            self.assertNotIn('innerHTML', source, name)
            self.assertNotIn("setAttribute('style'", source, name)


class Links(unittest.TestCase):
    def test_every_outside_link_opens_in_the_system_browser(self):
        page = (WEB / 'app.js').read_text(encoding='utf-8')
        links = re.findall(r"el\('a', \{([^}]*)\}", page)
        self.assertGreater(len(links), 8)
        for attrs in links:
            if re.search(r"href: ['`]#", attrs):
                continue
            self.assertIn("target: '_blank'", attrs, attrs)
            self.assertIn("rel: 'noopener noreferrer'", attrs, attrs)
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(re.findall(r'<a [^>]*href="(?!#)', html), [])

    def test_the_spotify_logo_has_a_name_in_both_looks(self):
        page = (WEB / 'app.js').read_text(encoding='utf-8')
        body = re.search(r'const spotifyMark = .*?\n(?=const )', page, re.S).group(0)
        self.assertEqual(body.count("alt: 'Spotify'"), 2, body)
        self.assertNotIn("alt: ''", body)

    def test_the_soundcloud_logo_has_a_name_in_both_looks(self):
        page = (WEB / 'app.js').read_text(encoding='utf-8')
        body = re.search(r'const soundcloudMark = .*?\n(?=const )', page, re.S).group(0)
        self.assertEqual(body.count("alt: t('scLogo')"), 2, body)
        css = (WEB / 'app.css').read_text(encoding='utf-8')
        self.assertIn('[data-theme="glass"] :is(.sp-black, .sc-black), '
                      '[data-theme="glass-light"] :is(.sp-white, .sc-white) { display: none; }', css)

    def test_the_soundcloud_header_link_is_written_out_for_the_links_test(self):
        page = (WEB / 'app.js').read_text(encoding='utf-8')
        self.assertIn("el('a', { class: 'sc-mark', href: 'https://soundcloud.com'", page)


def top_level_arguments(source, start):
    """Each argument of the call whose '(' is at source[start]: (text, has a top-level && or ||)."""
    args, depth, quote, current, condition = [], 0, None, '', False
    text = source[start + 1:]
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == '\\':
                current += text[i:i + 2]
                i += 2
                continue
            if char == quote:
                quote = None
        elif text.startswith('//', i):   # a comment runs to the end of the line
            i = text.index('\n', i)
            continue
        elif char in '\'"`':
            quote = char
        elif char in '([{':
            depth += 1
        elif char in ')]}':
            if depth == 0:
                return args + [(current, condition)]
            depth -= 1
        elif char == ',' and depth == 0:
            args.append((current, condition))
            current, condition = '', False
            i += 1
            continue
        elif depth == 0 and text[i:i + 2] in ('&&', '||'):
            condition = True
        current += char
        i += 1
    raise ValueError('unbalanced call')


class Children(unittest.TestCase):
    def test_no_condition_is_handed_straight_to_replace_children(self):
        # el() skips false children, but node.replaceChildren(false) (or append, before, ...) shows the word "false"
        page = (WEB / 'app.js').read_text(encoding='utf-8')
        calls = [m.end() - 1 for m in re.finditer(r'\.(?:replaceChildren|append|prepend|replaceWith|before|after)\(', page)]
        self.assertGreater(len(calls), 5)
        for start in calls:
            for argument, condition in top_level_arguments(page, start):
                self.assertFalse(condition, argument.strip()[:80])
                if argument.strip().startswith('...['):   # a list may hold conditions only when it is filtered
                    self.assertTrue(argument.strip().endswith('].filter(Boolean)'), argument.strip()[:80])


if __name__ == '__main__':
    unittest.main()
