"""Native, scrollback-friendly prompt composer for UTF-8 SSH terminals.

Enter submits; Ctrl+J or Alt+Enter inserts a newline. Use arrows, Home/End,
Backspace and Delete to edit. Tab cycles matching slash commands without
submitting. Ctrl+U clears the whole draft; Ctrl+C cancels and Ctrl+D on an
empty draft exits (otherwise deletes forward).

Bracketed paste inserts one sanitized draft (even for a single line). Review
or edit it, then press Enter to submit. Use the arrows to review clipped text.
Control keys inside the paste are data, not editing or submission events.
An oversized paste is discarded, not partially inserted. Plain mode and
unsupported terminals use input() and the caller's existing readline setup;
without bracketed paste, a terminal cannot distinguish pasted and typed keys.
"""

import codecs
import errno
import os
import re
import sys
import unicodedata

try:
    import termios
except ImportError:
    termios = None


DEFAULT_COMMANDS = ('/help', '/start', '/model', '/search', '/skills', '/status', '/time',
                    '/disk', '/new', '/exit')
_START = b'\x1b[200~'
_END = b'\x1b[201~'
_ENABLE = '\x1b[?2004h'
_DISABLE = '\x1b[?2004l'
_LIMIT = 64 * 1024
_TERM = re.compile(r'(?:xterm|screen|tmux|rxvt|alacritty|foot|kitty|wezterm|vte)(?:[-.].*)?')
_COMMAND = re.compile(r'/[a-zA-Z0-9_-]+')
# Match terminal control strings before stripping individual control characters.
_ESCAPE = re.compile(
    r'(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)'
    r'|(?:\x1b[P^_X]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c|$)'
    r'|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]'
    r'|\x1b[ -/]*[@-Z\\-_]'
)
_KEYS = {
    b'\x1b[A': 'up', b'\x1bOA': 'up', b'\x1b[B': 'down', b'\x1bOB': 'down',
    b'\x1b[C': 'right', b'\x1bOC': 'right', b'\x1b[D': 'left', b'\x1bOD': 'left',
    b'\x1b[H': 'home', b'\x1bOH': 'home', b'\x1b[1~': 'home', b'\x1b[7~': 'home',
    b'\x1b[F': 'end', b'\x1bOF': 'end', b'\x1b[4~': 'end', b'\x1b[8~': 'end',
    b'\x1b[3~': 'delete', b'\x04': 'delete', b'\x08': 'backspace', b'\x7f': 'backspace',
    b'\x1b\r': 'newline', b'\x1b\n': 'newline', b'\n': 'newline',
}


class Prompt(str):
    """Trimmed input carrying its eligibility for local command dispatch."""

    allow_commands: bool

    def __new__(cls, text, *, allow_commands):
        value = super().__new__(cls, text.strip())
        value.allow_commands = allow_commands
        return value


def _clean(text):
    return ''.join(c for c in text if c in '\n\t' or unicodedata.category(c)[0] != 'C')


def _clean_paste(raw):
    text = _ESCAPE.sub('', raw.decode('utf-8', errors='replace'))
    return _clean(text.replace('\r\n', '\n').replace('\r', '\n')
                      .replace('\u2028', '\n').replace('\u2029', '\n'))


def _read(fd):
    # Don't read ahead into the next prompt (which could be a readline prompt).
    try:
        key = os.read(fd, 1)
    except OSError as exc:
        if exc.errno != errno.EIO:
            raise
        raise EOFError from None  # Some PTYs report hangup as EIO.
    if not key:
        raise EOFError
    return key


def _key(fd):
    key = _read(fd)
    while key == b'\x1b':
        following = _read(fd)
        if following not in (b'[', b'O'):
            if following == b'\x1b':
                continue
            return key + following if following in (b'\r', b'\n') else following
        sequence = key + following
        # Wait for fragmented SSH reads. If a new ESC interrupts an invalid
        # sequence, restart so we can't lose a following paste-start marker.
        while True:
            part = _read(fd)
            if part in (b'\x03', b'\x04'):
                return part
            if part == b'\x1b':
                break
            if len(sequence) < 32:
                sequence += part
            if not b' ' <= part <= b'?':
                return sequence
    return key


