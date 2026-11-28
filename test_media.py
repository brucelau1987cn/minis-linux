"""Offline tests for Telegram media and the optional local helpers."""
import base64
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import minis_media as media


class AttachmentTest(unittest.TestCase):
    def test_identifies_utf8_document_and_keeps_caption_as_data(self):
        item = media.identify_attachment({'document': {
            'file_id': 'Test_1-2', 'file_name': 'notes.md', 'file_size': 15,
            'mime_type': 'text/plain'}, 'caption': '/new'})
        assert item is not None
        self.assertEqual((item.kind, item.file_id, item.filename, item.size, item.caption),
                         ('text', 'Test_1-2', 'notes.md', 15, '/new'))
        self.assertEqual(media.parse_text(b'\xef\xbb\xbfhello\n'), 'hello\n')

    def test_photo_prefers_largest_usable_variant(self):
        item = media.identify_attachment({'photo': [
            {'file_id': 'large', 'width': 300, 'height': 200, 'file_size': 2000},
            {'file_id': 'giant', 'width': 400, 'height': 300, 'file_size': media.MAX_IMAGE_BYTES + 1},
            {'file_id': 'small', 'width': 200, 'height': 100, 'file_size': 1000}]})
        assert item is not None
        self.assertEqual((item.kind, item.file_id, item.mime_type), ('image', 'large', 'image/jpeg'))

    def test_unrelated_message_has_no_attachment(self):
        self.assertIsNone(media.identify_attachment({'text': 'not an attachment'}))
        self.assertIsNone(media.identify_attachment({'sticker': {}}))

    def test_rejects_invalid_or_unsupported_attachment_metadata(self):
        base = {'file_id': 'valid', 'file_name': 'notes.txt'}
        for change in ({'file_name': '../notes.txt'}, {'file_name': 'x.exe'},
                       {'file_name': 'x\r\n.txt'}, {'file_id': 'https://host/file'},
                       {'file_size': True}, {'file_size': media.MAX_TEXT_BYTES + 1},
                       {'mime_type': 'application/pdf'}):
            with self.subTest(change=change), self.assertRaises(media.MediaError):
                media.identify_attachment({'document': {**base, **change}})
        with self.assertRaises(media.MediaError):
            media.identify_attachment({'document': base, 'photo': []})

    def test_voice_requires_explicit_opt_in(self):
        message = {'voice': {'file_id': 'clip', 'mime_type': 'audio/ogg', 'duration': 5}}
        with self.assertRaises(media.MediaError):
            media.identify_attachment(message)
        item = media.identify_attachment(message, voice_enabled=True)
        assert item is not None
        self.assertEqual(item.kind, 'voice')
        message['voice']['duration'] = media.MAX_VOICE_SECONDS + 1
        with self.assertRaises(media.MediaError):
            media.identify_attachment(message, voice_enabled=True)

    def test_rejects_non_utf8_binary_and_oversized_text(self):
        for data in (b'\xff', b'a\0b', b'  ', b'x' * (media.MAX_TEXT_BYTES + 1)):
            with self.subTest(prefix=data[:6]), self.assertRaises(media.MediaError):
                media.parse_text(data)


