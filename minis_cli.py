#!/usr/bin/env python3
"""Minis Linux terminal chat and stable-version updater."""
import argparse
import fcntl
import getpass
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import warnings

from minis import Provider, read_history, safe_display, save_history
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


def terminal_provider(home):
    config = home / 'terminal-config.json'
    if config.exists() or config.is_symlink():
        meta = config.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or meta.st_uid != os.geteuid():
            raise ValueError('终端配置必须是本人持有、权限为 0600 的普通文件。')
    cached = read_history(config)
    if cached and (len(cached) != 3 or not all(isinstance(item, str) and item for item in cached)):
        raise ValueError('终端模型配置无效；请修改本人状态目录下的 terminal-config.json。')
    if cached:
        base, model, key = cached
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
            if not model or not key or any(not 33 <= ord(ch) <= 126 for ch in key):
                raise ValueError('模型 ID 或 API Key 无效。')
            provider = Provider(base, key, model, read_only=True, extra_tools=(SKILL_TOOL,))
            if not key_from_env:
                save_history(config, [base, model, key])
            return provider
    if any(not 33 <= ord(ch) <= 126 for ch in key):
        raise ValueError('模型 API Key 无效。')
    return Provider(base, key, model, read_only=True, extra_tools=(SKILL_TOOL,))


def terminal_chat(home, *, demo=False):
    if not sys.stdin.isatty():
        raise ValueError('终端对话需要交互终端；通过 SSH 时请分配 TTY。')
    try:
        import readline  # noqa: F401 - enable line editing and in-process input history
    except ImportError:
        pass  # Minimal Python builds still support a plain input prompt.
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
            raise ValueError('已有 Minis 终端会话使用此目录。') from None
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
            if prompt:
                chat.handle(prompt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['update'])
    parser.add_argument('--version', action='store_true')
    parser.add_argument('--check', action='store_true', help='with update: check only')
    parser.add_argument('--demo', action='store_true', help='offline terminal demo')
    parser.add_argument('--home', type=Path, help='private terminal state directory')
    args = parser.parse_args()
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
