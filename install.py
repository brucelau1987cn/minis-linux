#!/usr/bin/env python3
"""Install or upgrade the Minis Linux Telegram service from a trusted checkout."""
import argparse
import base64
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from shlex import quote as shell_quote
import stat
import subprocess
import sys
import tempfile
import termios

from minis import Provider
from minis_cli import ask_secret, installed_version, select_model, terminal_config, terminal_lock, version_tuple
from minis_telegram import Telegram

UNIT = 'minis-linux-telegram.service'
APP_FILES = (
    'minis.py', 'minis_actions.py', 'minis_cli.py', 'minis_input.py', 'minis_job_ui.py', 'minis_media.py',
    'minis_schedule.py', 'minis_search.py', 'minis_sessions.py', 'minis_skills.py',
    'minis_telegram.py', 'minis_transport.py', 'minis_web.py', 'VERSION',
)
LAUNCHER = b'#!/bin/sh\n# Minis Linux managed launcher\nexec /usr/bin/python3 -EsB /opt/minis-linux/minis_cli.py "$@"\n'
USER_LAUNCHER = b'#!/bin/sh\n# Minis Linux user launcher\n'
SAMPLE = 'skills/weekly-report/SKILL.md'
CONFIG_FILES = ('telegram.conf', 'telegram.token', 'model.key')
PENDING = 'opt/minis-linux/.pending-upgrade.json'
INSTALL_DIRS = ('etc', 'etc/systemd', 'etc/systemd/system', 'opt', 'opt/minis-linux',
                'opt/minis-linux/skills', 'opt/minis-linux/skills/weekly-report',
                'usr', 'usr/local', 'usr/local/bin')
MANAGED = {*(f'opt/minis-linux/{name}' for name in (*APP_FILES, SAMPLE)),
           f'etc/systemd/system/{UNIT}', 'usr/local/bin/minis'}


def preflight(source, *, run):
    for name in (*APP_FILES, SAMPLE, f'systemd/{UNIT}'):
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'安装包缺少普通文件：{name}')
    installed_version(source)
    run(['systemd-analyze', 'verify', str(source / 'systemd' / UNIT)])


def ask_config(credentials_dir=None):
    if not sys.stdin.isatty():
        raise ValueError('首次安装需要交互终端。')
    print('首次安装：请准备专用 Telegram Bot；不要复用已有 Bot 的 Token。')
    owner = input('Telegram 数字用户 ID: ').strip()
    if not re.fullmatch(r'[1-9][0-9]{0,15}', owner):
        raise ValueError('Telegram 用户 ID 应为数字。')
    base = input('模型 Base URL（含 /v1）: ').strip()
    if not base:
        raise ValueError('已取消安装，未配置模型网关。')
    token = None
    if credentials_dir is not None:
        checked_directory(credentials_dir, private=True, accept_invoker=True)
        token, key = (read_input_credential(credentials_dir / name) for name in CONFIG_FILES[1:])
        Telegram(token)
    else:
        print('如 sudo/堡垒机录制终端输入，请取消并使用 --credentials-dir，避免记录密钥。')
        key = ask_secret('模型 API Key: ')
    model = select_model(base, key, None)
    if model is None:
        raise ValueError('已取消选模，未安装。')
    if token is None:
        token = ask_secret('Telegram Bot Token: ')
    return owner, base, model, token, key


def config_bytes(values):
    owner, base, model, token, key = values
    if not re.fullmatch(r'[1-9][0-9]{0,15}', owner):
        raise ValueError('Telegram 用户 ID 应为数字。')
    if not base or not model or any(not ch.isprintable() for ch in base + model):
        raise ValueError('模型 Base URL 或模型 ID 无效。')
    if not key or len(key) > 4096 or any(not 33 <= ord(ch) <= 126 for ch in key):
        raise ValueError('模型 API Key 缺失或格式无效。')
    Telegram(token)
    try:
        Provider(base, key, model, read_only=True)
    except ValueError:
        raise ValueError('模型 Base URL 无效；请使用 HTTPS，数字回环地址可用 HTTP。') from None

    def quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

    conf = f'MINIS_TELEGRAM_USER_ID={owner}\nMINIS_BASE_URL={quote(base)}\nMINIS_MODEL={quote(model)}\n'
    return [conf.encode(), (token + '\n').encode(), (key + '\n').encode()]


