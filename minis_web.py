"""Look up public web data only from the current, original owner prompt.

An empty result means ordinary chat; a result without sources is a local status.
For a successful lookup, use model_data only in an isolated user-role message;
source_footer is for local display, never for the model or conversation history.
"""
import codecs
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
import http.client
import io
import ipaddress
from queue import Empty, Queue
import re
import socket
import ssl
from threading import BoundedSemaphore, Thread
from time import monotonic
from urllib.parse import quote, unquote, urlsplit

_MAX_URL = 2048
_MAX_BODY = 512 * 1024
_MAX_WIRE = _MAX_BODY + 256 * 1024  # Includes headers and transfer framing.
_MAX_TEXT = 8000
_TIMEOUT = 8.0
_DNS_TIMEOUT = 3.0
_DNS_SLOTS = BoundedSemaphore(4)
_USER_AGENT = 'Minis-Linux (+https://github.com/brucelau1987cn/minis-linux)'
_URL = re.compile(r'''https?://[^\s<>"`“”‘’「」『』《》。，、；！？）】]+''',
                  re.IGNORECASE)
# The privacy scan also covers punctuation that the command grammar won't
# accept, so declining a read doesn't send a suspect link to normal chat.
_URL_TEXT = re.compile(r'''https?://[^\s<>"`“”‘’「」『』《》]+''', re.IGNORECASE)
_ZH_SEARCH = re.compile(
    r'^(?P<polite>请(?:你)?\s*(?:帮(?:我|忙)\s*)?|帮(?:我|忙)\s*)?'
    r'(?:(?:上网|联网|在网上|网上|网络|网页)\s*(?:搜索|搜(?:寻)?|查(?:找|询)?|检索)'
    r'|搜索|搜寻|搜)(?P<once>\s*(?:一下|一搜))?'
)
_EN_SEARCH = re.compile(
    r'^(?:(?:could|can|would|will) you\s+)?(?:please\s+)?'
    r'(?:search\s+(?:(?:the (?:web|internet)|online)(?:\s+for)?|for)'
    r'|(?P<lookup>look up)(?=\s+.+\s+(?:online|on the web)[?.!]*$)|web search|search(?=[:：]))(?=\s|[:：]|$)',
    re.IGNORECASE,
)
_ZH_READ = re.compile(r'^(?:请(?:你)?\s*)?(?:帮我\s*)?(?:打开|读取|阅读|读|总结|摘要|概括)(?:一下)?\s*[:：]?\s*')
_EN_READ = re.compile(r'^(?:(?:could|can|would|will) you\s+)?(?:please\s+)?'
                      r'(?:open|read|summari[sz]e)(?:\s+(?:this|the)\s+(?:page|link|url))?'
                      r'(?:\s+|[:：]\s*)', re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r'\[[^\[\]\r\n]*\]\((.+)\)')
# Small allowlist for ordinary public URL options; unknown parameters are not
# presumed public, even if they do not look like known signing parameters.
_PUBLIC_OPTIONS = {'q', 'page', 'view', 'lang', 'hl', 'id', 'v', 'format', 'sort', 'tab'}
_PUBLIC_SLUG = re.compile(r'[\w.\-]{1,48}')
_CREDENTIAL_WORD = (r'(?:tokens?|auth(?:orization)?|oauth2?|api[-_ ]?key|secrets?|pass(?:word|wd)|'
                    r'credentials?|signed|sig|signatures?|invite|invitation|magic[_-]?link|session(?:id)?|'
                    r'reset[_-]?password|password[_-]?reset)')
_CREDENTIAL_SEGMENT = re.compile(rf'(?:^|[/;:=._-]){_CREDENTIAL_WORD}(?:$|[/;:=._-])', re.IGNORECASE)
_CREDENTIAL_QUERY = re.compile(rf'\b{_CREDENTIAL_WORD}\s*[:=]|\bbearer\s+\S', re.IGNORECASE)
_OPAQUE_SEGMENT = re.compile(r'(?:[a-fA-F0-9]{24,}|[a-zA-Z0-9_-]{32,})')
_DNS_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
_INTERNAL = {'alt', 'arpa', 'corp', 'example', 'home', 'internal', 'intranet', 'invalid',
             'lan', 'local', 'localdomain', 'localhost', 'onion', 'test'}