class TelegramMediaTest(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from urllib.request import BaseHandler
        from minis_telegram import Telegram

        outer = self
        self.calls = []
        self.file_path = 'documents/file_1.txt'
        self.payload = b'hello world'
        self.redirect_method = None
        self.rate_limit = False
        self.logical_rate_limit = False
        self.length = None
        self.file_size = None
        self.token = '12345:' + 'a' * 32  # fixture credential
        self.telegram = Telegram(self.token)
        self.attachment = media.identify_attachment({'document': {
            'file_id': 'test', 'file_name': 'notes.txt'}})
        assert self.attachment is not None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def respond(self, code, body=b'', **headers):
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def dispatch(self):
                import json
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                outer.calls.append((self.command, self.path, dict(self.headers), body))
                if outer.redirect_method and self.path.endswith(outer.redirect_method):
                    self.respond(302, Location='/unexpected')
                elif self.path.endswith('/getFile'):
                    self.respond(200, json.dumps({'ok': True, 'result': {
                        'file_path': outer.file_path, 'file_size': outer.file_size}}).encode())
                elif self.path.endswith('/sendDocument'):
                    if outer.logical_rate_limit:
                        self.respond(200, b'{"ok":false,"error_code":429}', **{'Retry-After': '20'})
                    elif outer.rate_limit:
                        self.respond(429, b'{"ok":false,"parameters":{"retry_after":2}}',
                                     **{'Retry-After': '20'})
                    else:
                        self.respond(200, b'{"ok":true,"result":{"message_id":99}}')
                elif self.path.startswith('/file/'):
                    headers = {} if outer.length is None else {'Content-Length': str(outer.length)}
                    self.respond(200, outer.payload, **headers)
                elif self.path == '/v1/chat/completions':
                    self.respond(200, b'{"choices":[{"message":{"content":"A red rectangle."}}]}')
                else:
                    self.respond(404)

            do_GET = do_POST = dispatch

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.loopback = f'http://127.0.0.1:{server.server_port}'
        runner = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        runner.start()
        self.addCleanup(server.server_close)
        self.addCleanup(runner.join)
        self.addCleanup(server.shutdown)

        # Block external network I/O even if the fixture handler is misconfigured.
        guard = patch('http.client.HTTPSConnection.connect', side_effect=AssertionError('non-fixture HTTPS'))
        guard.start()
        self.addCleanup(guard.stop)

        import socket
        connect = socket.socket.connect

        def loopback_only(sock, address):
            outer.assertEqual(address, ('127.0.0.1', server.server_port))
            return connect(sock, address)

        guard = patch.object(socket.socket, 'connect', loopback_only)
        guard.start()
        self.addCleanup(guard.stop)

        class LoopbackHTTPS(BaseHandler):
            handler_order = 100

            def https_open(self, req):
                from urllib.error import HTTPError
                from urllib.request import Request, build_opener, ProxyHandler
                from minis import NoRedirect
                expected = 'https://api.telegram.org'
                outer.assertTrue(req.full_url.startswith(expected + '/'))
                fixture = Request(outer.loopback + req.full_url[len(expected):], data=req.data,
                                  headers=dict(req.header_items()), method=req.get_method())
                try:
                    response = build_opener(ProxyHandler({}), NoRedirect()).open(fixture, timeout=req.timeout)
                except HTTPError as error:
                    # Let the *outer* opener handle HTTP errors/redirects.
                    response = error
                response.url = req.full_url
                return response

        self.telegram.opener.add_handler(LoopbackHTTPS())

    def test_downloads_only_fixed_telegram_path(self):
        self.assertEqual(media.download_attachment(self.telegram, self.attachment), b'hello world')
        self.assertEqual([item[:2] for item in self.calls], [
            ('POST', f'/bot{self.token}/getFile'),
            ('GET', f'/file/bot{self.token}/documents/file_1.txt')])
        self.assertIn(b'"file_id": "test"', self.calls[0][3])

    def test_rejects_unsafe_paths_before_file_request(self):
        for filename in ('https://example.invalid/a', '//localhost/a', '../x',
                         'x/%2e%2e/y', 'x?query', 'x#z', 'x\\y', 'x//y'):
            self.file_path = filename
            with self.subTest(filename=filename), self.assertRaises(media.MediaError):
                media.download_attachment(self.telegram, self.attachment)
        self.assertTrue(all(call[1].endswith('/getFile') for call in self.calls))

    def test_download_size_is_bounded_with_or_without_length(self):
        for field in ('file_size', 'length', 'payload'):
            with self.subTest(field=field):
                oversized = media.MAX_TEXT_BYTES + 1
                original = getattr(self, field)
                setattr(self, field, b'x' * oversized if field == 'payload' else oversized)
                try:
                    with self.assertRaises(media.MediaError):
                        media.download_attachment(self.telegram, self.attachment)
                finally:
                    setattr(self, field, original)

    def test_truncated_download_is_rejected(self):
        self.length = len(self.payload) + 1
        with self.assertRaises(media.MediaError):
            media.download_attachment(self.telegram, self.attachment)

    def test_download_does_not_follow_redirect_or_disclose_token(self):
        self.redirect_method = '/file_1.txt'
        with self.assertRaises(media.MediaError) as error:
            media.download_attachment(self.telegram, self.attachment)
        self.assertNotIn(self.token, str(error.exception))
        self.assertEqual(len(self.calls), 2)

    def test_metadata_request_does_not_follow_redirect(self):
        from minis_telegram import TelegramError
        self.redirect_method = '/getFile'
        with self.assertRaises(TelegramError) as error:
            media.download_attachment(self.telegram, self.attachment)
        self.assertEqual(error.exception.code, 302)
        self.assertNotIn(self.token, str(error.exception))
        self.assertEqual(len(self.calls), 1)

    def test_rejects_custom_api_origin_before_get_file(self):
        self.telegram.url = 'http://127.0.0.1:1/bot' + self.token + '/'
        with self.assertRaises(media.MediaError):
            media.download_attachment(self.telegram, self.attachment)
        self.assertFalse(self.calls)

    def test_rejects_proxying_or_redirecting_openers(self):
        from urllib.request import build_opener, ProxyHandler
        from minis import NoRedirect
        for opener in (build_opener(ProxyHandler({})),
                       build_opener(ProxyHandler({'https': 'http://127.0.0.1:1'}), NoRedirect())):
            self.telegram.opener = opener
            with self.assertRaises(media.MediaError):
                media.download_attachment(self.telegram, self.attachment)
        self.assertFalse(self.calls)

    def test_sends_one_multipart_markdown_document(self):
        from email import policy
        from email.parser import BytesParser
        result = media.send_markdown(self.telegram, 1234, '# 标题', filename='记录.md')
        self.assertEqual(result, {'message_id': 99})
        [(method, url, headers, body)] = self.calls
        self.assertEqual((method, url), ('POST', f'/bot{self.token}/sendDocument'))
        raw = b'Content-Type: ' + headers['Content-Type'].encode() + b'\r\n\r\n' + body
        parts = list(BytesParser(policy=policy.default).parsebytes(raw).iter_parts())
        self.assertEqual([p.get_param('name', header='Content-Disposition') for p in parts],
                         ['chat_id', 'document'])
        self.assertEqual(parts[0].get_payload(decode=True), b'1234')
        self.assertEqual(parts[1].get_filename(), '记录.md')
        self.assertEqual(parts[1].get_content_type(), 'text/markdown')
        self.assertEqual(parts[1].get_payload(decode=True), '# 标题'.encode())

    def test_invalid_upload_never_sends(self):
        for name, data in (('x".txt', b'hi'), ('x.pdf', b'fake'), ('x.jpg', b'fake'),
                           ('x.txt', b'x' * (media.MAX_TEXT_BYTES + 1))):
            with self.subTest(name=name), self.assertRaises(media.MediaError):
                media.send_document(self.telegram, 1234, name, data)
        self.assertFalse(self.calls)

    def test_pdf_upload_preserves_approved_bytes(self):
        data = _pdf_fixture(b'Words')
        self.assertEqual(media.send_document(self.telegram, 1234, 'doc.PDF', data), {'message_id': 99})
        self.assertIn(b'Content-Type: application/pdf\r\n\r\n' + data, self.calls[0][3])

    def test_vision_wire_format_with_real_provider_and_loopback_gateway(self):
        import json
        from minis import Provider
        data = _image_fixture('PNG')
        provider = Provider(self.loopback + '/v1', 'fixture-model-key', 'fixture-vision', read_only=True)
        self.assertEqual(media.analyze_image(provider, data, 'Describe it.', vision_enabled=True),
                         'A red rectangle.')
        [(method, url, _, body)] = self.calls
        self.assertEqual((method, url), ('POST', '/v1/chat/completions'))
        request = json.loads(body)
        self.assertEqual(request['model'], 'fixture-vision')
        self.assertEqual(request['messages'], [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'Describe it.'}, media.image_part(data)]}])

    def test_logical_upload_429_honors_header(self):
        self.logical_rate_limit = True
        with self.assertRaises(media.MediaError) as error:
            media.send_document(self.telegram, 1234, 'notes.txt', b'hello')
        self.assertEqual((error.exception.code, error.exception.retry_after), (429, 20))

    def test_upload_429_sets_existing_text_and_document_cooldown(self):
        import time
        from minis_telegram import TelegramError
        self.rate_limit = True
        now = time.monotonic()
        with self.assertRaises(media.MediaError) as error:
            media.send_document(self.telegram, 1234, 'notes.txt', b'hello')
        self.assertEqual((error.exception.code, error.exception.retry_after), (429, 20))
        self.assertGreaterEqual(self.telegram.send_after, now + 20)
        with self.assertRaises(TelegramError):
            self.telegram.send(1234, 'hello')
        with self.assertRaises(media.MediaError):
            media.send_document(self.telegram, 1234, 'notes.txt', b'hello')
        self.assertEqual(len(self.calls), 1)


