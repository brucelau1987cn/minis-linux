#!/usr/bin/env python3
"""Minis Linux terminal chat and stable-version updater."""
import argparse
from contextlib import contextmanager
import fcntl
import http.client
from io import TextIOWrapper
import json
import locale
import os
from pathlib import Path
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import termios
from time import monotonic
from typing import TypeGuard
from unicodedata import category, east_asian_width
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request

import minis_input
from minis import HTTP_LIMIT, Provider, read_history, safe_display, save_history
from minis_search import search_web
from minis_skills import SKILL_TOOL, SkillCatalog
from minis_telegram import Bot

HERE = Path(__file__).resolve().parent
INSTALLED = Path('/opt/minis-linux')
REPO = 'https://github.com/brucelau1987cn/minis-linux.git'
RELEASE_SIGNER = ('minis-linux-release namespaces="git" ssh-ed25519 '
                  'AAAAC3NzaC1lZDI1NTE5AAAAIMerJN7TUiQp4h7KLlJaG5DGjHqnYNJ5f2gMj/GXsjHL\n')
VERSION_RE = r'(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})'
PYTHON = '/usr/bin/python3'


def version_tuple(text):
    match = re.fullmatch(VERSION_RE, text)
    if match is None:
        raise ValueError('版本格式必须为 MAJOR.MINOR.PATCH。')
    return tuple(int(part) for part in match.groups())


