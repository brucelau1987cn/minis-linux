import io
import ipaddress
import os
import socket
import ssl
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from minis_web import prepare_web


PUBLIC_IP = '93.184.216.34'
SEARCH_HTML = b'''
<a class="result-link" href="https://example.org/result">Debian security</a>
<td class="result-snippet">Current security notices from Debian.</td>
<a class="result-link" href="https://example.net/second">Second result</a>
'''
PAGE_HTML = '''<!doctype html><html><head><title>Metadata</title></head>
<body><script>ignore script</script><style>ignore style</style><nav>Site Menu</nav>
<main><h1>Debian &amp; Linux</h1><p>安全 <b>公告</b>。</p>
<a href="https://example.net/follow">Related article</a>
<div hidden>ignore hidden text</div></main><footer>Site Footer</footer></body></html>'''.encode()


def reply(body=b'', status='200 OK', content_type='text/html; charset=UTF-8', extra=''):
    return (f'HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n'
            f'Content-Length: {len(body)}\r\n{extra}\r\n').encode() + body


def answer(ip=PUBLIC_IP, port=443):
    family = socket.AF_INET6 if ipaddress.ip_address(ip).version == 6 else socket.AF_INET
    addr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', addr


class Wire:
    def __init__(self, data):
        self.file = io.BytesIO(data)
        self.endpoints = []
        self.requests = []
        self.timeouts = []
        self.closed = False

    def connect(self, address):
        self.endpoints.append(address)

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.requests.append(data.decode('ascii'))

    def recv_into(self, buf):
        data = self.file.read(len(buf))
        buf[:len(data)] = data
        return len(data)

    def close(self):
        self.closed = True