def allowed_owners(accept_invoker=False):
    owners = {os.geteuid()}
    invoker = os.environ.get('SUDO_UID', '')
    if accept_invoker and os.geteuid() == 0 and invoker.isdecimal():
        owners.add(int(invoker))
    return owners


def read_input_credential(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_uid not in allowed_owners(accept_invoker=True)):
            raise ValueError(f'凭据文件必须是本人或 root 持有的私有普通文件：{path.name}')
        data = stream.read(4098)
    if len(data) >= 4098:
        raise ValueError(f'凭据文件过大：{path.name}')
    try:
        return data.decode('ascii').removesuffix('\n').removesuffix('\r')
    except UnicodeError:
        raise ValueError(f'凭据文件不是 ASCII：{path.name}') from None


def checked_directory(path, *, private=False, accept_invoker=False):
    info = path.lstat()
    forbidden = 0o077 if private else 0o022
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in allowed_owners(accept_invoker) or info.st_mode & forbidden:
        raise ValueError(f'安装目录属主或权限不安全：{path}')


def mkdir_checked(path, mode=0o755):
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        pass
    else:
        path.chmod(mode)
    checked_directory(path, private=mode == 0o700)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, content, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.minis-install-', delete=False) as f:
        temp = Path(f.name)
        try:
            os.fchmod(f.fileno(), mode)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
            f.close()
            temp.replace(path)
            sync_directory(path.parent)
        finally:
            temp.unlink(missing_ok=True)


def snapshot_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError(f'现有程序文件属主或权限不安全：{path.name}')
    if info.st_size > 2 * 1024 * 1024:
        raise ValueError(f'现有程序文件超过备份上限：{path.name}')
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def clear_pending(journal):
    if journal is not None:
        try:
            journal.unlink(missing_ok=True)
            sync_directory(journal.parent)
        except OSError:
            print('警告：未能持久化清理安装记录；可稍后运行 install.py --recover。', file=sys.stderr)


def finish_activation(journal, version):
    # The activation record must be durable before VERSION can be published.
    path = journal.with_name('VERSION')
    target = (version.encode('ascii'), 0o644)
    if snapshot_file(path) != target:
        atomic_write(path, *target)
    clear_pending(journal)


def store_pending(root, old, active, enabled):
    files = {}
    for path, previous in old.items():
        name = path.relative_to(root).as_posix()
        if name not in MANAGED:
            raise ValueError('安装文件不在允许清单内。')
        files[name] = None if previous is None else [base64.b64encode(previous[0]).decode('ascii'), previous[1]]
    journal = root / PENDING
    atomic_write(journal, json.dumps({'files': files, 'active': bool(active), 'enabled': bool(enabled)}).encode(), 0o600)
    return journal


def recover_pending(root, *, run):
    journal = root / PENDING
    if not (journal.exists() or journal.is_symlink()):
        return None
    for relative in INSTALL_DIRS:
        checked_directory(root / relative)
    meta = journal.lstat()
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid()
            or meta.st_mode & 0o077 or meta.st_size > 32 * 1024 * 1024):
        raise ValueError('待恢复的安装快照权限或大小无效。')
    state = json.loads(journal.read_bytes())
    if isinstance(state, dict) and set(state) == {'activated_version'}:
        version = state['activated_version']
        if not isinstance(version, str) or not version.isascii() or len(version) > 64:
            raise ValueError('待恢复的安装版本格式无效。')
        version_tuple(version.strip())
        # A process can die after replacing the journal or VERSION but
        # before the directory fsync. Persist any visible renames first.
        sync_directory(journal.parent)
        finish_activation(journal, version)
        return 'activated'
    # Legacy snapshots have no activation record. The installed version
    # string alone does not prove that activation succeeded.
    if (not isinstance(state, dict) or type(state.get('active')) is not bool
            or type(state.get('enabled')) is not bool or not isinstance(state.get('files'), dict)
            or not state['files'] or set(state['files']) - MANAGED):
        raise ValueError('待恢复的安装快照格式无效。')
    restored = {}
    for name, item in state['files'].items():
        if item is not None:
            if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
                    or type(item[1]) is not int or not 0 <= item[1] <= 0o777 or item[1] & 0o022):
                raise ValueError('待恢复的安装快照文件记录无效。')
            item = (base64.b64decode(item[0], validate=True), item[1])
        restored[root / name] = item

    if not run(['systemctl', 'stop', UNIT], probe=True) and run(['systemctl', 'is-active', '--quiet', UNIT], probe=True):
        raise ValueError('无法停止待恢复的 Minis 服务。')
    if not state['enabled'] and run(['systemctl', 'is-enabled', '--quiet', UNIT], probe=True):
        run(['systemctl', 'disable', UNIT])
    for path, item in reversed(list(restored.items())):
        if item is None:
            path.unlink(missing_ok=True)
            sync_directory(path.parent)
        else:
            atomic_write(path, *item)
    run(['systemctl', 'daemon-reload'])
    if state['active']:
        run(['systemctl', 'reset-failed', UNIT], probe=True)
        run(['systemctl', 'start', UNIT])
        run(['systemctl', 'is-active', '--quiet', UNIT])
    clear_pending(journal)
    return 'rolled-back'


