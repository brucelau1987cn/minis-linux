"""Telegram attachments for an already authenticated, fresh owner-private message.

The caller owns authorization, approvals, worker/cancellation policy and chat
history. Treat captions, extracted content and model replies as untrusted data,
not commands. This module does not log or persist media or submit audio to a
model provider. See individual functions for the transient image request and
shared Telegram outbound cooldown contracts.
"""
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import base64
import http.client
import io
import json
import os
import re
import secrets
import selectors
import stat
import struct
import subprocess
import tempfile
import time
import unicodedata
import wave
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request

from minis import NoRedirect

__all__ = ['Attachment', 'MediaError', 'identify_attachment', 'download_attachment',
           'parse_text', 'extract_pdf_text', 'image_part', 'analyze_image',
           'transcribe_voice', 'send_document', 'send_markdown']

MAX_TEXT_BYTES = 32 * 1024
MAX_PDF_BYTES = 5 * 1024 * 1024
# Leave room for base64/JSON overhead under the existing Provider's 2 MiB cap.
MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_SIDE = 8192
MAX_VOICE_BYTES = 2 * 1024 * 1024
MAX_VOICE_SECONDS = 120
MAX_CAPTION_CHARS = 1024

_LIMITS = {'text': MAX_TEXT_BYTES, 'pdf': MAX_PDF_BYTES,
           'image': MAX_IMAGE_BYTES, 'voice': MAX_VOICE_BYTES}
_FORMATS = {'.txt': ('text', 'text/plain'), '.md': ('text', 'text/markdown'),
            '.markdown': ('text', 'text/markdown'), '.csv': ('text', 'text/csv'),
            '.json': ('text', 'application/json'), '.log': ('text', 'text/plain'),
            '.pdf': ('pdf', 'application/pdf'), '.jpg': ('image', 'image/jpeg'),
            '.jpeg': ('image', 'image/jpeg'), '.png': ('image', 'image/png'),
            '.webp': ('image', 'image/webp')}
_FILE_ID = re.compile(r'[A-Za-z0-9_-]{1,512}')


class MediaError(ValueError):
    """Safe media error; messages never include credentials or raw media."""

    def __init__(self, message, *, code=None, retry_after=0):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


@dataclass(frozen=True)
class Attachment:
    kind: str
    file_id: str = field(repr=False)
    filename: str
    mime_type: str
    size: int | None
    caption: str = field(default='', repr=False)


def _filename(name):
    if (not isinstance(name, str) or not 1 <= len(name) <= 160
            or name.startswith('.') or '/' in name or '\\' in name
            or any(unicodedata.category(ch).startswith('C') for ch in name)
            or len(name.encode('utf-8')) > 240):
        raise MediaError('附件文件名无效')
    return name


def _format(name):
    result = _FORMATS.get(PurePosixPath(_filename(name)).suffix.lower())
    if result is None:
        raise MediaError('不支持此附件格式；请发送 UTF-8 文本、PDF 或 JPG/PNG/WebP')
    return result


def _declared_size(size, limit):
    if size is not None and (type(size) is not int or not 0 < size <= limit):
        raise MediaError('附件大小无效或超过限制')


def _file_id(value):
    if not isinstance(value, str) or not _FILE_ID.fullmatch(value):
        raise MediaError('Telegram 附件 ID 无效')
    return value


def _dimensions(w, h):
    return (type(w) is int and type(h) is int and 0 < w <= MAX_IMAGE_SIDE
            and 0 < h <= MAX_IMAGE_SIDE and w * h <= MAX_IMAGE_PIXELS)


def _describe(kind, info, name, mime, caption):
    if not isinstance(info, dict):
        raise MediaError('Telegram 附件信息无效')
    size = info.get('file_size')
    _declared_size(size, _LIMITS[kind])
    return Attachment(kind, _file_id(info.get('file_id')), name, mime, size, caption)