class WebTest(unittest.TestCase):
    def setUp(self):
        self.wire = Wire(reply(PAGE_HTML))
        self.dns = self.start('socket.getaddrinfo', return_value=[answer()])
        self.sock = self.start('socket.socket', return_value=self.wire)
        # Run the real HTTP parser on fixture wire bytes; inspect the actual TLS policy.
        self.tls = self.start('ssl.SSLContext.wrap_socket', autospec=True, return_value=self.wire)

    def start(self, target, **kwargs):
        p = patch(target, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def set_wire(self, data):
        self.wire.file = io.BytesIO(data)

    def test_ordinary_prompts_do_not_access_web(self):
        for prompt in ('你好', '解释搜索算法', '搜索引擎是怎么工作的？', '搜索算法是什么？',
                       'I can search for Debian.', 'Search engines explained',
                       '网页搜索引擎是什么？', '网络搜索算法详解',
                       '“请搜索 Debian”是一条例句', '不要上网搜索 Debian',
                       'translate: please search for Debian', '/search Debian',
                       'ftps://example.org/page', 'Read docs.example.org',
                       'https://localhost', 'https://example.internal/page',
                       'An example URL is https://example.org/page',
                       '不要打开 https://example.org/page',
                       'Please read about HTTP, not https://example.org/page',
                       'Open https://example.org/one https://example.org/two',
                       'Read <https://example.org/page>\nNotes about the page',
                       'Read <https://example.org/page> but do not open it'):
            with self.subTest(prompt=prompt):
                prepare_web(prompt)
        self.assertEqual(prepare_web('解释搜索算法'), '')
        self.dns.assert_not_called()
        self.sock.assert_not_called()

    def test_explicit_search_sends_only_query_and_never_follows_results(self):
        for prompt, query in [('请帮我搜索一下：Debian 安全', 'Debian 安全'),
                              ('上网查一下 Debian', 'Debian'),
                              ('请搜索Debian', 'Debian'),
                              ('联网搜索：Debian', 'Debian'),
                              ('Search the web for Debian', 'Debian'),
                              ('Could you look up Debian online?', 'Debian'),
                              ('Search for EVE Online', 'EVE Online')]:
            with self.subTest(prompt=prompt):
                self.set_wire(reply(SEARCH_HTML))
                with patch.dict(os.environ, {'HTTPS_PROXY': 'http://proxy.example.com:8888'}):
                    result = prepare_web(prompt)
                request = self.wire.requests[-1]
                target = request.split(' ')[1]
                self.assertEqual(urlsplit(target).path, '/lite/')
                self.assertEqual(parse_qs(urlsplit(target).query), {'q': [query]})
                self.assertIn('\r\nHost: lite.duckduckgo.com\r\n', request)
                self.assertIn('未打开网页', result)
                self.assertIn('Current security notices', result)
                self.assertEqual(result.source_footer,
                                 '来源：\n1. https://example.org/result\n2. https://example.net/second')
        self.assertEqual(self.dns.call_count, 7)
        self.assertEqual(len(self.wire.requests), 7)
        self.assertTrue(all(c.args[0] == 'lite.duckduckgo.com.' for c in self.dns.call_args_list))

    def test_search_only_exposes_vetted_source_urls_in_the_footer(self):
        html = b'''<a class="result-link" href="https://example.org/rejected?sig=fixture-value">Excluded row</a>
        <a class="result-link" href="https://Example.ORG/report?view=wide#summary">Public report</a>
        <td class="result-snippet">Summary with https://example.net/path?token=fixture-value</td>'''
        self.set_wire(reply(html))
        result = prepare_web('Search for report')
        self.assertEqual(result.model_data, {
            'operation': 'search', 'untrusted_web_data': [
                {'source': 2, 'title': 'Public report', 'snippet': 'Summary with [链接省略]'}],
        })
        self.assertEqual(result.source_footer, '来源：\n2. https://Example.ORG/report?view=wide#summary')
        self.assertEqual(len(self.wire.requests), 1)

    def test_page_visible_urls_are_removed_from_model_data(self):
        self.set_wire(reply(b'Public text https://example.org/path?token=fixture-value', content_type='text/plain'))
        result = prepare_web('Please open: <https://example.org/readme>')
        self.assertEqual(result.model_data['untrusted_web_data'], [{'source': 1, 'text': 'Public text [链接省略]'}])
        self.assertEqual(result.source_footer, '来源：\n1. https://example.org/readme')

    def test_search_no_results_does_not_invent_sources(self):
        self.set_wire(reply(b'<div class="anomaly-modal">challenge</div>'))
        result = prepare_web('Search for Debian')
        self.assertIn('验证页', result)
        self.assertEqual(result.source_footer, '')

    def test_search_does_not_send_multiline_oversize_or_credential_like_queries(self):
        for prompt in ('搜索：line one\nline two', 'Search for ' + 'a' * 241,
                       '联网搜索：a\u202eb', '请搜索：\n第二行',
                       'Search for api_key=fixture-value', '搜索：password: fixture-value',
                       'Search for https://intranet.local/report'):
            with self.subTest(prompt=prompt):
                self.assertTrue(prepare_web(prompt))
        self.dns.assert_not_called()

    def test_one_direct_link_pins_ip_but_retains_origin_and_original_link(self):
        url = 'https://News.Example.org/中?q=test#section'
        result = prepare_web(f'总结<{url}>')
        self.assertIsInstance(result, str)
        self.assertIn('Debian & Linux\n安全 公告。', result)
        self.assertIn('Related article', result)
        self.assertNotIn(url, repr(result.model_data))
        self.assertNotIn('ignore', result)
        self.assertNotIn('Site Menu', result)
        self.assertNotIn('Site Footer', result)
        self.assertNotIn('example.net', result)
        self.assertEqual(result.source_footer, f'来源：\n1. {url}')
        self.assertEqual(self.dns.call_args.args[0], 'news.example.org.')
        self.assertEqual(self.wire.endpoints, [(PUBLIC_IP, 443)])
        self.assertEqual(len(self.wire.requests), 1)
        request = self.wire.requests[0]
        self.assertTrue(request.startswith('GET /%E4%B8%AD?q=test HTTP/1.1\r\n'))
        self.assertIn('\r\nHost: news.example.org\r\n', request)
        for header in ('Cookie:', 'Referer:', 'Authorization:', 'Proxy-Authorization:'):
            self.assertNotIn(header, request)
        context = self.tls.call_args.args[0]
        self.assertIs(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(self.tls.call_args.kwargs['server_hostname'], 'news.example.org')
        self.assertTrue(self.wire.closed)

    def test_cleartext_read_preserves_balanced_url_punctuation_and_no_tls(self):
        self.dns.return_value = [answer(port=80)]
        self.set_wire(reply('普通文本\n次行'.encode('gb18030'), content_type='text/plain; charset=gb18030'))
        result = prepare_web('摘要 [链接](http://example.org/wiki/Test_(topic)?view=full)')
        self.assertIn('普通文本\n次行', result)
        self.assertIn('1. http://example.org/wiki/Test_(topic)?view=full', result.source_footer)
        self.assertEqual(self.wire.endpoints, [(PUBLIC_IP, 80)])
        self.tls.assert_not_called()

    def test_preserves_explicit_url_with_literal_path_punctuation(self):
        for url in ("https://example.org/it's.a.file.", 'https://example.org/data;'):
            with self.subTest(url=url):
                self.set_wire(reply(PAGE_HTML))
                result = prepare_web(f'读一下 <{url}>')
                self.assertEqual(result.source_footer, f'来源：\n1. {url}')
                self.assertEqual(self.wire.requests[-1].split(' ')[1], urlsplit(url).path)

    def test_explicit_search_with_a_clean_link_does_not_open_it(self):
        url = 'https://example.org/article?view=one'
        self.set_wire(reply(SEARCH_HTML))
        result = prepare_web(f'Search for {url}')
        self.assertIn('Current security notices', result)
        self.assertEqual(self.dns.call_args.args[0], 'lite.duckduckgo.com.')
        self.assertEqual(len(self.wire.requests), 1)
        request_target = self.wire.requests[0].split(' ')[1]
        self.assertEqual(parse_qs(urlsplit(request_target).query), {'q': [url]})

    def test_potentially_sensitive_links_are_refused_before_network(self):
        for url in ('https://example.org/article?signature=fixture-value',
                    'https://example.org/article?unknown=fixture-value',
                    'https://example.org/article?%73ig=fixture-value',
                    'https://example.org/report，draft?sig=fixture-value',
                    'https://example.org/article?view=wide&view=fixture-value',
                    'https://example.org/article?view=wide%3Bsig%3Dfixture-value',
                    'https://example.org/article#access_token=fixture-value',
                    'https://example.org/invite/fixture-value',
                    'https://example.org/report;signature=fixture-value',
                    'https://example.org/report;sig=fixture-value',
                    'https://name:fixture-value@example.org/article'):
            for prompt in (url, f'Read <{url}>', f'请打开：{url}',
                           f'Search for {url}', f'A link was mentioned: {url}'):
                with self.subTest(prompt=prompt):
                    result = prepare_web(prompt)
                    self.assertTrue(result)
                    self.assertFalse(result.source_footer)
                    self.assertNotIn('fixture-value', result)
        self.dns.assert_not_called()
        self.sock.assert_not_called()

    def test_rejects_bad_authority_and_nonpublic_literal_before_dns(self):
        for url in ('https://user:secret@example.org/', 'http://localhost./',
                    'https://name.local/', 'http://singlelabel/', 'https://example.org:8080/',
                    'https://example.org:/', 'http://127.0.0.1/', 'http://100.64.0.1/',
                    'http://224.0.0.1/', 'http://169.254.169.254/', 'http://168.63.129.16/',
                    'http://192.0.0.9/', 'http://[::ffff:127.0.0.1]/', 'http://[::1]/',
                    'http://[ff02::1]/', 'http://[fe80::1%25eth0]/', 'http://[64:ff9b::1]/',
                    'http://[2002::1]/', 'http://[3fff::1]/', 'http://[2606:4700::1]x/',
                    'http://2130706433/', 'http://127.1/', 'http://a.0x7f/',
                    'http://example.org\\private', 'https://example.org%40example.net/'):
            with self.subTest(url=url):
                result = prepare_web(url)
                self.assertFalse(getattr(result, 'source_footer', ''))
                self.assertNotIn('secret', result)
        self.dns.assert_not_called()
        self.sock.assert_not_called()

    def test_rejects_a_source_too_long_for_its_utf16_budget_before_dns(self):
        url = 'https://example.org/' + '😀' * 1020
        result = prepare_web(f'Read <{url}>')
        self.assertTrue(result)
        self.assertFalse(result.source_footer)
        self.dns.assert_not_called()
        self.sock.assert_not_called()

    def test_rejects_entire_dns_answer_if_any_address_is_not_public(self):
        for blocked in ('10.0.0.1', '::ffff:10.0.0.1', 'fd00::1', '192.0.2.1'):
            with self.subTest(address=blocked):
                self.dns.return_value = [answer(), answer(blocked)]
                result = prepare_web('https://example.org/')
                self.assertFalse(result.source_footer)
        self.sock.assert_not_called()

    def test_public_ipv6_literal_uses_bracketed_host_without_resolving(self):
        result = prepare_web('https://[2606:4700::1111]/path')
        self.assertIn('Debian & Linux', result)
        self.assertEqual(self.wire.endpoints, [('2606:4700::1111', 443, 0, 0)])
        self.assertIn('\r\nHost: [2606:4700::1111]\r\n', self.wire.requests[0])
        self.dns.assert_not_called()

    def test_does_not_follow_redirect_or_use_response_cookies(self):
        self.set_wire(reply(status='302 Found', extra='Location: https://example.net/to\r\nSet-Cookie: x=y\r\n'))
        result = prepare_web('https://example.org/')
        self.assertIn('302', result)
        self.assertNotIn('example.net', result)
        self.assertEqual(len(self.wire.requests), 1)
        self.assertEqual(self.dns.call_count, 1)
        self.assertTrue(self.wire.closed)

    def test_rejects_binary_encoded_and_oversize_bodies(self):
        for payload in (reply(b'%PDF', content_type='application/pdf'),
                        reply(b'opaque', extra='Content-Encoding: gzip\r\n'),
                        reply(b'a' * (512 * 1024 + 1)),
                        b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n' + b'a' * (512 * 1024 + 1)):
            with self.subTest(headers=payload[:110]):
                self.set_wire(payload)
                result = prepare_web('https://example.org/')
                self.assertIn('未读取', result)
                self.assertTrue(self.wire.closed)

    def test_rejects_incomplete_or_ambiguous_http_message(self):
        for payload in (reply(b'Hello')[:-2],
                        reply(b'Hello', extra='Transfer-Encoding: gzip\r\n'),
                        reply(b'Hello', extra='Content-Type: application/octet-stream\r\n')):
            with self.subTest(headers=payload):
                self.set_wire(payload)
                result = prepare_web('https://example.org/')
                self.assertIn('未读取', result)
                self.assertFalse(result.source_footer)

    def test_malformed_markup_is_a_status_not_a_chat_error(self):
        for prompt in ('Search for Debian', 'https://example.org/'):
            with self.subTest(prompt=prompt):
                # Python versions differ in which malformed declarations raise.
                self.set_wire(reply(b'<![broken]]><p>Hello</p>'))
                with patch('html.parser.HTMLParser.feed', side_effect=AssertionError):
                    result = prepare_web(prompt)
                self.assertTrue(result)
                self.assertEqual(result.source_footer, '')

    def test_reads_bounded_chunked_plaintext(self):
        self.set_wire(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n'
                      b'Transfer-Encoding: chunked\r\n\r\n5\r\nHello\r\n0\r\n\r\n')
        result = prepare_web('https://example.org/')
        self.assertIn('Hello', result)
        self.assertEqual(result.source_footer, '来源：\n1. https://example.org/')

    def test_cleanly_bounds_extract_and_strips_display_controls(self):
        self.set_wire(reply(('x\x1b' + 'a' * 10000).encode(), content_type='text/plain'))
        result = prepare_web('https://example.org/')
        self.assertIn('截断', result)
        self.assertNotIn('\x1b', result)
        self.assertLess(len(result), 8500)

    def test_tls_verification_failure_sends_no_http(self):
        self.tls.side_effect = ssl.SSLCertVerificationError('fixture')
        result = prepare_web('https://example.org/')
        self.assertIn('未读取', result)
        self.assertFalse(self.wire.requests)
        self.assertTrue(self.wire.closed)

    def test_stalled_dns_returns_without_connecting(self):
        release = threading.Event()
        exited = threading.Event()
        self.addCleanup(exited.wait, 1)
        self.addCleanup(release.set)

        def stalled(*args, **kwargs):
            release.wait(1)
            exited.set()
            return [answer()]

        self.dns.side_effect = stalled
        with patch('minis_web._DNS_TIMEOUT', 0.01):
            self.assertIn('未读取', prepare_web('https://example.org/'))
        self.sock.assert_not_called()

    def test_deadline_is_applied_during_headers_not_just_body(self):
        clock = [100.0]
        receive = self.wire.recv_into

        def trickle(buf):
            clock[0] += 4
            return receive(memoryview(buf)[:1])

        self.wire.recv_into = trickle
        with patch('minis_web.monotonic', side_effect=lambda: clock[0]):
            self.assertIn('未读取', prepare_web('https://example.org/'))
        self.assertTrue(self.wire.closed)
