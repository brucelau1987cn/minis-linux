import io
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from minis_search import search_web


class Response(io.BytesIO):
    def __init__(self, data, status):
        super().__init__(data)
        self.status = status


class SearchTest(unittest.TestCase):
    def search(self, page, query='Debian 安全', status=200):
        response = Response(page.encode(), status)
        opener = Mock()
        opener.open.return_value = response
        result = search_web(query, opener=opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).hostname, 'lite.duckduckgo.com')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {'q': [query]})
        self.assertIsNone(request.get_header('Authorization'))
        return result

    def test_returns_title_link_and_snippet_and_skips_unsafe_link(self):
        result = self.search('''<table><tr><td><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.debian.org%2Fsecurity%2F&amp;rut=fixture">Debian <b>Security</b></a></td></tr>
<tr><td class="result-snippet">Official &amp; current</td></tr>
<tr><td><a class="result-link" href="javascript:private()">Bad link</a></td></tr></table>''')
        self.assertIn('Debian Security', result)
        self.assertIn('https://www.debian.org/security/', result)
        self.assertIn('Official & current', result)
        self.assertNotIn('private()', result)

    def test_challenge_is_not_reported_as_empty_results(self):
        for status in (200, 202):
            with self.subTest(status=status):
                self.assertIn('暂不可用', self.search('<div class="anomaly-modal">challenge</div>', status=status))

    def test_limits_results(self):
        page = ''.join(f'<a class="result-link" href="https://example.org/{i}">Page {i}</a>' for i in range(7))
        result = self.search(page)
        self.assertIn('5. Page 4', result)
        self.assertNotIn('Page 5', result)

    def test_empty_query_does_not_contact_network(self):
        opener = Mock()
        self.assertIn('/search', search_web('  ', opener=opener))
        opener.open.assert_not_called()