def identify_attachment(message, *, voice_enabled=False):
    """Classify an authorized Telegram *message* (not update).

    Return Attachment or None (no photo/document/voice); raise MediaError for
    an unsupported or malformed attachment in those fields. Caption is data,
    never a command. No I/O; disabled voice fails before any download.
    """
    if not isinstance(message, dict):
        return None
    keys = [key for key in ('photo', 'document', 'voice') if key in message]
    if not keys:
        return None
    if len(keys) != 1:
        raise MediaError('一条消息只能处理一个附件')
    key = keys[0]
    info = message[key]
    caption = message.get('caption', '')
    if (not isinstance(caption, str) or len(caption) > MAX_CAPTION_CHARS
            or any(unicodedata.category(ch) in ('Cc', 'Cs') and ch not in '\r\n\t'
                   for ch in caption)):
        raise MediaError('附件说明无效或超过限制')
    if key == 'photo':
        if not isinstance(info, list) or not 1 <= len(info) <= 32:
            raise MediaError('Telegram 图片信息无效')
        best, score = None, (0, 0)
        for photo in info:
            if not isinstance(photo, dict) or not _dimensions(photo.get('width'), photo.get('height')):
                continue
            try:
                candidate = _describe('image', photo, 'photo.jpg', 'image/jpeg', caption)
            except MediaError:
                continue
            rank = (photo['width'] * photo['height'], candidate.size or 0)
            if rank > score:
                best, score = candidate, rank
        if best is None:
            raise MediaError('没有符合大小限制的 Telegram 图片')
        return best
    if key == 'voice' and voice_enabled is not True:
        raise MediaError('本地语音转写尚未启用')
    if not isinstance(info, dict):
        raise MediaError('Telegram 附件信息无效')
    mime = info.get('mime_type')
    if key == 'voice':
        seconds = info.get('duration')
        if (type(seconds) is not int or not 0 <= seconds <= MAX_VOICE_SECONDS
                or mime not in (None, 'audio/ogg', 'audio/opus')):
            raise MediaError('仅接受不超过 120 秒的 Telegram Ogg/Opus 语音')
        return _describe('voice', info, 'voice.ogg', 'audio/ogg', caption)
    name = _filename(info.get('file_name'))
    kind, canonical = _format(name)
    allowed = (None, 'application/octet-stream', canonical)
    if mime not in allowed and not (kind == 'text' and mime == 'text/plain'):
        raise MediaError('附件类型与文件扩展名不匹配')
    return _describe(kind, info, name, canonical, caption)


def _bytes(data, limit):
    if not isinstance(data, bytes) or not 0 < len(data) <= limit:
        raise MediaError('附件为空或超过大小限制')
    return data


def _decode_text(data, *, formfeed=False):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise MediaError('附件不是有效的 UTF-8 文本') from None
    allowed = '\t\r\n\f' if formfeed else '\t\r\n'
    if any(unicodedata.category(ch) == 'Cc' and ch not in allowed for ch in text):
        raise MediaError('附件包含非文本控制字符')
    return text


def parse_text(data):
    """Decode strict UTF-8 (optional BOM), without trimming document content."""
    text = _decode_text(_bytes(data, MAX_TEXT_BYTES))
    if not text.strip():
        raise MediaError('文本附件没有可读内容')
    return text


_ORIGIN = 'https://api.telegram.org'
_BOT_URL = re.compile(re.escape(_ORIGIN) + r'/bot([1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,100})/')
_PATH_PART = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}')
_HTTP_TIMEOUT = 15
_HTTP_ERROR_LIMIT = 8192
_HTTP_REPLY_LIMIT = 1024 * 1024


def _telegram_token(telegram):
    # The baseline Telegram exposes its validated token only in .url. Accept
    # that exact URL form and fail closed if a caller swaps in a redirecting
    # or proxying urllib opener. The client itself is trusted local code.
    url = getattr(telegram, 'url', None)
    match = _BOT_URL.fullmatch(url) if isinstance(url, str) else None
    handlers = getattr(getattr(telegram, 'opener', None), 'handlers', ())
    redirects = [h for h in handlers if isinstance(h, HTTPRedirectHandler)]
    if (match is None or len(redirects) != 1 or type(redirects[0]) is not NoRedirect
            or any(isinstance(h, ProxyHandler) and getattr(h, 'proxies', True) for h in handlers)):
        raise MediaError('Telegram 媒体需要官方 HTTPS 接口及禁用代理和重定向的客户端')
    return match[1]