def _pdf_fixture(text=None):
    stream = b'' if text is None else b'BT /F1 12 Tf 10 40 Td (' + text + b') Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R '
               b'/Resources << /Font << /F1 5 0 R >> >> >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    data = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(data)
    data += b'xref\n0 6\n0000000000 65535 f \n'
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return data + f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()


class PDFTest(unittest.TestCase):
    @unittest.skipUnless(Path('/usr/bin/pdftotext').is_file(), 'pdftotext unavailable')
    def test_real_text_layer_and_blank_pdf(self):
        self.assertEqual(media.extract_pdf_text(_pdf_fixture(b'Hello from PDF')), 'Hello from PDF')
        with self.assertRaisesRegex(media.MediaError, 'OCR'):
            media.extract_pdf_text(_pdf_fixture())

    def test_absent_pdftotext_is_optional(self):
        with patch.object(media, '_PDFTOTEXT', '/nonexistent/pdftotext'):
            with self.assertRaisesRegex(media.MediaError, 'pdftotext'):
                media.extract_pdf_text(_pdf_fixture(b'Hello'))

    def test_invalid_pdf_never_runs_helper(self):
        with patch('subprocess.Popen') as run, self.assertRaises(media.MediaError):
            media.extract_pdf_text(b'<html>fake PDF')
        run.assert_not_called()

    def test_runner_stops_on_time_and_output_limits(self):
        import sys
        for source, timeout in (('import time; time.sleep(2)', .2), ('print("a" * 100000)', 2)):
            with self.subTest(source=source), self.assertRaises(media.MediaError):
                media._run_helper([sys.executable, '-c', source], data=None, limit=100, timeout=timeout)


