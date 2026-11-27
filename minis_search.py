"""Explicit, keyless DuckDuckGo Lite search. Never fetch result URLs."""
from html.parser import HTMLParser
import http.client
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from minis import NoRedirect, safe_display

SEARCH_URL = 'https://lite.duckduckgo.com/lite/'
MAX_PAGE = 512 * 1024
MAX_RESULTS = 5


def result_url(href):
    try:
        u = urlsplit(href)
        if (u.netloc in ('duckduckgo.com', 'lite.duckduckgo.com') and u.path == '/l/'
                and u.scheme in ('', 'https')):
            href = parse_qs(u.query, max_num_fields=16).get('uddg', [''])[0]
            u = urlsplit(href)
        if (u.scheme in ('http', 'https') and u.hostname and u.username is None and u.password is None
                and len(href) <= 1024 and all(c.isprintable() and not c.isspace() for c in href)):
            return href
    except ValueError:
        pass
    return ''


class LiteResults(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.seen = set()
        self.pending = ''
        self.active = ''
        self.parts = []
        self.attach_snippet = False
        self.challenge = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get('class') or '').split()
        if ('anomaly-modal' in ' '.join(classes)
                or attrs.get('id') in ('challenge-form', 'img-form')):
            self.challenge = True
        if tag == 'a' and 'result-link' in classes:
            self.pending = result_url(attrs.get('href') or '')
            self.active, self.parts = 'a', []
            self.attach_snippet = False
        elif tag == 'td' and 'result-snippet' in classes and self.attach_snippet:
            self.active, self.parts = 'td', []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if not self.active or tag != self.active:
            return
        text = safe_display(' '.join(''.join(self.parts).split()))
        if tag == 'a':
            if self.pending and text and self.pending not in self.seen and len(self.rows) < MAX_RESULTS:
                self.rows.append([text[:180], self.pending, ''])
                self.seen.add(self.pending)
                self.attach_snippet = True
        else:
            self.rows[-1][2] = text[:280]
            self.attach_snippet = False
        self.active, self.parts = '', []


def search_web(query, *, opener=None):
    query = query.strip()
    if not query or len(query) > 240 or not query.isprintable():
        return '用法：/search 关键词（最多 240 个字符，不带控制字符）。'
    request = Request(SEARCH_URL + '?' + urlencode({'q': query}), headers={
        'Accept': 'text/html', 'User-Agent': 'Minis-Linux (+https://github.com/brucelau1987cn/minis-linux)',
    })
    if opener is None:
        opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        deadline = monotonic() + 8
        with opener.open(request, timeout=5) as response:
            if response.status != 200:
                return f'DuckDuckGo 暂不可用（HTTP {response.status}），本次未返回搜索结果。'
            data = bytearray()
            while len(data) <= MAX_PAGE:
                if monotonic() >= deadline:
                    return 'DuckDuckGo 暂不可用（读取超时）。'
                chunk = response.read1(min(32768, MAX_PAGE + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
        if len(data) > MAX_PAGE:
            return 'DuckDuckGo 暂不可用（返回页面超出大小限制）。'
        page = LiteResults()
        page.feed(data.decode('utf-8', errors='replace'))
        page.close()
    except HTTPError as exc:
        exc.close()
        return f'DuckDuckGo 暂不可用（HTTP {exc.code}），本次未返回搜索结果。'
    except (OSError, ValueError, http.client.HTTPException):
        return 'DuckDuckGo 暂不可用（连接失败或响应异常）。'
    if page.challenge:
        return 'DuckDuckGo 暂不可用（返回了验证页），本次未返回搜索结果。'
    if not page.rows:
        return '未取得 DuckDuckGo 搜索结果；可能没有匹配项，或返回页面不受支持。'
    entries = [f'{i}. {title}\n{url}' + (f'\n{snippet}' if snippet else '')
               for i, (title, url, snippet) in enumerate(page.rows, 1)]
    return 'DuckDuckGo 搜索结果（未打开网页）\n\n' + '\n\n'.join(entries)