def installed_version(directory=HERE):
    fd = os.open(directory / 'VERSION', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('VERSION 必须是普通文件。')
        raw = stream.read(65)
    if len(raw) > 64:
        raise ValueError('VERSION 文件过长。')
    text = raw.decode('ascii').strip()
    version_tuple(text)
    return text


def default_terminal_home():
    home = Path.home()
    private = home / '.minis/terminal'
    if private.exists() or private.is_symlink():
        return private
    return home / '.local/share/minis-linux-terminal'


def select_version(refs, current):
    current_key = version_tuple(current)
    result = None
    best = current_key
    for line in refs.splitlines():
        match = re.fullmatch(r'([a-f0-9]{40}|[a-f0-9]{64})\trefs/tags/v(' + VERSION_RE + ')', line)
        if not match:
            continue
        key = version_tuple(match[2])
        if key > best:
            best, result = key, (match[2], match[1])
    return result


def clean_environment():
    # No caller git configuration, hooks, credential helpers or alternate routes during root updates.
    return {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TEMPLATE_DIR': '', 'GIT_TERMINAL_PROMPT': '0', 'HOME': '/'}


def git_command(args):
    return subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null',
                           '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                           '-c', 'http.followRedirects=false', *args], cwd='/', env=clean_environment(),
                          stdin=subprocess.DEVNULL, text=True, capture_output=True, check=True, timeout=90).stdout


def update(*, check, current, git=git_command, run=subprocess.run):
    pending = INSTALLED / '.pending-upgrade.json'
    if HERE == INSTALLED and (pending.exists() or pending.is_symlink()):
        raise ValueError('有未完成的安装；请从可信安装包运行 install.py --recover。')
    latest = select_version(git(['ls-remote', '--tags', '--refs', REPO]), current)
    if latest is None:
        print(f'没有比 {current} 更新的稳定版本。')
        return 0
    version, oid = latest
    print(f'发现稳定版本：{current} → {version}', flush=True)
    if check:
        return 0
    if HERE != INSTALLED:
        raise ValueError('此入口不执行系统升级；用户版请从可信新版安装包重新运行 install.py --user。')
    if os.geteuid() != 0:
        return run(['/usr/bin/sudo', '/usr/local/bin/minis', 'update'], check=False).returncode
    # Explicit root-only staging, independent of user-controlled current directory or TMPDIR.
    with tempfile.TemporaryDirectory(prefix='minis-update-', dir='/var/tmp') as tmp:
        checkout = Path(tmp) / 'src'
        tag = f'v{version}'
        git(['clone', '--quiet', '--no-checkout', '--depth', '1', '--single-branch', '--branch', tag, REPO, str(checkout)])
        actual = git(['-C', str(checkout), 'rev-parse', '--verify', f'refs/tags/{tag}']).strip()
        head = git(['-C', str(checkout), 'rev-parse', '--verify', 'HEAD']).strip()
        commit = git(['-C', str(checkout), 'rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}']).strip()
        if actual != oid or head != commit:
            raise ValueError('远端标签在下载时变化；未安装。')
        signers = Path(tmp) / 'allowed-signers'
        signers.write_text(RELEASE_SIGNER, encoding='ascii')
        try:
            git(['-C', str(checkout), '-c', f'gpg.ssh.allowedSignersFile={signers}',
                 '-c', 'gpg.ssh.program=/usr/bin/ssh-keygen', '-c', 'gpg.openpgp.program=/usr/bin/false',
                 '-c', 'gpg.x509.program=/usr/bin/false', 'verify-tag', f'refs/tags/{tag}'])
        except subprocess.CalledProcessError:
            raise ValueError('发布标签的签名未通过内置公钥校验；未安装。') from None
        tag_header = git(['-C', str(checkout), 'cat-file', 'tag', actual]).split('\n\n', 1)[0].splitlines()
        if tag_header[:3] != [f'object {commit}', 'type commit', f'tag {tag}']:
            raise ValueError('签名标签不匹配请求的版本；未安装。')
        git(['-C', str(checkout), 'checkout', '--quiet', '--detach', commit])
        if installed_version(checkout) != version:
            raise ValueError('签名版本与 VERSION 不匹配；未安装。')
        installer = checkout / 'install.py'
        if not installer.is_file() or installer.is_symlink():
            raise ValueError('稳定版缺少安装器；未安装。')
        result = run([PYTHON, '-EsB', str(installer), '--upgrade-to', version],
                     cwd='/', env=clean_environment(), check=False)
        if result.returncode == 0:
            print(f'Minis Linux 已更新至 {installed_version(INSTALLED)}。')
        return result.returncode


class Terminal:
    def send(self, owner, text):
        print_reply(text)


def print_reply(text):
    print(f'Minis › {safe_display(text)}', flush=True)


def report_model(model):
    if model is not None:
        print(f'本轮模型：{safe_display(model)}', flush=True)


class TerminalStream:
    """Render public deltas, with a separate line for each streamed model round."""

    def __init__(self):
        self.fragments = []
        self.model = None

    def end_round(self):
        if self.fragments:
            print(flush=True)

    def checkpoint(self, stage):
        if stage == '等待模型':  # complete_turn: immediately before each model round
            self.end_round()
            self.fragments.clear()

    def delta(self, text):
        if text:
            print(('' if self.fragments else 'Minis › ') + safe_display(text), end='', flush=True)
            self.fragments.append(text)

    def used_model(self, model):
        self.model = model

    def remaining(self, reply, history):
        # Intermediate SSE text stays in scrollback, but is not the final
        # answer. If the last round was JSON, print the full final reply even
        # if it is identical to something from an earlier tool round.
        if not (history and self.fragments):
            return reply
        body = history[-1].get('content') if history[-1].get('role') == 'assistant' else None
        if not isinstance(body, str) or body != ''.join(self.fragments):
            return reply
        # Bot can wrap the model body with local warnings and source citations.
        # Replay only those additions, not the already displayed final body.
        for text in (body, body.strip()):
            if not text:
                continue
            start = reply.find(text)
            while start != -1:
                end = start + len(text)
                if ((start == 0 or reply.endswith('\n\n', 0, start))
                        and (end == len(reply) or reply.startswith('\n\n', end))):
                    before, after = reply[:start], reply[end:]
                    return '\n\n'.join(part.strip() for part in (before, after) if part.strip())
                start = reply.find(text, start + 1)
        return reply


def stream_turn(chat, prompt, fallback):
    if prompt.allow_commands:
        reply = chat.local_command(prompt)
        if reply is not None:
            chat.send_reply(reply)
            return
    stream, proposals = TerminalStream(), []
    try:
        reply, history = chat.generate(prompt, checkpoint=stream.checkpoint, on_delta=stream.delta,
                                       on_notice=print_reply, on_model=stream.used_model,
                                       proposals=proposals, fallback_model=fallback)
    finally:
        stream.end_round()
    extra = stream.remaining(reply, history)
    previews = []
    if history is not None:
        try:
            previews = chat.commit_turn(history, proposals)
        except (OSError, ValueError, RecursionError):
            chat.send_reply('无法保存此轮会话，请检查本机状态目录。')
            return
    if extra:
        chat.send_reply(extra)
    for preview in previews:
        chat.send_reply(preview)
    report_model(stream.model)


class EchoDemo:
    def reply(self, messages):
        return {'content': '离线演示：' + messages[-1]['content']}


def terminal_config(home):
    config = home / 'terminal-config.json'
    if config.exists() or config.is_symlink():
        meta = config.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or meta.st_uid != os.geteuid():
            raise ValueError('终端配置必须是本人持有、权限为 0600 的普通文件。')
    cached = read_history(config)
    def fields(row, size):
        return isinstance(row, list) and len(row) == size and all(isinstance(v, str) and v for v in row)

    if not cached:
        return config, [], 0
    if fields(cached, 3):
        return config, [('原有网关', *cached)], 0
    if (len(cached) == 2 and type(cached[0]) is int and isinstance(cached[1], list)
            and 0 <= cached[0] < len(cached[1]) <= 32 and all(fields(row, 4) for row in cached[1])):
        return config, [tuple(row) for row in cached[1]], cached[0]
    raise ValueError('终端模型配置无效；请检查本人状态目录下的 terminal-config.json。')


def valid_model_id(model) -> TypeGuard[str]:
    return (isinstance(model, str) and 0 < len(model) <= 512 and model.isprintable()
            and not any(ch.isspace() for ch in model))


class TerminalProvider(Provider):
    gateway_profile: str | None = None


def make_provider(base, model, key, *, validate_model=False):
    if not key or len(key) > 4096 or any(not 33 <= ord(ch) <= 126 for ch in key):
        raise ValueError('模型 API Key 无效。')
    if not model or (validate_model and not valid_model_id(model)):
        raise ValueError('模型 ID 无效。')
    try:
        if '?' in base or '#' in base or any(ch.isspace() or not ch.isprintable() for ch in base):
            raise ValueError('Invalid URL')
        return TerminalProvider(base, key, model, read_only=True, extra_tools=(SKILL_TOOL,))
    except ValueError:
        raise ValueError('模型 Base URL 无效，请填写 OpenAI 兼容地址（通常以 /v1 结尾）。') from None


FALLBACK_FILE = 'terminal-fallbacks.json'


def terminal_fallbacks(home):
    path = home / FALLBACK_FILE
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('备用模型配置必须是本人持有的私有普通文件。')
    rows = read_history(path)
    settings = {}
    for row in rows:
        if (not isinstance(row, list) or len(row) != 3
                or not all(isinstance(v, str) and v for v in row) or not valid_model_id(row[2])
                or row[0] in settings):
            raise ValueError('备用模型配置无效；请检查 terminal-fallbacks.json。')
        name, endpoint, model = row
        settings[name] = (endpoint, model)
    return settings


def gateway_fallback(provider, settings):
    saved = settings.get(getattr(provider, 'gateway_profile', None))
    if saved is not None and saved[0] == provider.endpoint and saved[1] != provider.model:
        return saved[1]
    return None


def gateway_profile(gateways, name):
    # Legacy readers permit manually edited, ambiguous names. Never attach a
    # fallback to either of two indistinguishable profiles.
    return name if sum(row[0].casefold() == name.casefold() for row in gateways) == 1 else None


def read_paste_marker(fd, *, pasting):
    """Read only the expected bracketed-paste marker after Escape."""
    deadline = monotonic() + 0.25
    for byte in (b'[201~' if pasting else b'[200~'):
        remaining = deadline - monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            return False
        if os.read(fd, 1) != bytes((byte,)):
            return False
    return True


def ask_secret(prompt):
    """Read a printable ASCII credential from a TTY without echoing its value."""
    try:
        fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
        out = fd
    except OSError:
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise ValueError('密钥输入需要交互终端（SSH 请分配 TTY）。') from None
        out = sys.stderr.fileno()
        fd = os.dup(sys.stdin.fileno())
    try:
        original = termios.tcgetattr(fd)
        hidden = termios.tcgetattr(fd)
        hidden[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.IEXTEN | termios.ISIG)
        hidden[6][termios.VMIN] = 1
        hidden[6][termios.VTIME] = 0
        secret = bytearray()
        invalid = False
        pasting = False
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, hidden)
            os.write(out, prompt.encode(sys.stderr.encoding or 'utf-8', 'backslashreplace'))
            while True:
                ch = os.read(fd, 1)
                if ch == b'\x1b':
                    if not read_paste_marker(fd, pasting=pasting):
                        raise KeyboardInterrupt
                    pasting = not pasting
                    continue
                if ch in (b'\r', b'\n'):
                    if pasting:
                        invalid = True  # A credential must be one line; do not submit mid-paste.
                        continue
                    if invalid:
                        raise ValueError('密钥格式无效；请只粘贴完整密钥，不要带空格。')
                    return secret.decode('ascii')
                if ch in (b'\x03', b'\x1a'):
                    raise KeyboardInterrupt
                if ch in (b'', b'\x04'):
                    raise EOFError('已取消密钥输入。')
                if not pasting and ch == b'\x15':
                    os.write(out, b'\b \b' * len(secret))
                    secret.clear()
                    invalid = False
                elif invalid:
                    continue
                elif not pasting and ch in (b'\x08', b'\x7f'):
                    if secret:
                        secret.pop()
                        os.write(out, b'\b \b')
                elif b'!' <= ch <= b'~' and len(secret) < 4096:
                    secret.extend(ch)
                    os.write(out, b'*')
                else:
                    invalid = True
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, original)
            os.write(out, b'\n')
    finally:
        os.close(fd)