def _read_download(telegram, url, limit, sizes):
    req = Request(url, method='GET', headers={'Accept-Encoding': 'identity'})
    with telegram.opener.open(req, timeout=_HTTP_TIMEOUT) as response:
        if (response.status != 200 or response.geturl() != url
                or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
            raise MediaError('Telegram 附件响应无效')
        lengths = response.headers.get_all('Content-Length', [])
        if lengths:
            if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,12}', lengths[0]):
                raise MediaError('Telegram 附件大小无效')
            size = int(lengths[0])
            _declared_size(size, limit)
            sizes.append(size)
        data = _bytes(response.read(limit + 1), limit)
        if any(size != len(data) for size in sizes):
            raise MediaError('Telegram 附件大小与实际内容不符')
        return data


def download_attachment(telegram, attachment):
    """Call getFile, then fetch bounded bytes from the official Telegram origin.

    Pass the existing Telegram client (request/opener/url) and a descriptor
    from identify_attachment. TelegramError from getFile retains status and
    retry_after; content/transport errors use MediaError. No redirects/retries.
    """
    token = _telegram_token(telegram)
    if (not isinstance(attachment, Attachment) or not isinstance(attachment.kind, str)
            or attachment.kind not in _LIMITS):
        raise MediaError('无效的附件描述')
    limit = _LIMITS[attachment.kind]
    _declared_size(attachment.size, limit)
    try:
        info = telegram.request('getFile', {'file_id': _file_id(attachment.file_id)}, timeout=_HTTP_TIMEOUT)
        path = info.get('file_path') if isinstance(info, dict) else None
        if (not isinstance(path, str) or len(path) > 512
                or not all(_PATH_PART.fullmatch(part) for part in path.split('/'))):
            raise MediaError('Telegram 返回了无效的附件路径')
        size = info.get('file_size')
        _declared_size(size, limit)
        sizes = [s for s in (attachment.size, size) if s is not None]
        if len(set(sizes)) > 1:
            raise MediaError('Telegram 附件大小信息不一致')
        return _read_download(telegram, f'{_ORIGIN}/file/bot{token}/{path}', limit, sizes)
    except HTTPError as exc:
        exc.close()
        raise MediaError(f'Telegram 附件下载失败（HTTP {exc.code}）') from None
    except (OSError, http.client.HTTPException):
        raise MediaError('Telegram 附件下载失败') from None


def _pdf_header(data):
    _bytes(data, MAX_PDF_BYTES)
    if not re.match(rb'%PDF-[12]\.[0-9](?:\r|\n)', data):
        raise MediaError('附件不是有效的 PDF 格式')


def validated_document_mime(filename, data):
    kind, mime = _format(filename)
    if '"' in filename:
        raise MediaError('附件文件名不能包含引号')
    if kind == 'text':
        parse_text(data)
    elif kind == 'pdf':
        _pdf_header(data)
    else:
        _verify_image(data, mime)
    return mime


def snapshot_document(workspace, filename):
    """Freeze one bounded, validated top-level workspace file for approval."""
    workspace.validate_name(filename)
    kind, _ = _format(filename)
    limit = _LIMITS[kind]
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=workspace.fd)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise MediaError('不发送特殊文件或硬链接')
        _declared_size(before.st_size, limit)
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (before.st_size != len(data) or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns or after.st_nlink != 1):
            raise MediaError('文件在预览时发生变化；请重新申请')
    return data, validated_document_mime(filename, data)


