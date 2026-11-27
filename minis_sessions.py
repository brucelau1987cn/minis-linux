"""Local named sessions. The caller holds the existing entry-point state lock.

Pass the original, trusted history path (including bot and owner identity) in
an existing state directory. Serialize session changes with history writes;
for a worker turn, keep its history path fixed until commit or cancellation.
This module does not write transcripts or expose anything to the model.
"""

import json
import os
import re
import secrets
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from minis import HISTORY_LIMIT

MAX_SESSIONS = 32  # includes the original session
MAX_TITLE = 80
MAX_INDEX_BYTES = 64 * 1024
MAX_SEARCH_BYTES = 16 * 1024 * 1024
MAX_QUERY = 200
MAX_HITS = 20
MAX_SNIPPET = 180
MAX_EXPORT_BYTES = 512 * 1024
EXPORT_REMINDER = '已尽力遮盖常见密钥；分享前请检查是否仍含敏感内容。'
_TRUNCATED = '\n\n> 导出达到大小上限，后续内容已截断。\n'
INDEX = 'index.json'
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW


class SessionError(ValueError):
    """A user-facing session error; no raw state or transcript in the message."""


@dataclass(frozen=True)
class Session:
    id: int
    title: str
    active: bool


@dataclass(frozen=True)
class SearchHit:
    session_id: int
    title: str
    role: str
    message_index: int  # original 1-based history position
    snippet: str


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    skipped_ids: tuple[int, ...]  # unreadable, invalid, or over byte budget


@dataclass(frozen=True)
class _Index:
    active: int = 1
    titles: tuple[str, ...] = ('默认会话',)

    def session(self, id):
        return Session(id, self.titles[id - 1], id == self.active)


def _title(text):
    if (not isinstance(text, str) or not 1 <= len(text) <= MAX_TITLE
            or not text.strip() or any(unicodedata.category(c)[0] == 'C' or c in '\u2028\u2029' for c in text)):
        raise SessionError('会话标题须为 1–80 字符的单行文本。')
    return text.strip()


def _id(value, state):
    if value is None:
        return state.active
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,8}', value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= len(state.titles):
        raise SessionError('会话 ID 无效或不存在。')
    return value