def terminal_provider(home):
    config, gateways, active = terminal_config(home)
    if gateways:
        name, base, model, key = gateways[active]
        provider = make_provider(base, model, key)
        provider.gateway_profile = gateway_profile(gateways, name)
        return provider
    # Prefer a one-off environment configuration, which is never persisted.
    base, model, key = (os.getenv(k, '') for k in ('MINIS_BASE_URL', 'MINIS_MODEL', 'MINIS_API_KEY'))
    if base and model and key:
        return make_provider(base, model, key)
    key_from_env = bool(key)
    print('首次终端配置（与 Telegram 分开）。录制键盘输入的终端请使用预置环境变量，不要输入密钥。')
    base = base or input('模型 Base URL（含 /v1）: ').strip()
    model = model or input('模型 ID: ').strip()
    key = key or ask_secret('模型 API Key: ')
    provider = make_provider(base, model, key)
    if not key_from_env:
        save_history(config, [base, model, key])
        provider.gateway_profile = '原有网关'
    return provider


@contextmanager
def terminal_lock(home):
    os.umask(0o077)
    if any((p / '.git').exists() for p in (home.resolve(), *home.resolve().parents)):
        raise ValueError('终端状态目录不能放在 Git 工作树内。')
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    meta = home.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
        raise ValueError('终端状态目录必须是本人持有的私有目录（0700）。')
    fd = os.open(home / 'terminal.lock', os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'rb') as guard:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('终端锁必须是本人持有的私有普通文件。')
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('已有 Minis 终端会话使用此目录，请先退出该会话。') from None
        yield