def send_document(telegram, owner, filename, data):
    """Send already approved bytes to the authorized owner, never read a path.

    Uses the Telegram client's no-proxy/no-redirect opener, shared
    send_limit_lock/send_after, and one bounded sendDocument request. On 429,
    uses the larger of integer Retry-After header and JSON retry_after (1–3600
    seconds); updated cooldown applies to text and documents. No retry, no
    sleep. Returns Telegram's result dict; raises MediaError (code/retry_after
    available for Telegram failures).
    The parent must gate approval and must supply the same approved bytes.
    """
    token = _telegram_token(telegram)
    if type(owner) is not int or not 0 < owner < 2**63:
        raise MediaError('无效的 Telegram 所有者 ID')
    mime = validated_document_mime(filename, data)
    # Browser-style UTF-8 filename; CR/LF, quotes, slashes and other unsafe
    # filename characters have been rejected. Do not add a parse_mode.
    boundary = secrets.token_hex(24)
    while boundary.encode() in data or boundary in filename:
        boundary = secrets.token_hex(24)
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{owner}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f'Content-Type: {mime}\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    req = Request(f'{_ORIGIN}/bot{token}/sendDocument', data=body, method='POST',
                  headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    with telegram.send_limit_lock:
        if time.monotonic() < telegram.send_after:
            raise MediaError('Telegram sendDocument 尚在冷却中', code=429)
    status, header, raw = None, None, b''
    try:
        with telegram.opener.open(req, timeout=_HTTP_TIMEOUT) as response:
            if response.geturl() != req.full_url:
                raise MediaError('Telegram sendDocument 响应无效')
            header = response.headers.get('Retry-After')
            raw = response.read(_HTTP_REPLY_LIMIT + 1)
    except HTTPError as exc:
        status = exc.code
        with exc:
            header = exc.headers.get('Retry-After')
            try:
                raw = exc.read(_HTTP_ERROR_LIMIT)
            except (OSError, http.client.HTTPException):
                pass
    except (OSError, http.client.HTTPException):
        raise MediaError('Telegram sendDocument 失败') from None
    try:
        reply = json.loads(raw) if len(raw) <= _HTTP_REPLY_LIMIT else None
    except (ValueError, RecursionError):
        reply = None
    retry = 0
    if isinstance(reply, dict):
        if status is None and reply.get('ok') is True and isinstance(reply.get('result'), dict):
            return reply['result']
        code = reply.get('error_code')
        if status is None and type(code) is int and 100 <= code <= 599:
            status = code
        parameters = reply.get('parameters')
        seconds = parameters.get('retry_after') if isinstance(parameters, dict) else None
        if type(seconds) is int:
            retry = seconds
    if status == 429:
        seconds = int(header) if header and re.fullmatch(r'[0-9]{1,10}', header) else 0
        retry = min(3600, max(1, retry, seconds))
        with telegram.send_limit_lock:
            telegram.send_after = max(telegram.send_after, time.monotonic() + retry)
    raise MediaError('Telegram sendDocument 失败' + (f'（{status}）' if status else ''),
                     code=status, retry_after=retry if status == 429 else 0)


def send_markdown(telegram, owner, markdown, *, filename='history.md'):
    """Send approved, caller-reviewed/redacted Markdown (no automatic redaction)."""
    if (_format(filename)[1] != 'text/markdown' or not isinstance(markdown, str)
            or len(markdown) > MAX_TEXT_BYTES):
        raise MediaError('Markdown 导出无效或超过大小限制')
    try:
        data = markdown.encode('utf-8')
    except UnicodeError:
        raise MediaError('Markdown 导出不是有效的 UTF-8 文本') from None
    return send_document(telegram, owner, filename, data)


_PDFTOTEXT = '/usr/bin/pdftotext'


def _run_helper(argv, *, data, limit, timeout):
    """Capture stdout with a hard byte cap and wall deadline on Linux."""
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        with ExitStack() as stack:
            source = subprocess.DEVNULL
            if data is not None:
                source = stack.enter_context(tempfile.TemporaryFile())
                source.write(data)
                source.seek(0)
            child = stack.enter_context(subprocess.Popen(
                argv, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'HOME': '/nonexistent'}))
            try:
                selector = stack.enter_context(selectors.DefaultSelector())
                assert child.stdout is not None
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise MediaError('本地媒体处理超时')
                    block = os.read(child.stdout.fileno(), min(65536, limit + 1 - len(output)))
                    if not block:
                        break
                    output.extend(block)
                    if len(output) > limit:
                        raise MediaError('本地媒体处理输出超过限制')
                if child.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                    raise MediaError('本地媒体处理失败；文件可能损坏或不受支持')
            finally:
                if child.poll() is None:
                    child.kill()
    except subprocess.TimeoutExpired:
        raise MediaError('本地媒体处理超时') from None
    except OSError:
        raise MediaError('无法运行本地媒体处理程序') from None
    return bytes(output)