def activate(payloads, *, run, root=None):
    version_path = root / 'opt/minis-linux/VERSION' if root is not None else None
    version = payloads[version_path][0].decode('ascii') if version_path is not None else None
    if version is not None:
        version_tuple(version.strip())
    old = {path: snapshot_file(path) for path in payloads}
    was_active = run(['systemctl', 'is-active', '--quiet', UNIT], probe=True)
    was_enabled = run(['systemctl', 'is-enabled', '--quiet', UNIT], probe=True)
    journal = store_pending(root, old, was_active, was_enabled) if root is not None else None
    changed = []
    activation_started = False
    enable_attempted = False
    try:
        for path, (content, mode) in payloads.items():
            if path == version_path:
                continue
            changed.append(path)
            atomic_write(path, content, mode)
        run(['systemctl', 'daemon-reload'])
        activation_started = True
        run(['systemctl', 'restart', UNIT])
        run(['systemctl', 'is-active', '--quiet', UNIT])
        enable_attempted = True
        run(['systemctl', 'enable', UNIT])
    except (OSError, subprocess.CalledProcessError, KeyboardInterrupt):
        failed = False

        def restore(callback, *args, **kwargs):
            nonlocal failed
            try:
                callback(*args, **kwargs)
            except (OSError, subprocess.CalledProcessError):
                failed = True

        if activation_started:
            restore(run, ['systemctl', 'stop', UNIT])
        if enable_attempted and not was_enabled:
            restore(run, ['systemctl', 'disable', UNIT])
        for path in reversed(changed):
            previous = old[path]
            if previous is None:
                restore(path.unlink, True)
                restore(sync_directory, path.parent)
            else:
                restore(atomic_write, path, *previous)
        if changed:
            restore(run, ['systemctl', 'daemon-reload'])
        if activation_started and was_active and not failed:
            # A loaded unit may still be rate-limited; an unloaded unit has no
            # counter to reset. Let start and is-active decide recovery success.
            restore(run, ['systemctl', 'reset-failed', UNIT], probe=True)
        if activation_started and was_active and not failed:
            restore(run, ['systemctl', 'start', UNIT])
        if activation_started and was_active and not failed:
            restore(run, ['systemctl', 'is-active', '--quiet', UNIT])
        if failed:
            raise ValueError('安装失败且自动回退未完成；请从可信安装包运行 install.py --recover。') from None
        clear_pending(journal)
        raise
    if journal is not None and version is not None:
        # Atomically replace the rollback snapshot after readiness and enable
        # succeed, but BEFORE publishing VERSION. A failed fsync may leave
        # either record on disk. Let recovery follow the record that survives.
        try:
            atomic_write(journal, json.dumps({'activated_version': version}).encode(), 0o600)
            finish_activation(journal, version)
        except OSError as exc:
            raise OSError('未能持久化安装结果；请从可信安装包运行 install.py --recover。') from exc