def _regular(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _check_history(folder, name, *, unused=False):
    try:
        info = os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return
    if unused or not _regular(info):
        raise SessionError('会话文件位置不安全或已被占用。')


def _read_at(folder, name, limit, *, private=False):
    """Read only a bounded regular file without following links."""
    fd = os.open(name, FILE_FLAGS, dir_fd=folder)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if not _regular(info) or (private and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
            raise SessionError('会话文件位置或权限不安全。')
        if info.st_size > limit:
            raise SessionError('会话文件超过大小上限。')
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise SessionError('会话文件超过大小上限。')
    return raw


def _decode(raw):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise SessionError('会话文件格式无效。') from None


def _history(raw):
    data = _decode(raw)
    if not isinstance(data, list):
        raise SessionError('会话历史格式无效。')
    return data


def _messages(data, *, newest_first=False):
    positions = range(len(data) - 1, -1, -1) if newest_first else range(len(data))
    for pos in positions:
        msg = data[pos]
        if isinstance(msg, dict):
            role, content = msg.get('role'), msg.get('content')
            if role in ('user', 'assistant') and isinstance(content, str) and content.strip():
                yield pos + 1, role, content


_PRIVATE_KEY = re.compile(
    r'-----BEGIN [A-Z0-9 ]{0,32}PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]{0,32}PRIVATE KEY-----|\Z)')
_HEADER = re.compile(r'(?i)(?<![\w-])((?:proxy-authorization|authorization|set-cookie|cookie)["\']?[ \t]*:[ \t]*)[^\r\n]+')
_AUTH = re.compile(r'(?i)\b(?:bearer|basic)[ \t]+[a-z0-9._~+/=-]+')
_URL_AUTH = re.compile(r'(?i)\b(https?://)[^\s/@]{1,256}:[^\s/@]{1,256}@')
_ASSIGNMENT = re.compile(
    r'''(?ix)((?<![^_\W])(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|token|passw(?:or)?d|'''
    r'''secret|authorization|cookie)["']?[ \t]*[:=][ \t]*)'''
    r'''(?:"(?:\\[^\r\n]|[^"\\\r\n])*"|'(?:\\[^\r\n]|[^'\\\r\n])*'|[^\s,;"'<>]+)''')
_TOKEN = re.compile(
    r'(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|'
    r'github_pat_[A-Za-z0-9_]{16,}|[0-9]{6,12}:[A-Za-z0-9_-]{30,}|'
    r'(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{10,})')


def _redact(text):
    text = _PRIVATE_KEY.sub('[已遮盖]', text)
    text = _HEADER.sub(r'\g<1>[已遮盖]', text)
    text = _AUTH.sub('[已遮盖]', text)
    text = _URL_AUTH.sub(r'\g<1>[已遮盖]@', text)
    text = _ASSIGNMENT.sub(r'\g<1>[已遮盖]', text)
    return _TOKEN.sub('[已遮盖]', text)


def _visible(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u2028', '\n').replace('\u2029', '\n')
    return ''.join(c if c in '\n\t' or unicodedata.category(c)[0] != 'C' else '\ufffd' for c in text)


def _snippet(text, needle):
    text = ' '.join(_visible(_redact(text)).split())
    folded = text.casefold()
    pos = folded.find(needle)
    if pos < 0:
        return None
    if len(text) <= MAX_SNIPPET:
        return text
    # Casefold may expand characters (e.g. ß); use the matching original offset.
    if len(folded) != len(text):
        offset = 0
        for index, c in enumerate(text):
            offset += len(c.casefold())
            if offset > pos:
                pos = index
                break
    start = max(0, pos - MAX_SNIPPET // 4)
    end = min(len(text), start + MAX_SNIPPET - 2)
    return ('…' if start else '') + text[start:end] + ('…' if end < len(text) else '')


class Sessions:
    def __init__(self, legacy_history_path):
        self.legacy_path = Path(legacy_history_path).absolute()
        if not self.legacy_path.name:
            raise SessionError('无效的原始会话路径。')
        self._store = self.legacy_path.with_name(self.legacy_path.name + '.sessions')

    @contextmanager
    def _scope(self, *, create=False):
        # The caller owns the parent state directory. Do not follow links into
        # the session store, or trust a directory writable by another user.
        try:
            parent = os.open(self.legacy_path.parent, DIR_FLAGS)
            try:
                name = self._store.name
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                try:
                    store = os.open(name, DIR_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    if create:
                        raise
                    store = None
                try:
                    if store is not None:
                        info = os.fstat(store)
                        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                            raise SessionError('会话目录权限不安全。')
                    yield parent, store
                finally:
                    if store is not None:
                        os.close(store)
            finally:
                os.close(parent)
        except OSError:
            raise SessionError('无法访问或保存会话状态；未重置旧会话。') from None

    @staticmethod
    def _load(store):
        if store is None:
            return _Index()
        try:
            data = _decode(_read_at(store, INDEX, MAX_INDEX_BYTES, private=True))
        except FileNotFoundError:
            return _Index()
        if (not isinstance(data, dict) or data.keys() != {'version', 'active', 'titles'}
                or type(data['version']) is not int or data['version'] != 1
                or not isinstance(data['titles'], list) or not 1 <= len(data['titles']) <= MAX_SESSIONS
                or type(data['active']) is not int or not 1 <= data['active'] <= len(data['titles'])):
            raise SessionError('会话索引无效；未重置旧会话。')
        return _Index(data['active'], tuple(_title(title) for title in data['titles']))

    @staticmethod
    def _save(store, state):
        raw = json.dumps({'version': 1, 'active': state.active, 'titles': state.titles},
                         ensure_ascii=False).encode('utf-8')
        if len(raw) > MAX_INDEX_BYTES:
            raise SessionError('会话索引超过大小上限。')
        temp = f'.{INDEX}.{secrets.token_hex(8)}.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=store)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, INDEX, src_dir_fd=store, dst_dir_fd=store)
        finally:
            try:
                os.unlink(temp, dir_fd=store)
            except FileNotFoundError:
                pass

    def _location(self, parent, store, id):
        return (parent, self.legacy_path) if id == 1 else (store, self._store / f'{id}.json')

    @property
    def active_path(self) -> Path:
        with self._scope() as (parent, store):
            folder, path = self._location(parent, store, self._load(store).active)
            _check_history(folder, path.name)
            return path

    def list(self) -> list[Session]:
        """List sessions by stable numeric ID without reading transcripts."""
        with self._scope() as (_, store):
            state = self._load(store)
            return [state.session(id) for id in range(1, len(state.titles) + 1)]

    def create(self, title=None) -> Session:
        """Activate a fresh, empty session. The history is only saved by chat."""
        if title is not None:
            title = _title(title)
        with self._scope(create=True) as (parent, store):
            state = self._load(store)
            id = len(state.titles) + 1
            if id > MAX_SESSIONS:
                raise SessionError('已达会话数量上限。')
            folder, path = self._location(parent, store, id)
            _check_history(folder, path.name, unused=True)
            state = _Index(id, (*state.titles, title if title is not None else f'会话 {id}'))
            self._save(store, state)
            return state.session(id)

    def title(self, title, session_id=None) -> Session:
        """Rename the active session (or a given numeric ID)."""
        title = _title(title)
        with self._scope(create=True) as (_, store):
            state = self._load(store)
            id = _id(session_id, state)
            state = _Index(state.active, state.titles[:id - 1] + (title,) + state.titles[id:])
            self._save(store, state)
            return state.session(id)

    def switch(self, session_id) -> Session:
        with self._scope() as (parent, store):
            state = self._load(store)
            id = _id(session_id, state)
            folder, path = self._location(parent, store, id)
            _check_history(folder, path.name)
            if id != state.active:
                state = _Index(id, state.titles)
                self._save(store, state)
            return state.session(id)

    def search(self, query, *, session_id=None, limit=10) -> SearchResult:
        """Search visible text only, newest session ID and message position first.

        None searches all indexed sessions in this scope; specify an ID to
        search only that session. Stop reading once the requested limit is met.
        """
        if not isinstance(query, str) or not 1 <= len(query) <= MAX_QUERY or not query.strip():
            raise SessionError('搜索词须为 1–200 字符。')
        if type(limit) is not int or not 1 <= limit <= MAX_HITS:
            raise SessionError('搜索结果上限须为 1–20。')
        needle = ' '.join(query.split()).casefold()
        hits, skipped, budget = [], [], MAX_SEARCH_BYTES
        with self._scope() as (parent, store):
            state = self._load(store)
            ids = (range(len(state.titles), 0, -1) if session_id is None else (_id(session_id, state),))
            for id in ids:
                folder, path = self._location(parent, store, id)
                try:
                    raw = _read_at(folder, path.name, min(budget, HISTORY_LIMIT))
                    budget -= len(raw)
                    messages = _history(raw)
                except FileNotFoundError:
                    continue  # An empty session need not have a history file.
                except (SessionError, OSError):
                    skipped.append(id)
                    continue
                for pos, role, text in _messages(messages, newest_first=True):
                    if needle not in ' '.join(text.split()).casefold():
                        continue
                    snippet = _snippet(text, needle)
                    if snippet is not None:
                        hits.append(SearchHit(id, _redact(state.titles[id - 1]), role, pos, snippet))
                        if len(hits) == limit:
                            return SearchResult(tuple(hits), tuple(skipped))
        return SearchResult(tuple(hits), tuple(skipped))

    def export(self, session_id=None) -> str:
        """Return Markdown for exactly one session (active by default); no file or delivery."""
        with self._scope() as (parent, store):
            state = self._load(store)
            id = _id(session_id, state)
            folder, path = self._location(parent, store, id)
            try:
                messages = _history(_read_at(folder, path.name, HISTORY_LIMIT))
            except FileNotFoundError:
                messages = []
        # Indented code blocks render messages and titles literally, so chat
        # text cannot insert Markdown links/images or raw HTML into the export.
        title = _redact(state.titles[id - 1])
        report = [f'# Minis 会话 {id}\n\n    {title}\n\n> {EXPORT_REMINDER}\n']
        budget = MAX_EXPORT_BYTES - len(report[0].encode('utf-8')) - len(_TRUNCATED.encode('utf-8'))
        for _, role, text in _messages(messages):
            heading = f'\n## {role.capitalize()}\n\n'
            text = _visible(_redact(text))
            # Slice only after redaction, and bound encoded output, not merely characters.
            body = (heading + '    ' + text[:budget].replace('\n', '\n    ') + '\n').encode('utf-8')
            if len(body) > budget:
                if budget > len(heading) + 4:
                    report.append(body[:budget].decode('utf-8', 'ignore').rstrip())
                report.append(_TRUNCATED)
                break
            report.append(body.decode('utf-8'))
            budget -= len(body)
        if len(report) == 1:
            report.append('\n没有可导出的消息。\n')
        return ''.join(report)