_SPECIAL_V4 = tuple(map(ipaddress.ip_network, ('192.0.0.0/24', '192.88.99.0/24', '168.63.129.16/32')))
_SPECIAL_V6 = tuple(map(ipaddress.ip_network, ('2001::/23', '2002::/16', '3fff::/20')))
_V6_UNICAST = ipaddress.ip_network('2000::/3')
_MEDIA = {'text/plain', 'text/html', 'application/xhtml+xml'}
_CHARSETS = {'utf-8', 'utf-8-sig', 'utf-16', 'utf-16-be', 'utf-16-le', 'ascii', 'iso8859-1', 'cp1252',
             'gb18030', 'gbk', 'gb2312', 'big5', 'big5hkscs', 'shift_jis', 'euc_jp', 'euc_kr'}


class _WebContext(str):
    source_footer: str
    model_data: dict

    def __new__(cls, text, sources=(), *, kind='read', documents=()):
        value = super().__new__(cls, text)
        value.model_data = {'operation': kind, 'untrusted_web_data': list(documents) or [{'text': text}]}
        value.source_footer = ('来源：\n' + '\n'.join(f'{n}. {url}' for n, url in sources)
                               if sources else '')
        return value


class _WebError(ValueError):
    pass


@dataclass(frozen=True)
class _Target:
    host: str
    authority: str
    port: int
    tls: bool
    path: str
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None


def _search_query(prompt):
    match = _ZH_SEARCH.match(prompt)
    if match:
        rest = prompt[match.end():]
        if (not (match['polite'] or match['once'])
                and rest and not (rest[0].isspace() or rest[0] in ':：')):
            return None
        return rest.lstrip(' :：\t').strip()
    match = _EN_SEARCH.match(prompt)
    if match:
        rest = prompt[match.end():].lstrip(' :：\t').strip()
        if match['lookup']:
            rest = re.sub(r'\s+(?:online|on the web)[?.!]*$', '', rest, flags=re.IGNORECASE)
        return rest.rstrip('?').rstrip()
    return None


def _read_url(prompt, links):
    # Deliberately a narrow grammar: the entire message (after an optional
    # read verb) is a single raw, angle-bracketed, or Markdown link.
    if len(links) != 1 or len(prompt.splitlines()) > 1:
        return ''
    verb = _ZH_READ.match(prompt) or _EN_READ.match(prompt)
    value = prompt[verb.end():] if verb else prompt
    if value.startswith('<') and value.endswith('>'):
        value = value[1:-1]
    elif link := _MARKDOWN_LINK.fullmatch(value):
        value = link[1]
        if value.startswith('<') and value.endswith('>'):
            value = value[1:-1]
    return value if _URL.fullmatch(value) else ''


def _public_slug(value):
    return (bool(_PUBLIC_SLUG.fullmatch(value)) and not _CREDENTIAL_SEGMENT.search(value)
            and not _OPAQUE_SEGMENT.fullmatch(value))


