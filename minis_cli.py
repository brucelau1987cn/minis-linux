#!/usr/bin/env python3
"""Minis Linux terminal chat and stable-version updater."""
import argparse
from contextlib import contextmanager
import fcntl
import getpass
import http.client
from io import TextIOWrapper
import json
import locale
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import warnings
from urllib.parse import urlsplit
from urllib.request import Request

from minis import HTTP_LIMIT, Provider, read_history, safe_display, save_history
from minis_skills import SKILL_TOOL
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
        raise ValueError('请先安装，再运行 minis update；源码目录不做原地更新。')
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
        print(f'Minis › {safe_display(text)}', flush=True)


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


def make_provider(base, model, key, *, validate_model=False):
    if not key or len(key) > 4096 or any(not 33 <= ord(ch) <= 126 for ch in key):
        raise ValueError('模型 API Key 无效。')
    if not model or (validate_model and (len(model) > 512 or not model.isprintable()
                                         or any(ch.isspace() for ch in model))):
        raise ValueError('模型 ID 无效。')
    try:
        if '?' in base or '#' in base or any(ch.isspace() or not ch.isprintable() for ch in base):
            raise ValueError('Invalid URL')
        return Provider(base, key, model, read_only=True, extra_tools=(SKILL_TOOL,))
    except ValueError:
        raise ValueError('模型 Base URL 无效，请填写 OpenAI 兼容地址（通常以 /v1 结尾）。') from None


def terminal_provider(home):
    config, gateways, active = terminal_config(home)
    if gateways:
        _, base, model, key = gateways[active]
    else:
        # Prefer a one-off environment configuration, which is never persisted.
        base, model, key = (os.getenv(k, '') for k in ('MINIS_BASE_URL', 'MINIS_MODEL', 'MINIS_API_KEY'))
        key_from_env = bool(key)
        if not (base and model and key):
            print('首次终端配置（与 Telegram 分开）。录制键盘输入的终端请使用预置环境变量，不要输入密钥。')
            base = base or input('模型 Base URL（含 /v1）: ').strip()
            model = model or input('模型 ID: ').strip()
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                key = key or getpass.getpass('模型 API Key: ')
            provider = make_provider(base, model, key)
            if not key_from_env:
                save_history(config, [base, model, key])
            return provider
    return make_provider(base, model, key)


@contextmanager
def terminal_lock(home):
    os.umask(0o077)
    if any((p / '.git').exists() for p in (home.resolve(), *home.resolve().parents)):
        raise ValueError('终端状态目录不能放在 Git 工作树内。')
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    meta = home.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
        raise ValueError('终端状态目录必须是本人持有的私有目录（0700）。')
    with (home / 'terminal.lock').open('ab') as guard:
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
    return sorted({model for model in ids if isinstance(model, str) and 0 < len(model) <= 512
                   and model.isprintable() and not any(ch.isspace() for ch in model)})


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
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                key = getpass.getpass(menu_text('模型 API Key（回车取消）: ', 'Model API key (blank cancels): '))
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


def session_model(home, provider, model_id):
    base = provider.endpoint.removesuffix('/chat/completions')
    key, current = provider.key, provider.model
    if not model_id:
        _, gateways, active = terminal_config(home)
        if gateways:
            active = next((i for i, (_, url, _, secret) in enumerate(gateways)
                           if (url.rstrip('/'), secret) == (base, key)), active)
            print(menu_text('切换网关后，后续请求会把会话上下文发送到所选网关。',
                            'Switching gateways sends conversation context to the selected gateway on later requests.'))
            index = pick_index(menu_text('当前会话：选择已配置网关', 'Current session: choose a saved gateway'),
                               gateway_labels(gateways), initial=active)
            if index is None:
                return provider
            _, url, saved_model, secret = gateways[index]
            if (url.rstrip('/'), secret) != (base, key):
                base, current, key = url, saved_model, secret
        model_id = select_model(base, key, current)
        if model_id is None:
            return provider
    selected = make_provider(base, model_id, key, validate_model=True)
    print(f'已切换当前会话模型：{model_id}（未改默认设置）。')
    return selected


def terminal_chat(home, *, demo=False):
    if not sys.stdin.isatty():
        raise ValueError('终端对话需要交互终端；通过 SSH 时请分配 TTY。')
    try:
        import readline  # noqa: F401 - enable line editing and in-process input history
    except ImportError:
        pass  # Minimal Python builds still support a plain input prompt.
    with terminal_lock(home):
        provider = EchoDemo() if demo else terminal_provider(home)
        skill_dir = home / 'skills'
        if not skill_dir.exists() and not skill_dir.is_symlink():
            skill_dir = HERE / 'skills'
        chat = Bot(home, 1, 'terminal', Terminal(), provider, skills_dir=skill_dir, channel='终端')
        print(f'Minis Linux {installed_version()} · 终端对话' + ('（离线演示）' if demo else ''))
        print('直接输入消息，/help 查看帮助，/exit 退出。')
        while True:
            try:
                prompt = input('\n你 › ').strip()
            except EOFError:
                print()
                return 0
            if prompt == '/exit':
                return 0
            if not prompt:
                continue
            command, *args = prompt.split(maxsplit=1)
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
            chat.handle(prompt)


def main():
    if not unicode_menu():
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, TextIOWrapper):
                stream.reconfigure(errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['update', 'model'])
    parser.add_argument('model_id', nargs='?', metavar='MODEL_ID', help='with model: save a specific ID')
    parser.add_argument('--show', action='store_true', help='with model: show the saved default')
    parser.add_argument('--version', action='store_true')
    parser.add_argument('--check', action='store_true', help='with update: check only')
    parser.add_argument('--demo', action='store_true', help='offline terminal demo')
    parser.add_argument('--home', type=Path, help='private terminal state directory')
    args = parser.parse_args()
    if args.command != 'model' and (args.show or args.model_id is not None):
        parser.error('--show 和 MODEL_ID 仅适用于 model')
    if args.command == 'model' and (args.demo or args.check or (args.show and args.model_id is not None)):
        parser.error('model 不支持 --demo 或 --check，--show 不能和 MODEL_ID 一起使用')
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
                return terminal_chat(Path(tmp), demo=True)
        home = args.home or Path.home() / '.local/share/minis-linux-terminal'
        if args.command == 'model':
            return model_command(home.expanduser(), model_id=args.model_id, show=args.show)
        return terminal_chat(home.expanduser(), demo=args.demo)
    except KeyboardInterrupt:
        print('\n已退出 Minis。')
        return 130
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        if args.command == 'update':
            print('更新未完成：请检查 Git、sudo 和到 GitHub 的 HTTPS 连接。', file=sys.stderr)
        else:
            print('无法启动 Minis：' + safe_display(str(exc)), file=sys.stderr)
        return 1
    except (OSError, ValueError, EOFError, RecursionError, getpass.GetPassWarning) as exc:
        print('Minis：' + safe_display(str(exc)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