def _paste(fd):
    raw = bytearray()
    tail = b''
    overflow = False
    while True:
        tail += _read(fd)
        if tail == _END:
            return None if overflow else _clean_paste(raw)
        while not _END.startswith(tail):
            if len(raw) < _LIMIT:
                raw.append(tail[0])
            else:
                overflow = True
            tail = tail[1:]


def _fit(text, width):
    """Clip a single display row without causing terminal autowrap."""
    used = 0
    for i, char in enumerate(text):
        used += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if used > width:
            return text[:i], True
    return text, False


def _line_view(line, cursor, width):
    left = ''
    # Keep the cursor in view without wrapping long source lines.
    prefix = line[:cursor].expandtabs(4)
    suffix = line[cursor:].expandtabs(4)
    _, overflow = _fit(prefix, width - 2)
    if overflow:
        visible, _ = _fit(prefix[::-1], width - 3)
        prefix = visible[::-1]
        left = '<'
    row, clipped = _fit(left + prefix + suffix, width - 1)
    return row + ('>' if clipped else ''), left + prefix


class _Composer:
    def __init__(self, prompt, commands, width, height):
        self.prompt = _fit(prompt, width // 2)[0]
        self.rows = min(6, max(1, height - 3))
        self.width = width
        self.commands = tuple(dict.fromkeys(c for c in commands if _COMMAND.fullmatch(c)))
        self.text = ''
        self.cursor = 0
        self.row = 0
        self.cycle = ()
        self.choice = -1
        self.pasted = False
        self.notice = ''

    def _matches(self):
        prefix = self.text.lstrip()
        if self.pasted or '\n' in self.text or not prefix.startswith('/') or any(map(str.isspace, prefix)):
            return ()
        return tuple(c for c in self.commands if c.startswith(prefix))

    def _complete(self):
        if not self.cycle:
            self.cycle = self._matches()
        if self.cycle:
            self.choice = (self.choice + 1) % len(self.cycle)
            self.text = self.cycle[self.choice]
            self.cursor = len(self.text)

    def _insert(self, text):
        self.text = self.text[:self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)

    def _line_bounds(self):
        start = self.text.rfind('\n', 0, self.cursor) + 1
        end = self.text.find('\n', self.cursor)
        return start, len(self.text) if end == -1 else end

    def _move(self, key):
        start, end = self._line_bounds()
        if key == 'left':
            self.cursor = max(0, self.cursor - 1)
        elif key == 'right':
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key == 'home':
            self.cursor = start
        elif key == 'end':
            self.cursor = end
        elif key == 'up' and start:
            previous = self.text.rfind('\n', 0, start - 1) + 1
            self.cursor = min(start - 1, previous + self.cursor - start)
        elif key == 'down' and end < len(self.text):
            next_end = self.text.find('\n', end + 1)
            self.cursor = min(len(self.text) if next_end == -1 else next_end,
                              end + 1 + self.cursor - start)

    def draw(self):
        lines = self.text.split('\n')
        start, _ = self._line_bounds()
        index = self.text.count('\n', 0, start)
        first = max(0, min(index - self.rows // 2, len(lines) - self.rows))
        shown = []
        cursor_prefix = ''
        for i in range(first, min(len(lines), first + self.rows)):
            prompt = self.prompt if i == 0 else ('... ' if i != first else '^.. ')
            # Leave safety margin for wide prompt characters and autowrap.
            available = self.width - sum(2 if unicodedata.east_asian_width(c) in ('W', 'F')
                                         else 1 for c in prompt)
            visible, prefix = _line_view(lines[i], self.cursor - start if i == index else 0, available)
            shown.append(prompt + visible)
            if i == index:
                cursor_prefix = prompt + prefix
        if self.notice:
            shown.append(self.notice)
        elif self.pasted:
            shown.append('Paste preview — Enter to submit; Ctrl+U to clear')
        elif matches := self.cycle or self._matches():
            shown.append('Suggestions: ' + ' '.join(matches))
        if first + self.rows < len(lines):
            shown.append('↓ More lines')
        up = f'\x1b[{self.row}A' if self.row else ''
        self.row = index - first
        down_rows = len(shown) - 1 - self.row
        back = f'\x1b[{down_rows}A' if down_rows else ''
        rendered = '\r\n'.join(_fit(line, self.width)[0] for line in shown)
        _write('\r' + up + '\x1b[J' + rendered + '\r' + back + cursor_prefix)

    def finish(self):
        up = f'\x1b[{self.row}A' if self.row else ''
        _write(_DISABLE + '\r' + up + '\x1b[J' + self.prompt + '\r\n')

    def edit(self, fd):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.draw()
        while True:
            key = _key(fd)
            if len(key) == 1 and key >= b' ' and key != b'\x7f':
                text = _clean(decoder.decode(key))
                if not text:
                    continue
            else:
                decoder.reset()
                text = ''

            if key == b'\x03':
                raise KeyboardInterrupt
            if key == b'\x04' and not self.text:
                raise EOFError
            if key == b'\r':
                return Prompt(self.text, allow_commands=not self.pasted and '\n' not in self.text)
            self.notice = ''
            if key != b'\t':
                self.cycle, self.choice = (), -1
            action = _KEYS.get(key)
            if key == _START:
                pasted = _paste(fd)
                self.pasted = True
                if pasted is None or len(pasted) + len(self.text) > _LIMIT:
                    self.notice = 'Paste too large — discarded; Ctrl+U to clear'
                else:
                    self._insert(pasted)
            elif key == b'\x15':
                self.text, self.cursor, self.pasted = '', 0, False
            elif key == b'\t':
                self._complete()
            elif action == 'backspace' and self.cursor:
                self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
                self.cursor -= 1
            elif action == 'delete':
                self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
            elif action == 'newline' or text:
                if len(self.text) < _LIMIT:
                    self._insert('\n' if action == 'newline' else text)
            elif action:
                self._move(action)
            self.draw()


def _write(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def _terminal():
    if termios is None or not _TERM.fullmatch(os.environ.get('TERM', '')):
        return None
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty() and
                codecs.lookup(sys.stdout.encoding or 'ascii').name == 'utf-8'):
            return None
        fd = sys.stdin.fileno()
        size = os.get_terminal_size(sys.stdout.fileno())
        if (size.columns and size.columns < 24) or (size.lines and size.lines < 4):
            return None
        return fd, termios.tcgetattr(fd), (size.columns or 80) - 1, size.lines or 24
    except (AttributeError, ValueError, OSError, termios.error):
        return None


def read_prompt(prompt, *, plain=False, commands=DEFAULT_COMMANDS):
    """Return a trimmed Prompt, or raise EOFError/KeyboardInterrupt like input.

    Check ``result.allow_commands`` BEFORE dispatching any local command,
    including in any downstream handler. If false, send as ordinary chat text.
    Read it before conversions such as ``str(result)`` or ``result.strip()``.
    Plain/fallback mode retains the caller's input() and readline behavior.
    """
    tty = None if plain else _terminal()
    if tty is None or termios is None:
        return Prompt(input(prompt), allow_commands=True)
    fd, original, width, height = tty
    mode = original[:]
    # Raw input, but leave output processing (including CR/LF) unchanged.
    mode[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INLCR | termios.IGNCR |
                 termios.ISTRIP | termios.IXON | termios.IXOFF | termios.PARMRK)
    mode[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.IEXTEN | termios.ISIG)
    mode[6] = original[6][:]
    mode[6][termios.VMIN] = 1
    mode[6][termios.VTIME] = 0
    lines = _clean(_ESCAPE.sub('', prompt)).expandtabs(4).split('\n')
    editor = _Composer(lines[-1], commands, width, height)
    try:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, mode)
        except (OSError, termios.error):
            editor = None
        if editor is not None:
            _write(''.join(line + '\r\n' for line in lines[:-1]) + _ENABLE)
            return editor.edit(fd)
    finally:
        # A hangup may make both cleanup operations fail; don't mask the
        # original exception. Always attempt restoration, even on write failure.
        try:
            if editor is not None:
                editor.finish()
        except OSError:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, original)
            except (OSError, termios.error):
                pass
    return Prompt(input(prompt), allow_commands=True)