def _potentially_sensitive(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    path = unquote(parts.path)
    if ('@' in parts.netloc or '%' in parts.netloc or '%' in path
            or _CREDENTIAL_SEGMENT.search(path)
            or any(_OPAQUE_SEGMENT.fullmatch(segment) for segment in path.split('/'))):
        return True
    if parts.fragment and not _public_slug(unquote(parts.fragment)):
        return True
    keys = set()
    for option in parts.query.split('&') if parts.query else ():
        key, sep, value = option.partition('=')
        key = unquote(key).lower()
        if not sep or key not in _PUBLIC_OPTIONS or key in keys or not _public_slug(unquote(value)):
            return True
        keys.add(key)
    return False


def without_urls(text):
    # URLs shown to the owner come only from the checked source ledger. Text
    # in titles/snippets/pages may itself contain arbitrary URL strings.
    return _URL_TEXT.sub('[链接省略]', text)


def _global(ip):
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    if ip.version == 4:
        return not any(ip in net for net in _SPECIAL_V4)
    # Public unicast only; exclude mapped, translation and transition ranges.
    return ip in _V6_UNICAST and not any(ip in net for net in _SPECIAL_V6)


def _target(url):
    if (not 0 < len(url) <= _MAX_URL or '\\' in url
            or any(c.isspace() or not c.isprintable() for c in url)
            or len(url.encode('utf-16-le')) // 2 > _MAX_URL):
        raise _WebError('网址格式不受支持')
    parts = urlsplit(url)
    tls = parts.scheme == 'https'
    port = 443 if tls else 80
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or any(c in parts.netloc for c in '@%')
            or parts.port not in (None, port)):
        raise _WebError('仅支持无凭据的标准端口 HTTP(S) 网址')
    host = parts.hostname
    # Match the entire authority (urlsplit alone accepts some trailing junk).
    authority = f'[{host}]' if ':' in host else host
    if parts.netloc.lower() not in (authority, f'{authority}:{port}'):
        raise _WebError('网址格式不受支持')
    host = host.removesuffix('.')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _global(literal):
            raise _WebError('目标不是公网地址')
    else:
        host = host.encode('idna').decode('ascii').lower()
        labels = host.split('.')
        if (len(host) > 253 or len(labels) < 2 or labels[-1] in _INTERNAL
                or any(label == 'localhost' for label in labels)
                or re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', labels[-1])
                or any(not _DNS_LABEL.fullmatch(label) for label in labels)):
            raise _WebError('目标不是公网域名')
    authority = f'[{host}]' if ':' in host else host
    if parts.port is not None:
        authority += f':{port}'
    path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=")
    if parts.query:
        path += '?' + quote(parts.query, safe="/%?:@!$&'()*+,;=")
    return _Target(host, authority, port, tls, path, literal)


def _remaining(deadline):
    left = deadline - monotonic()
    if left <= 0:
        raise TimeoutError('web deadline expired')
    return left


def _addresses(target, deadline):
    if target.literal is not None:
        return [target.literal]
    if not _DNS_SLOTS.acquire(blocking=False):
        raise TimeoutError('web resolver busy')
    result = Queue(1)

    def resolve():
        try:
            # Absolute DNS name avoids resolver suffix expansion into local domains.
            result.put(socket.getaddrinfo(target.host + '.', target.port, type=socket.SOCK_STREAM,
                                          proto=socket.IPPROTO_TCP))
        except OSError as exc:
            result.put(exc)
        finally:
            _DNS_SLOTS.release()

    try:
        Thread(target=resolve, daemon=True).start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise _WebError('无法启动域名解析') from None
    # libc DNS is not cancellable; only the bounded resolver workers may outlive the turn.
    try:
        rows = result.get(timeout=min(_DNS_TIMEOUT, _remaining(deadline)))
    except Empty:
        raise TimeoutError('web DNS timed out') from None
    if isinstance(rows, OSError):
        raise rows
    if not 1 <= len(rows) <= 64:
        raise _WebError('域名解析结果为空或过多')
    ips = []
    for family, _, _, _, address in rows:
        ip = ipaddress.ip_address(address[0])
        if (family != (socket.AF_INET6 if ip.version == 6 else socket.AF_INET)
                or address[1] != target.port
                or (isinstance(ip, ipaddress.IPv6Address) and (ip.scope_id or address[3]))
                or not _global(ip)):
            raise _WebError('域名解析中含有非公网地址')
        if ip not in ips:
            ips.append(ip)
    return ips


def _connect(target, deadline):
    for ip in _addresses(target, deadline)[:4]:
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        endpoint = (str(ip), target.port, 0, 0) if ip.version == 6 else (str(ip), target.port)
        sock = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            sock.settimeout(min(2, _remaining(deadline)))
            sock.connect(endpoint)  # Numeric, already validated; no second hostname lookup.
        except OSError:
            sock.close()
            continue
        return sock
    raise OSError('no validated address accepted the connection')


class _BoundedReader(io.RawIOBase):
    """Put the total deadline and wire bound below HTTP header/chunk parsing."""
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline
        self.budget = _MAX_WIRE

    def readable(self):
        return True

    def readinto(self, buf):
        if self.budget <= 0:
            raise _WebError('响应超出大小限制')
        self.sock.settimeout(_remaining(self.deadline))
        size = self.sock.recv_into(memoryview(buf)[:self.budget])
        self.budget -= size
        return size

    def makefile(self, mode):
        return io.BufferedReader(self)


def _check_headers(response):
    headers = response.headers
    if len(headers.get_all('Content-Type', [])) != 1 or headers.get_content_type() not in _MEDIA:
        raise _WebError('不支持的网页内容类型')
    if any(item.strip().lower() != 'identity' for item in headers.get_all('Content-Encoding', [])):
        raise _WebError('不支持压缩响应')
    transfer = [value.strip().lower() for value in headers.get_all('Transfer-Encoding', [])]
    lengths = headers.get_all('Content-Length', [])
    if transfer not in ([], ['chunked']) or (transfer and lengths) or len(lengths) > 1:
        raise _WebError('不支持的响应格式')
    if lengths and (response.length is None or not re.fullmatch(r'[0-9]+', lengths[0].strip())):
        raise _WebError('不支持的响应格式')
    if response.length is not None and response.length > _MAX_BODY:
        raise _WebError('响应超出大小限制')


@contextmanager
def _get(target):
    deadline = monotonic() + _TIMEOUT
    sock = _connect(target, deadline)
    try:
        sock.settimeout(_remaining(deadline))
        if target.tls:
            # Unlike create_default_context(), this never honors SSLKEYLOGFILE.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_default_certs()
            sock.settimeout(_remaining(deadline))
            sock = context.wrap_socket(sock, server_hostname=target.host)
        sock.settimeout(_remaining(deadline))
        sock.sendall((f'GET {target.path} HTTP/1.1\r\nHost: {target.authority}\r\n'
                      f'User-Agent: {_USER_AGENT}\r\nAccept: text/html, text/plain, application/xhtml+xml\r\n'
                      'Accept-Encoding: identity\r\nConnection: close\r\n\r\n').encode('ascii'))
        # HTTPResponse uses only the socket-like makefile() API.
        reader = _BoundedReader(sock, deadline)
        with http.client.HTTPResponse(reader, method='GET') as response:  # type: ignore[arg-type]
            response.begin()
            if response.status == 200:
                _check_headers(response)
            yield response
            if response.status == 200 and response.length not in (None, 0):
                raise _WebError('响应不完整')
    finally:
        sock.close()


class _SearchOpener:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def open(self, request, *, timeout):
        # Only minis_search may construct the outbound search target.
        if not request.full_url.startswith(self.endpoint + '?'):
            raise _WebError('不支持的搜索端点')
        return _get(_target(request.full_url))


def _search(query):
    # Lazy import: minis_search imports minis, which may in turn import this module.
    import minis_search

    # Use the existing search implementation and its stable result-line format.
    try:
        text = minis_search.search_web(query, opener=_SearchOpener(minis_search.SEARCH_URL))
    except AssertionError:
        # HTMLParser can raise on malformed declarations in untrusted pages.
        text = 'DuckDuckGo 暂不可用（返回页面不受支持）。'
    header = 'DuckDuckGo 搜索结果（未打开网页）'
    if not text.startswith(header + '\n\n'):
        return _WebContext(text, kind='search')
    sources, documents = [], []
    for block in text.split('\n\n')[1:]:
        title_line, url, *snippet = block.split('\n')
        number, _, title = title_line.partition('. ')
        try:
            _target(url)  # Syntax and literals only: never resolve or open result links.
        except ValueError:
            continue
        if _potentially_sensitive(url):
            continue
        number = int(number)
        sources.append((number, url))
        documents.append({'source': number, 'title': without_urls(title),
                          'snippet': without_urls(' '.join(snippet))})
    if not documents:
        return _WebContext('未取得可引用的公开搜索结果；未打开结果网页。', kind='search')
    return _WebContext(header + '\n\n' + '\n'.join(row['snippet'] for row in documents),
                       sources, kind='search', documents=documents)


class _PageText(HTMLParser):
    _SKIP = {'head', 'script', 'style', 'template', 'noscript', 'nav', 'footer', 'aside', 'svg', 'form', 'iframe'}
    _BLOCK = {'article', 'blockquote', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'main',
              'p', 'pre', 'section', 'table', 'td', 'th', 'tr', 'ul', 'ol'}
    _VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_tag = None
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag not in self._VOID and (tag in self._SKIP or any(
                key == 'hidden' or (key == 'aria-hidden' and (value or '').lower() == 'true')
                for key, value in attrs)):
            self.skip_tag, self.skip_depth = tag, 1
        elif tag in self._BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if not self.skip_depth:
                    self.skip_tag = None
        elif tag in self._BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip_tag:
            self.parts.append(re.sub(r'\s+', ' ', data))


def _extract(target, url):
    from minis import safe_display

    try:
        with _get(target) as response:
            if response.status != 200:
                return _WebContext(f'未读取网页（HTTP {response.status}；不跟随重定向）。')
            media = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or 'utf-8-sig'
            body = response.read(_MAX_BODY + 1)
        if len(body) > _MAX_BODY:
            raise _WebError('响应超出大小限制')
        try:
            charset = codecs.lookup(charset).name
        except LookupError:
            charset = 'utf-8-sig'
        text = body.decode(charset if charset in _CHARSETS else 'utf-8-sig', errors='replace')
        if media != 'text/plain':
            page = _PageText()
            page.feed(text)
            page.close()
            text = ''.join(page.parts)
        text = safe_display('\n'.join(line for raw in text.splitlines() if (line := ' '.join(raw.split()))))
        if not text:
            return _WebContext('未读取到网页正文；不执行脚本。')
    except _WebError as exc:
        return _WebContext(f'未读取网页（{exc}）。')
    except (OSError, ValueError, AssertionError, http.client.HTTPException):
        return _WebContext('未读取网页（连接失败、超时或响应异常）。')
    text = without_urls(text)
    tail = '\n（正文已截断。）' if len(text) > _MAX_TEXT else ''
    text = text[:_MAX_TEXT] + tail
    return _WebContext(text, [(1, url)], documents=[{'source': 1, 'text': text}])


def prepare_web(prompt: str) -> _WebContext:
    """At most one lookup: explicit search, or one deliberately supplied link."""
    prompt = prompt.strip()
    # Reject suspect URLs even in prose so the caller cannot leak them by
    # falling through to ordinary model chat. This is not a general DLP filter.
    links = [match[0] for match in _URL_TEXT.finditer(prompt)]
    query = _search_query(prompt)
    url = _read_url(prompt, links) if query is None else ''
    if any(_potentially_sensitive(link) for link in ([url] if url else links)):
        return _WebContext('未联网：链接可能包含私密信息，请提供公开、无凭据的链接。')
    if query is not None:
        if len(prompt.splitlines()) > 1:
            return _WebContext('未搜索：关键词必须在同一行。')
        if _CREDENTIAL_QUERY.search(query):
            return _WebContext('未搜索：关键词可能包含凭据，请仅输入公开关键词。', kind='search')
        try:
            for link in links:
                _target(link)  # Don't send obviously nonpublic URLs to search.
        except ValueError:
            return _WebContext('未搜索：关键词中仅允许公开的 HTTP(S) 链接。', kind='search')
        return _search(query)
    if not url:
        return _WebContext('')
    try:
        target = _target(url)
    except ValueError:
        return _WebContext('未读取网页（仅支持无凭据的公网 HTTP(S) 链接）。')
    return _extract(target, url)