def model_catalog(provider):
    endpoint = provider.endpoint.removesuffix('/chat/completions') + '/models'
    request = Request(endpoint, headers={'Authorization': f'Bearer {provider.key}'})
    with provider.opener.open(request, timeout=8) as response:
        raw = response.read(HTTP_LIMIT + 1)
    if len(raw) > HTTP_LIMIT:
        raise ValueError('模型目录过大。')
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        raise ValueError('模型目录格式无效。')
    ids = (row.get('id') for row in data['data'] if isinstance(row, dict))
    return sorted({model for model in ids if valid_model_id(model)})


def unicode_menu():
    # In Python UTF-8 mode stdout can be UTF-8 while libc/curses is still ASCII.
    return locale.nl_langinfo(locale.CODESET).lower().replace('-', '') == 'utf8'


def menu_text(zh, en):
    return zh if unicode_menu() else en


def pick_index(title, options, *, initial=0, searchable=False):
    # Curses is optional on minimal Python builds or a dumb terminal.
    if unicode_menu() and sys.stdout.isatty() and os.environ.get('TERM') not in (None, '', 'dumb'):
        try:
            import curses
        except ImportError:
            pass
        else:
            try:
                return curses.wrapper(draw_picker, curses, title, options, initial, searchable)
            except curses.error:
                pass
    # Escape labels explicitly: the installed launcher can use UTF-8 streams
    # even when the terminal locale is ASCII. Keep original values by index.
    if not unicode_menu():
        title = title.encode('ascii', 'backslashreplace').decode('ascii')
        options = [label.encode('ascii', 'backslashreplace').decode('ascii') for label in options]
    visible = list(enumerate(options))
    while True:
        print(title)
        page = visible[:100]
        if searchable and len(visible) > 100:
            page = [*visible[:99], visible[-1]]
            print(menu_text('仅显示部分结果；输入搜索词可查找其他模型。',
                            'Only some results are shown. Search to find more models.'))
        for n, (_, label) in enumerate(page, 1):
            print(f'{n:3d}. {label}')
        prompt = menu_text('输入编号' + ('或搜索词' if searchable else '') + '；回车取消: ',
                           'Enter a number' + (' or a search term' if searchable else '') + '; blank cancels: ')
        choice = input(prompt).strip()
        if not choice:
            return None
        if choice.isascii() and choice.isdecimal() and len(choice) <= 3:
            number = int(choice)
            if 1 <= number <= len(page):
                return page[number - 1][0]
        elif searchable:
            visible = [(i, label) for i, label in enumerate(options)
                       if i == len(options) - 1 or choice.casefold() in label.casefold()]
            continue
        print(menu_text('编号超出范围。', 'Number out of range.'))