def install(source, root, *, ask, run, upgrade_to=None):
    recovered = recover_pending(root, run=run)
    if recovered == 'rolled-back':
        print('上次中断的安装已回退。')
    elif recovered == 'activated':
        print('已完成上次已启用的安装。')
    # Validate ancestors before inspecting any managed leaf.
    for relative in INSTALL_DIRS:
        path = root / relative
        if path.exists() or path.is_symlink():
            checked_directory(path)
    conf = root / 'etc/minis-linux'
    paths = [conf / name for name in CONFIG_FILES]
    if conf.exists() or conf.is_symlink():
        checked_directory(conf, private=True)
    present = [p.exists() or p.is_symlink() for p in paths]
    if any(present) and not all(present):
        raise ValueError('现有配置不完整；请检查 /etc/minis-linux，未覆盖任何文件。')
    launcher = root / 'usr/local/bin/minis'
    previous_launcher = snapshot_file(launcher)
    if previous_launcher is not None and b'# Minis Linux managed launcher\n' not in previous_launcher[0]:
        raise ValueError('/usr/local/bin/minis 已被其他程序使用；未覆盖。')
    app = root / 'opt/minis-linux'
    if upgrade_to is not None:
        if not all(present) or installed_version(source) != upgrade_to:
            raise ValueError('未安装完整的 Minis，或更新版本与安装包不匹配。')
        if version_tuple(upgrade_to) <= version_tuple(installed_version(app)):
            print('已是同版或更新版本，跳过安装。')
            return
    contents = None
    if all(present):
        for path in paths:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or not info.st_size):
                raise ValueError(f'现有配置文件为空或权限不安全：{path.name}')
    else:
        contents = config_bytes(ask())
    preflight(source, run=run)

    if contents is not None:
        mkdir_checked(conf.parent)
        with tempfile.TemporaryDirectory(prefix='.minis-config-', dir=conf.parent) as scratch:
            staged = Path(scratch) / 'config'
            staged.mkdir(mode=0o700)
            for name, value in zip(CONFIG_FILES, contents):
                atomic_write(staged / name, value, 0o600)
            if conf.exists() and any(conf.iterdir()):
                raise ValueError('现有配置目录非空，未覆盖任何配置。')
            staged.replace(conf)
            sync_directory(conf.parent)

    units = root / 'etc/systemd/system'
    for relative in INSTALL_DIRS:
        mkdir_checked(root / relative)
    # File renames sync their immediate parents, not newly created ancestors.
    # Persist the directory tree before any journal can depend on it.
    for parent in dict.fromkeys((root / relative).parent for relative in reversed(INSTALL_DIRS)):
        sync_directory(parent)
    payloads = {app / name: ((source / name).read_bytes(), 0o644) for name in APP_FILES}
    sample = app / SAMPLE
    if not (sample.exists() or sample.is_symlink()):
        payloads[sample] = ((source / SAMPLE).read_bytes(), 0o644)
    payloads[units / UNIT] = ((source / 'systemd' / UNIT).read_bytes(), 0o644)
    payloads[launcher] = (LAUNCHER, 0o755)
    activate(payloads, run=run, root=root)