def _installed(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def extract_pdf_text(data):
    """Extract an existing PDF text layer locally (5 MiB in, 32 KiB out, 10 s).

    No OCR or password handling. A missing utility, malformed or textless PDF,
    or an exceeded limit raises MediaError; no partial result is returned.
    """
    _pdf_header(data)
    if not _installed(_PDFTOTEXT):
        raise MediaError('PDF 文本提取需要 /usr/bin/pdftotext')
    raw = _run_helper([_PDFTOTEXT, '-q', '-enc', 'UTF-8', '-eol', 'unix', '-', '-'],
                      data=data, limit=MAX_TEXT_BYTES, timeout=10)
    text = _decode_text(raw, formfeed=True).replace('\f', '\n').strip()
    if not text:
        raise MediaError('PDF 没有可提取的文本层；不支持扫描件 OCR')
    return text


_IMAGE_MIME = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}


def _check_webp(data):
    # Some Pillow/libwebp builds allocate the WebP canvas in Image.open(),
    # before Pillow exposes its dimensions. Check RIFF and both canvas and
    # frame dimensions before invoking the decoder; Pillow still verifies pixels.
    invalid = 'WebP 图片损坏、包含动画或尺寸超过限制'
    if len(data) < 20 or data[8:12] != b'WEBP' or int.from_bytes(data[4:8], 'little') + 8 != len(data):
        raise MediaError(invalid)
    offset, frame = 12, False
    while offset + 8 <= len(data):
        tag, size = struct.unpack_from('<4sI', data, offset)
        start = offset + 8
        offset = start + size + (size & 1)
        if offset > len(data) or tag in (b'ANIM', b'ANMF'):
            raise MediaError(invalid)
        head = data[start:start + min(10, size)]
        if tag == b'VP8X':
            if start != 20 or size != 10 or head[0] & 2:
                raise MediaError(invalid)
            width = 1 + int.from_bytes(head[4:7], 'little')
            height = 1 + int.from_bytes(head[7:10], 'little')
        elif tag == b'VP8 ' and size >= 10 and head[3:6] == b'\x9d\x01\x2a':
            width, height = (n & 0x3fff for n in struct.unpack_from('<HH', head, 6))
        elif tag == b'VP8L' and size >= 5 and head[0] == 0x2f:
            bits = struct.unpack_from('<I', head, 1)[0]
            width, height = (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
        elif tag in (b'VP8 ', b'VP8L'):
            raise MediaError(invalid)
        else:
            continue
        if not _dimensions(width, height) or (tag != b'VP8X' and frame):
            raise MediaError(invalid)
        frame |= tag != b'VP8X'
    if not frame or offset != len(data):
        raise MediaError(invalid)


def _verify_image(data, expected):
    _bytes(data, MAX_IMAGE_BYTES)
    if data.startswith(b'RIFF') or expected == 'image/webp':
        _check_webp(data)
    try:
        from PIL import Image
    except ImportError:
        raise MediaError('图片验证需要 Pillow') from None
    try:
        with Image.open(io.BytesIO(data), formats=tuple(_IMAGE_MIME)) as image:
            mime = _IMAGE_MIME.get(image.format or '')
            if (mime is None or (expected is not None and mime != expected)
                    or not _dimensions(*image.size) or getattr(image, 'n_frames', 1) != 1):
                raise MediaError('图片类型不符、包含动画或尺寸超过限制')
            image.verify()
        # verify() alone does not decode JPEG/WebP pixel data.
        with Image.open(io.BytesIO(data), formats=tuple(_IMAGE_MIME)) as image:
            image.load()
    except (OSError, SyntaxError, ValueError, OverflowError, IndexError, RuntimeError, EOFError, struct.error,
            Image.DecompressionBombError) as exc:
        if isinstance(exc, MediaError):
            raise
        raise MediaError('图片损坏或不受支持') from None
    return mime


def image_part(data, *, mime_type=None):
    """Build a verified OpenAI image_url part for a transient request only.

    Accepts static JPG/PNG/WebP up to 1 MiB; MIME, if supplied, must match.
    Do not log this object or append it to chat history.
    """
    mime = _verify_image(data, mime_type)
    encoded = base64.b64encode(data).decode('ascii')
    return {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{encoded}'}}


def analyze_image(provider, data, prompt, *, mime_type=None, vision_enabled=False):
    """Send a single transient multimodal turn to the selected vision provider.

    The caller must confirm that the model supports vision and disclose the
    image transfer. Do not pass this through the normal history/tool runner.
    Calls Provider.reply(messages) once; returns bounded plain text only.
    Use a non-logging provider; the remote service may retain the request.
    """
    if vision_enabled is not True:
        raise MediaError('当前模型未启用图片分析')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4096:
        raise MediaError('图片分析提示无效或超过限制')
    part = image_part(data, mime_type=mime_type)
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': prompt}, part]}]
    try:
        result = provider.reply(messages)
    except (ValueError, OSError, http.client.HTTPException):
        raise MediaError('图片分析失败；请检查模型是否支持图片') from None
    text = result.get('content') if isinstance(result, dict) else None
    if (not isinstance(result, dict) or not isinstance(text, str) or not text.strip() or len(text) > 8192
            or result.get('tool_calls') or result.get('function_call')
            or any(unicodedata.category(ch) in ('Cc', 'Cs') and ch not in '\t\r\n' for ch in text)
            or 'data:image/' in text.lower() or part['image_url']['url'].split(',', 1)[1] in text):
        raise MediaError('图片模型没有返回有效文本')
    return text


_FFMPEG = '/usr/bin/ffmpeg'
_WHISPER = '/usr/local/bin/whisper-cli'
_VOICE_RATE = 16000


def _decode_voice(data):
    _bytes(data, MAX_VOICE_BYTES)
    # First Ogg page must begin an Opus stream. FFmpeg checks the container.
    segments = data[26] if len(data) > 26 else 0
    if (not data.startswith(b'OggS\x00\x02') or not segments
            or not data.startswith(b'OpusHead', 27 + segments)):
        raise MediaError('仅支持 Telegram Ogg/Opus 语音')
    if not _installed(_FFMPEG):
        raise MediaError('本地语音转写需要 /usr/bin/ffmpeg')
    # Decode at most one extra second so longer clips fail the output cap
    # instead of silently transcribing a prefix. Input/output are pipe-only.
    pcm = _run_helper([_FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
                       '-protocol_whitelist', 'pipe', '-f', 'ogg', '-c:a', 'opus', '-threads', '1',
                       '-i', 'pipe:0', '-map', '0:a:0', '-vn', '-sn', '-dn',
                       '-t', str(MAX_VOICE_SECONDS + 1), '-ac', '1', '-ar', str(_VOICE_RATE),
                       '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'],
                      data=data, limit=MAX_VOICE_SECONDS * _VOICE_RATE * 2, timeout=15)
    if not pcm or len(pcm) % 2:
        raise MediaError('语音没有可解码的音频')
    return pcm


def transcribe_voice(data, *, model_path, enabled=False):
    """Opt-in local-only STT; return text for a direct Telegram reply.

    Set enabled=True only from local operator configuration. model_path is
    an operator-controlled absolute path to an already-installed whisper.cpp
    model; never accept it from Telegram, the workspace, or a language model.
    Requires /usr/bin/ffmpeg and /usr/local/bin/whisper-cli. No downloads or
    cloud fallback. A private temporary WAV is removed after use (not secure
    erasure, and abrupt process termination may leave the temporary file).
    """
    if enabled is not True:
        raise MediaError('本地语音转写尚未启用')
    try:
        model = Path(model_path)
        usable = model.is_absolute() and model.is_file() and os.access(model, os.R_OK)
    except (TypeError, ValueError, OSError):
        raise MediaError('请配置本地可信的 whisper.cpp 模型文件') from None
    if not usable:
        raise MediaError('请配置本地可信的 whisper.cpp 模型文件')
    if not _installed(_WHISPER):
        raise MediaError('本地语音转写需要 /usr/local/bin/whisper-cli')
    pcm = _decode_voice(data)
    try:
        with tempfile.TemporaryDirectory(prefix='minis-voice-') as directory:
            audio = str(Path(directory) / 'voice.wav')
            with wave.open(audio, 'wb') as wav:
                wav.setparams((1, 2, _VOICE_RATE, 0, 'NONE', 'not compressed'))
                wav.writeframes(pcm)
            raw = _run_helper([_WHISPER, '-m', str(model), '-f', audio, '-l', 'auto',
                               '-nt', '-np', '-ng', '-t', '2'],
                              data=None, limit=MAX_TEXT_BYTES, timeout=120)
    except (OSError, wave.Error):
        raise MediaError('无法处理临时语音文件') from None
    text = _decode_text(raw).strip()
    if not text:
        raise MediaError('未识别到可转写的语音')
    return text