def draw_picker(screen, curses, title, options, initial, searchable):
    screen.keypad(True)
    # Give Escape a short delay while still letting curses parse arrow sequences.
    curses.set_escdelay(100)
    query, pos, top = '', initial, 0
    keys = [label.casefold() for label in options]
    visible = list(range(len(options)))
    while True:
        height, width = screen.getmaxyx()
        if height < 5 or width < 20:
            raise curses.error('terminal too small for picker')
        screen.erase()
        room = max(1, height - 4)
        top = max(0, min(top, pos), pos - room + 1)

        def line(y, text, attrs=0):
            # Stay clear of the right edge even for double-width characters.
            budget = width - 1 if text.isascii() else (width - 1) // 2
            screen.addnstr(y, 0, text, budget, attrs)

        line(0, title)
        line(1, '↑↓ 移动 · Enter 确认 · Esc 取消' + (' · 输入搜索词' if searchable else ''))
        line(2, f'搜索：{query}' if searchable else '')
        for y, i in enumerate(visible[top:top + room], 3):
            selected = i == visible[pos]
            line(y, ('› ' if selected else '  ') + options[i], curses.A_REVERSE if selected else 0)
        screen.refresh()
        key = screen.get_wch()
        if key == '\x1b':
            return None
        if key in ('\n', '\r', curses.KEY_ENTER):
            return visible[pos]
        if key == curses.KEY_UP:
            pos = max(0, pos - 1)
        elif key == curses.KEY_DOWN:
            pos = min(len(visible) - 1, pos + 1)
        elif searchable:
            if key in (curses.KEY_BACKSPACE, '\b', '\x7f'):
                query = query[:-1]
            elif isinstance(key, str) and key.isprintable() and len(query) < 512:
                query += key
            else:
                continue
            visible = [i for i, label in enumerate(keys) if query.casefold() in label or i == len(keys) - 1]
            pos = top = 0


def select_model(base, key, current):
    provider = make_provider(base, 'catalog', key)
    try:
        models = model_catalog(provider)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        print(menu_text(f'网关模型目录返回 HTTP {status}。', f'Model catalog returned HTTP {status}.'))
        if status in (401, 403):
            print(menu_text('请检查该网关的普通调用 API Key 和权限。', 'Check the API key and access for this gateway.'))
        models = []
    except (OSError, ValueError, http.client.HTTPException, RecursionError):
        print(menu_text('无法读取网关模型目录，可以手动输入模型 ID。', 'Model catalog unavailable; enter the model ID manually.'))
        models = []
    if models:
        title = menu_text('选择模型', 'Choose model')
        if current:
            title += menu_text(f'（当前：{safe_display(current)}）', f' (current: {safe_display(current)})')
        index = pick_index(title, [*models, menu_text('手动输入模型 ID', 'Enter model ID manually')],
                           initial=models.index(current) if current in models else 0, searchable=True)
        if index is None:
            return None
        if index < len(models):
            return models[index]
    return input(menu_text('模型 ID（回车取消）: ', 'Model ID (blank cancels): ')).strip() or None


def gateway_labels(gateways):
    labels = []
    for name, base, _, _ in gateways:
        try:
            url = urlsplit(base)
            host = url.hostname or '无效地址'
            host = f'[{host}]' if ':' in host else host
            if url.port is not None:
                host += f':{url.port}'
        except ValueError:
            host = '无效地址'
        labels.append(safe_display(f'{name} · {host}'))
    return labels