def _image_fixture(format, *, size=(12, 8), animate=False, **options):
    try:
        from PIL import Image, features
    except ImportError:
        raise unittest.SkipTest('Pillow unavailable') from None
    if format == 'WEBP' and not features.check('webp'):
        raise unittest.SkipTest('WebP decoder unavailable')
    stream = io.BytesIO()
    image = Image.new('RGB', size, 'red')
    if animate:
        options.update(save_all=True, append_images=[Image.new('RGB', size, 'blue')])
    image.save(stream, format, **options)
    return stream.getvalue()


class ImageTest(unittest.TestCase):
    def test_real_verified_formats(self):
        for format, mime in (('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp')):
            with self.subTest(format=format):
                data = _image_fixture(format)
                part = media.image_part(data, mime_type=mime)
                self.assertEqual(part['type'], 'image_url')
                prefix, encoded = part['image_url']['url'].split(',', 1)
                self.assertEqual(prefix, 'data:' + mime + ';base64')
                self.assertEqual(base64.b64decode(encoded), data)

    def test_rejects_spoofed_truncated_animated_and_excessive_images(self):
        real = _image_fixture('PNG')
        for data, mime in ((real, 'image/jpeg'), (real[:30], 'image/png'),
                           (_image_fixture('PNG', animate=True), 'image/png'),
                           (_image_fixture('PNG', size=(media.MAX_IMAGE_SIDE + 1, 1)), 'image/png'),
                           (real + b'x' * media.MAX_IMAGE_BYTES, 'image/png')):
            with self.subTest(size=len(data), mime=mime), self.assertRaises(media.MediaError):
                media.image_part(data, mime_type=mime)

    def test_rejects_oversized_webp_before_calling_decoder(self):
        image = _image_fixture('WEBP', size=(media.MAX_IMAGE_SIDE + 1, 1))
        with patch('PIL.Image.open') as decoder, self.assertRaises(media.MediaError):
            media.image_part(image, mime_type='image/webp')
        decoder.assert_not_called()

    def test_accepts_lossless_and_extended_webp(self):
        for data in (_image_fixture('WEBP', lossless=True),
                     _image_fixture('WEBP', icc_profile=b'fixture')):
            self.assertTrue(media.image_part(data)['image_url']['url'].startswith('data:image/webp;'))

    def test_analysis_returns_only_text_no_tool_dispatch(self):
        from unittest.mock import Mock
        provider = Mock()
        provider.reply.return_value = {'role': 'assistant', 'content': 'A red rectangle.'}
        image = _image_fixture('PNG')
        with self.assertRaises(media.MediaError):
            media.analyze_image(provider, image, 'describe', mime_type='image/png')
        provider.reply.assert_not_called()
        self.assertEqual(media.analyze_image(provider, image, 'describe', mime_type='image/png',
                                            vision_enabled=True), 'A red rectangle.')
        [messages] = provider.reply.call_args.args
        self.assertEqual([m['role'] for m in messages], ['user'])
        self.assertEqual(messages[0]['content'][0], {'type': 'text', 'text': 'describe'})
        self.assertEqual(messages[0]['content'][1], media.image_part(image, mime_type='image/png'))
        for result in ({'content': 'call', 'tool_calls': [{'id': '1'}]},
                       {'content': 'data:image/png;base64,sample'}, {}):
            provider.reply.return_value = result
            with self.assertRaises(media.MediaError):
                media.analyze_image(provider, image, 'describe', vision_enabled=True)
        provider.reply.side_effect = ValueError('sensitive upstream body')
        with self.assertRaises(media.MediaError) as error:
            media.analyze_image(provider, image, 'describe', vision_enabled=True)
        self.assertNotIn('sensitive', str(error.exception))


class VoiceTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.model = Path(temporary.name) / 'trusted-model.bin'
        self.model.write_bytes(b'model fixture')

    def test_disabled_voice_does_not_inspect_input_or_run_commands(self):
        with patch.object(media, '_run_helper') as run, self.assertRaisesRegex(media.MediaError, '启用'):
            media.transcribe_voice(b'not an ogg file', model_path=self.model)
        run.assert_not_called()

    def test_rejects_relative_model_and_missing_whisper_before_decode(self):
        with (patch.object(media, '_WHISPER', '/nonexistent/whisper-cli'),
              patch.object(media, '_decode_voice') as decode):
            for path in (Path('model.bin'), self.model):
                with self.subTest(path=path), self.assertRaises(media.MediaError):
                    media.transcribe_voice(b'data', model_path=path, enabled=True)
        decode.assert_not_called()

    def test_mock_whisper_gets_normalized_wav_and_returns_only_text(self):
        import wave
        seen = []

        def whisper(argv, **kwargs):
            self.assertEqual(argv[:3], [media._WHISPER, '-m', str(self.model)])
            self.assertIn('-nt', argv)
            self.assertIsNone(kwargs['data'])
            self.assertEqual(kwargs['limit'], media.MAX_TEXT_BYTES)
            wav = Path(argv[argv.index('-f') + 1])
            seen.append(wav)
            self.assertEqual(wav.parent.stat().st_mode & 0o777, 0o700)
            with wave.open(str(wav), 'rb') as audio:
                self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()),
                                 (16000, 1, 2))
                self.assertEqual(audio.readframes(10), b'\x00\x01' * 10)
            return b' Local transcript.\n'

        with (patch.object(media, '_installed', return_value=True),
              patch.object(media, '_decode_voice', return_value=b'\x00\x01' * 10),
              patch.object(media, '_run_helper', side_effect=whisper)):
            self.assertEqual(media.transcribe_voice(b'data', model_path=self.model, enabled=True),
                             'Local transcript.')
        self.assertFalse(seen[0].exists())

    def test_invalid_or_oversized_audio_never_reaches_ffmpeg(self):
        with patch.object(media, '_run_helper') as run:
            for data in (b'https://example.invalid/file', b'x' * (media.MAX_VOICE_BYTES + 1)):
                with self.subTest(prefix=data[:10]), self.assertRaises(media.MediaError):
                    media._decode_voice(data)
        run.assert_not_called()

    @unittest.skipUnless(Path('/usr/bin/ffmpeg').is_file(), 'ffmpeg unavailable')
    def test_real_ffmpeg_normalizes_ogg_opus(self):
        import subprocess
        source = subprocess.run(['/usr/bin/ffmpeg', '-hide_banner', '-loglevel', 'error',
                                 '-f', 's16le', '-ar', '48000', '-ac', '1', '-i', 'pipe:0',
                                 '-c:a', 'libopus', '-f', 'ogg', 'pipe:1'],
                                input=b'\x00\x00' * 4800, capture_output=True, timeout=5)
        if source.returncode != 0 and b'Unknown encoder' in source.stderr:
            self.skipTest('ffmpeg fixture encoder libopus unavailable')
        source.check_returncode()
        self.assertEqual(len(media._decode_voice(source.stdout)), 1600 * 2)


if __name__ == '__main__':
    unittest.main()