def install_user(source, home):
    """Install a per-user terminal app without enabling any system service."""
    names = (*APP_FILES, SAMPLE)
    payloads = {}
    digest = sha256()
    for name in names:
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'安装包缺少普通文件：{name}')
        content = path.read_bytes()
        payloads[name] = content
        digest.update(name.encode() + b'\0' + sha256(content).digest())
    version = installed_version(source)

    base, bindir = home / '.minis', home / '.local/bin'
    launcher = bindir / 'minis'
    os.umask(0o077)
    checked_directory(home)
    mkdir_checked(bindir.parent)
    mkdir_checked(bindir)
    existing = snapshot_file(launcher)
    if existing is not None and not existing[0].startswith(USER_LAUNCHER):
        raise ValueError('~/.local/bin/minis 已被其他程序使用；未覆盖。')

    with terminal_lock(base):
        apps, state = base / 'app', base / 'terminal'
        mkdir_checked(apps, 0o700)
        bundle = apps / f'{version}-{digest.hexdigest()}'
        if bundle.exists() or bundle.is_symlink():
            for folder in (bundle, bundle / 'skills', (bundle / SAMPLE).parent):
                checked_directory(folder, private=True)
            for name, content in payloads.items():
                prior = snapshot_file(bundle / name)
                if prior is None or prior[0] != content:
                    raise ValueError('已安装程序文件与安装包不一致；未覆盖启动命令。')
        else:
            with tempfile.TemporaryDirectory(prefix='.staging-', dir=apps) as scratch:
                staged = Path(scratch) / 'bundle'
                staged.mkdir(mode=0o700)
                (staged / SAMPLE).parent.mkdir(mode=0o700, parents=True)
                for name, content in payloads.items():
                    atomic_write(staged / name, content, 0o600)
                staged.rename(bundle)
                sync_directory(apps)
        legacy = home / '.local/share/minis-linux-terminal'
        if legacy.exists() or legacy.is_symlink():
            checked_directory(legacy.parent)
            checked_directory(legacy, private=True)
            if state.exists() or state.is_symlink():
                raise ValueError('新旧终端状态目录同时存在；为避免覆盖，安装已停止。')
            with terminal_lock(legacy):
                terminal_config(legacy)
                legacy.rename(state)
        else:
            mkdir_checked(state, 0o700)
        entry = shell_quote(str(bundle / 'minis_cli.py'))
        atomic_write(launcher, USER_LAUNCHER + f'exec /usr/bin/python3 -EsB {entry} "$@"\n'.encode(), 0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='check package and systemd unit without installing')
    parser.add_argument('--recover', action='store_true', help='finish or roll back a pending installation without starting a new one')
    parser.add_argument('--credentials-dir', type=Path, help='read telegram.token and model.key from private files instead of prompting for secrets')
    parser.add_argument('--upgrade-to', help=argparse.SUPPRESS)
    parser.add_argument('--user', action='store_true', help='install only terminal chat into the current user home')
    args = parser.parse_args()
    if sys.platform != 'linux' or sys.version_info < (3, 10):
        parser.error('需要 Linux 和 Python 3.10+。')
    source = Path(__file__).resolve().parent

    def run(command, *, probe=False):
        return subprocess.run(command, check=not probe).returncode == 0

    try:
        if args.user:
            if args.check or args.recover or args.credentials_dir is not None or args.upgrade_to is not None:
                parser.error('--user 不能与系统安装选项合用')
            if os.geteuid() == 0 and os.environ.get('SUDO_UID', '') not in ('', '0'):
                raise ValueError('请直接用目标 Linux 用户运行 --user，不要加 sudo。')
            home = Path.home()
            install_user(source, home)
            print('终端已安装到 ~/.minis；未配置 Telegram。')
            if str(home / '.local/bin') in os.get_exec_path():
                print('在任意目录输入 minis 开始对话。')
            else:
                print('请将 ~/.local/bin 加入 PATH，或运行 ~/.local/bin/minis。')
            return 0
        os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
        if args.check:
            preflight(source, run=run)
            print('安装包和 systemd 服务文件检查通过；未安装或连接外部服务。')
            return 0
        if os.geteuid() != 0:
            raise ValueError('安装系统服务需要 sudo；使用 sudo /usr/bin/python3 -EsB install.py。')
        if not Path('/run/systemd/system').is_dir():
            raise ValueError('当前环境没有运行系统级 systemd。')
        os.umask(0o077)
        fd = os.open('/run/minis-linux-install.lock', os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('另一个 Minis 安装进程正在运行。') from None
            if args.recover:
                recovered = recover_pending(Path('/'), run=run)
                print({'rolled-back': '已恢复中断前的安装。', 'activated': '已完成已启用的安装。',
                       None: '没有待恢复的安装。'}[recovered])
                return 0
            install(source, Path('/'), ask=lambda: ask_config(args.credentials_dir), run=run,
                    upgrade_to=args.upgrade_to)
        print('Minis 已安装并设置开机启动。请在专用 Bot 私聊发 /status，再发一句话验证模型。')
        return 0
    except (OSError, ValueError, EOFError, termios.error, subprocess.CalledProcessError) as exc:
        print(f'安装未完成：{exc}', file=sys.stderr)
        if not args.user:
            print('若服务启动失败，请检查 sudo journalctl -u minis-linux-telegram.service -n 30。', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n安装已取消。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