def model_command(home, *, model_id=None, show=False):
    with terminal_lock(home):
        config, gateways, active = terminal_config(home)
        if show:
            print('默认模型：' + safe_display(gateways[active][2]) if gateways else '尚未设置默认模型。')
            return 0
        index = active
        if model_id is None:
            if not sys.stdin.isatty():
                raise ValueError('请提供模型 ID：minis model MODEL_ID；只查看可用 --show。')
            if gateways:
                add_label = menu_text('添加新网关', 'Add gateway')
                index = pick_index(menu_text('选择默认网关', 'Choose default gateway'),
                                   [*gateway_labels(gateways), add_label], initial=active)
                if index is None:
                    return 0
        if index < len(gateways):
            name, base, current, key = gateways[index]
        else:
            if not sys.stdin.isatty():
                raise ValueError('请先在交互式终端运行 minis model 完成模型接入。')
            if len(gateways) >= 32:
                raise ValueError('最多保存 32 个网关。')
            name = input(menu_text('新网关名称（回车取消）: ', 'Gateway name (blank cancels): ')).strip()
            if not name:
                return 0
            if len(name) > 80 or not name.isprintable() or name.casefold() in {
                    row[0].casefold() for row in gateways}:
                raise ValueError('网关名称无效或重复。')
            base = input(menu_text('模型 Base URL（含 /v1，回车取消）: ',
                                   'Model Base URL (include /v1; blank cancels): ')).strip()
            if not base:
                return 0
            print(menu_text('录制键盘输入的终端不要输入密钥。', 'Do not enter keys in a recorded terminal.'))
            key = ask_secret(menu_text('模型 API Key（回车取消）: ', 'Model API key (blank cancels): '))
            if not key:
                return 0
            current = None
        model_id = model_id if model_id is not None else select_model(base, key, current)
        if model_id is None:
            return 0
        make_provider(base, model_id, key, validate_model=True)
        existing_context = any((home / 'telegram' / f'bot-terminal-owner-1{suffix}.json').exists()
                               for suffix in ('', '.memory'))
        if (gateways and index != active) or (not gateways and existing_context):
            destination, = gateway_labels([(name, base, model_id, key)])
            print(menu_text(f'新默认网关：{destination}。下次对话会将已有聊天记录和可能包含的长期记忆发送给该网关。',
                            f'New default gateway: {destination}. Your next request will send existing chat history '
                            'and may send saved memory to this gateway.'))
            if input(menu_text('确认切换默认网关？[y/N]: ', 'Change the default gateway? [y/N]: ')).strip().lower() != 'y':
                return 0
        gateways[index:index + 1] = [(name, base, model_id, key)]
        save_history(config, [index, gateways])
        print(f'已保存终端默认模型：{model_id}（未进行推理验证）。')
        return 0


def fallback_command(home, *, gateway=None, model_id=None, clear=False):
    with terminal_lock(home):
        _, gateways, active = terminal_config(home)
        if not gateways:
            raise ValueError('请先运行 minis model 保存网关；一次性环境配置不支持备用模型。')
        if gateway is not None:
            matches = [i for i, row in enumerate(gateways) if row[0].casefold() == gateway.casefold()]
            if len(matches) != 1:
                raise ValueError('--gateway 必须唯一匹配一个已保存的网关名称。')
            active = matches[0]
        name, base, current, key = gateways[active]
        if gateway_profile(gateways, name) is None:
            raise ValueError('已保存的网关名称重复；无法绑定备用模型。')
        provider = make_provider(base, current, key)
        settings = terminal_fallbacks(home)
        if model_id is not None:
            if not valid_model_id(model_id) or model_id == current:
                raise ValueError('备用模型 ID 无效或与默认模型相同。')
            settings[name] = (provider.endpoint, model_id)
        elif clear:
            settings.pop(name, None)
        if model_id is not None or clear:
            save_history(home / FALLBACK_FILE, [[label, *data] for label, data in settings.items()])
        endpoint, saved = settings.get(name, (None, None))
        status = saved if endpoint == provider.endpoint else None
        suffix = '（与默认模型相同，当前不重试）' if status == current else ''
        label, = gateway_labels([gateways[active]])
        print(f'{label} · 备用模型：{safe_display(status) if status else "未设置"}{suffix}')
        return 0


def session_model(home, provider, model_id):
    base = provider.endpoint.removesuffix('/chat/completions')
    key, current = provider.key, provider.model
    profile = getattr(provider, 'gateway_profile', None)
    if not model_id:
        _, gateways, active = terminal_config(home)
        if gateways:
            active = next((i for i, (name, url, _, secret) in enumerate(gateways)
                           if (name, url.rstrip('/'), secret) == (profile, base, key)), active)
            print(menu_text('切换网关后，后续请求会把会话上下文发送到所选网关。',
                            'Switching gateways sends conversation context to the selected gateway on later requests.'))
            index = pick_index(menu_text('当前会话：选择已配置网关', 'Current session: choose a saved gateway'),
                               gateway_labels(gateways), initial=active)
            if index is None:
                return provider
            name, url, saved_model, secret = gateways[index]
            if (name, url.rstrip('/'), secret) != (profile, base, key):
                base, current, key = url, saved_model, secret
            profile = gateway_profile(gateways, name)
        model_id = select_model(base, key, current)
        if model_id is None:
            return provider
    selected = make_provider(base, model_id, key, validate_model=True)
    selected.gateway_profile = profile
    print(f'已切换当前会话模型：{model_id}（未改默认设置）。')
    return selected


class TerminalView:
    LOGO = (
        '█▄ ▄█  ███  █▄  █  ███  ████',
        '█ █ █   █   █ █ █   █   █   ',
        '█   █   █   █  ██   █   ▀▀▀█',
        '█   █  ███  █   █  ███  ████',
    )

    def __init__(self, *, plain=False):
        self.plain = plain or not unicode_menu() or os.environ.get('TERM') == 'dumb'
        self.color = not self.plain and sys.stdout.isatty() and 'NO_COLOR' not in os.environ
        self.elapsed: float | None = None

    @staticmethod
    def width():
        return max(1, min(shutil.get_terminal_size((80, 24)).columns - 1, 100))

    @staticmethod
    def fit(text, width):
        used, chars = 0, []
        for ch in safe_display(text).replace('\n', ' ').replace('\t', ' '):
            cells = 0 if category(ch).startswith('M') else 2 if east_asian_width(ch) in ('F', 'W') else 1
            if used + cells > width:
                break
            chars.append(ch)
            used += cells
        return ''.join(chars), used

    def paint(self, text, color):
        return f'\033[{color}m{text}\033[0m' if self.color else text

    @staticmethod
    def model(provider):
        return getattr(provider, 'model', '离线演示')

    def welcome(self, chat, version, *, demo):
        title = f'Minis Linux {version} · ' + ('离线演示' if demo else '终端对话')
        width = self.width()
        if self.plain or width < 40:
            print(title)
            print('直接输入消息，/help 查看帮助，/exit 退出。')
            return
        for logo_line, color in zip(self.LOGO, ('93;1', '33;1', '33', '33;2')):
            print(self.paint(logo_line, color))
        title, cells = self.fit(' ' + title + ' ', width - 3)
        print(self.paint('╭' + '─' + title + '─' * (width - cells - 3) + '╮', '33'))

        def row(text):
            content, cells = self.fit(text, width - 4)
            print(self.paint('│', '33') + ' ' + content + ' ' * (width - cells - 4) + ' ' + self.paint('│', '33'))

        provider = chat.provider
        endpoint = getattr(provider, 'endpoint', '')
        gateway = urlsplit(endpoint).hostname if endpoint else None
        tools = [tool['function']['name'] for tool in getattr(provider, 'tools', ())]
        try:
            skills = list(SkillCatalog(chat.skills_dir).entries)
        except (OSError, ValueError):
            skills = None
        row('模型：' + self.model(provider))
        if gateway:
            row('网关：' + gateway)
        row(f'可用工具（{len(tools)}）：' + (', '.join(tools) or '无'))
        if skills is None:
            row('可用技能：暂不可读')
        else:
            row(f'可用技能（{len(skills)}）：' + (', '.join(skills) or '无'))
        print(self.paint('╰' + '─' * (width - 2) + '╯', '33'))
        print('直接提问，或用 /help 查看命令。')

    def prompt(self, provider, *, demo):
        width = self.width()
        if self.plain or width < 40:
            return '\n你 › '
        status = '当前模型：' + self.model(provider)
        if self.elapsed is not None:
            status += f'  ·  上轮耗时：{self.elapsed:.1f}s'
        status += '  ·  ' + ('离线 · /help' if demo else '/model · /help')
        print('\n' + self.paint('─' * width, '2'))
        print(self.paint(self.fit(status, width)[0], '33'))
        print(self.paint('─' * width, '2'))
        return '你 › '


def terminal_chat(home, *, demo=False, plain=False):
    if not sys.stdin.isatty():
        raise ValueError('终端对话需要交互终端；通过 SSH 时请分配 TTY。')
    try:
        import readline  # noqa: F401 - enable line editing and in-process input history
    except ImportError:
        pass  # Minimal Python builds still support a plain input prompt.
    with terminal_lock(home):
        provider = EchoDemo() if demo else terminal_provider(home)
        try:
            fallbacks = {} if demo else terminal_fallbacks(home)
        except (OSError, ValueError, RecursionError):
            print('备用模型配置不可读；本次会话不使用备用模型。', file=sys.stderr)
            fallbacks = {}
        skill_dir = home / 'skills'
        if not skill_dir.exists() and not skill_dir.is_symlink():
            skill_dir = HERE / 'skills'
        chat = Bot(home, 1, 'terminal', Terminal(), provider,
                   skills_dir=skill_dir, channel='终端', features=not demo)
        view = TerminalView(plain=plain)
        view.welcome(chat, installed_version(), demo=demo)
        commands = minis_input.DEFAULT_COMMANDS
        if not demo:
            commands += ('/sessions', '/session', '/title', '/history', '/export', '/actions',
                         '/preview', '/approve', '/cancel', '/audit', '/trash', '/restore', '/skill')
        while True:
            try:
                prompt = minis_input.read_prompt(view.prompt(chat.provider, demo=demo),
                                                 plain=view.plain, commands=commands)
            except EOFError:
                print()
                return 0
            if not prompt:
                continue
            command, *args = prompt.split(maxsplit=1) if prompt.allow_commands else ('',)
            if command == '/exit' and prompt == '/exit':
                return 0
            if command == '/search':
                print('离线演示不联网搜索。' if demo else safe_display(search_web(args[0] if args else '')))
                continue
            if command == '/model':
                if demo:
                    print('离线演示不连接模型网关。')
                else:
                    try:
                        chat.provider = session_model(home, chat.provider, args[0] if args else None)
                    except (OSError, ValueError, EOFError, RecursionError) as exc:
                        print('未切换模型：' + safe_display(str(exc)), file=sys.stderr)
                continue
            if command == '/help' and not demo:
                print('/model [模型 ID]：切换当前会话模型；/exit：退出终端。')
                print('/search 关键词：只将关键词发送给 DuckDuckGo 搜索，不调用模型。')
                print('同网关备用模型：退出后运行 minis model --help。')
                if not view.plain:
                    print('支持的终端可用 Ctrl+J 换行、Tab 补全命令，粘贴预览后按 Enter 发送。')
                    print('模型可能在调用工具前输出文字；最终回答另起一行，已显示的最终回答不重复打印。')
            started = monotonic()
            fallback = gateway_fallback(chat.provider, fallbacks)
            if view.plain or demo:
                chat.handle(prompt, allow_commands=prompt.allow_commands, fallback_model=fallback,
                            on_notice=print_reply if fallback else None,
                            on_model=report_model if fallback else None)
            else:
                stream_turn(chat, prompt, fallback)
            if not command.startswith('/'):
                view.elapsed = monotonic() - started


def main():
    if not unicode_menu():
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, TextIOWrapper):
                stream.reconfigure(errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['update', 'model'])
    parser.add_argument('model_id', nargs='?', metavar='MODEL_ID', help='with model: save a specific ID')
    model_modes = parser.add_mutually_exclusive_group()
    model_modes.add_argument('--show', action='store_true', help='with model: show the saved default')
    model_modes.add_argument('--fallback', metavar='MODEL_ID', help='with model: set a same-gateway fallback')
    model_modes.add_argument('--clear-fallback', action='store_true', help='with model: remove the fallback')
    model_modes.add_argument('--show-fallback', action='store_true', help='with model: show the saved fallback')
    parser.add_argument('--gateway', metavar='NAME', help='with fallback options: saved gateway (default: active)')
    parser.add_argument('--version', action='store_true')
    parser.add_argument('--check', action='store_true', help='with update: check only')
    parser.add_argument('--demo', action='store_true', help='offline terminal demo')
    parser.add_argument('--plain', action='store_true', help='use simple line input and whole replies')
    parser.add_argument('--home', type=Path, help='private terminal state directory')
    args = parser.parse_args()
    fallback_mode = args.fallback is not None or args.clear_fallback or args.show_fallback
    if args.command != 'model' and (args.show or args.model_id is not None or fallback_mode or args.gateway is not None):
        parser.error('模型配置选项仅适用于 model')
    if args.gateway is not None and not fallback_mode:
        parser.error('--gateway 仅适用于备用模型选项')
    if args.command and args.plain:
        parser.error('--plain 仅用于终端对话')
    if args.command == 'model' and (args.demo or args.check or ((args.show or fallback_mode) and args.model_id is not None)):
        parser.error('model 不支持 --demo 或 --check，查看或设置备用模型时不能指定默认 MODEL_ID')
    try:
        version = installed_version()
        if args.version:
            print(f'Minis Linux {version}')
            return 0
        if args.command == 'update':
            if args.demo or args.home is not None:
                parser.error('update 不支持 --demo 或 --home')
            return update(check=args.check, current=version)
        if args.check:
            parser.error('--check 仅用于 update')
        if args.demo:
            with tempfile.TemporaryDirectory(prefix='minis-demo-', dir=args.home) as tmp:
                return terminal_chat(Path(tmp), demo=True, plain=args.plain)
        home = args.home or default_terminal_home()
        if args.command == 'model':
            if fallback_mode:
                return fallback_command(home.expanduser(), gateway=args.gateway,
                                        model_id=args.fallback, clear=args.clear_fallback)
            return model_command(home.expanduser(), model_id=args.model_id, show=args.show)
        return terminal_chat(home.expanduser(), plain=args.plain)
    except KeyboardInterrupt:
        print('\n已退出 Minis。')
        return 130
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        if args.command == 'update':
            print('更新未完成：请检查 Git、sudo 和到 GitHub 的 HTTPS 连接。', file=sys.stderr)
        else:
            print('无法启动 Minis：' + safe_display(str(exc)), file=sys.stderr)
        return 1
    except (OSError, ValueError, EOFError, RecursionError, termios.error) as exc:
        print('Minis：' + safe_display(str(exc)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
