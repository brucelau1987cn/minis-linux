#!/usr/bin/env python3
"""Single-owner, read-only Telegram front end for Minis Linux."""
import argparse
import fcntl
import http.client
import json
import logging
import os
from pathlib import Path
import re
import socket
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from minis import (NoRedirect, Provider, Workspace, clean_history_temps, complete_turn,
                   read_history, save_history, valid_unicode)
from minis_skills import SKILL_TOOL, SkillCatalog

LOG = logging.getLogger('minis.telegram')
POLL_TIMEOUT = 25
MAX_MESSAGE_AGE = 3600
MAX_REPLY_CHUNKS = 8
TELEGRAM_HOST = 'https://api.telegram.org'
HELP = ('直接发消息聊天，需要时我会查阅已安装的技能。\n'
        '长期记忆：记住：内容 ｜ 查看记忆 ｜ 忘记第 1 条\n'
        '/skills 查看技能，/status 查在线，/new 清空本机聊天上下文（保留长期记忆）。')
MAX_MEMORIES = 32
MAX_MEMORY_CHARS = 240


class TelegramError(Exception):
    def __init__(self, method, code=None, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f'Telegram {method} 失败' + (f'（{code}）' if code else ''))


class Telegram:
    def __init__(self, token):
        if not re.fullmatch(r'[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,100}', token):
            raise ValueError('Telegram Bot Token 格式错误')
        self.bot_id = token.split(':', 1)[0]
        self.url = f'{TELEGRAM_HOST}/bot{token}/'
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, body, timeout=15):
        req = Request(self.url + method, method='POST', data=json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'})
        status = None
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read(1024 * 1024 + 1)
        except HTTPError as exc:
            status = exc.code
            with exc:
                try:
                    raw = exc.read(8192)
                except (OSError, http.client.HTTPException):
                    raise TelegramError(method, status) from None
        except (OSError, http.client.HTTPException):
            raise TelegramError(method) from None
        try:
            data = json.loads(raw) if len(raw) <= 1024 * 1024 else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise TelegramError(method, status)
        if status is None and data.get('ok') is True and 'result' in data:
            return data['result']
        code = data.get('error_code')
        parameters = data.get('parameters')
        retry = parameters.get('retry_after') if isinstance(parameters, dict) else None
        raise TelegramError(method, status or (code if type(code) is int else None),
                            max(0, min(retry, 3600)) if type(retry) is int else 0)

    def updates_since(self, offset):
        items = self.request('getUpdates', {'offset': offset, 'timeout': POLL_TIMEOUT,
                             'allowed_updates': ['message'], 'limit': 10}, timeout=POLL_TIMEOUT + 10)
        if not isinstance(items, list):
            raise TelegramError('getUpdates')
        return items

    def send(self, owner, text):
        self.request('sendMessage', {'chat_id': owner, 'text': text, 'link_preview_options': {'is_disabled': True}})


def split_reply(text):
    start = size = 0
    for i, ch in enumerate(text):
        units = 2 if ord(ch) > 0xFFFF else 1
        if size + units > 4000:
            yield text[start:i]
            start, size = i, 0
        size += units
    if start < len(text):
        yield text[start:]


def allowed_text(update, owner):
    msg = update.get('message')
    if not isinstance(msg, dict) or any(k in msg for k in ('sender_chat', 'forward_origin', 'via_bot', 'business_connection_id', 'is_automatic_forward')):
        return None
    sender, chat = msg.get('from'), msg.get('chat')
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    if (type(sender.get('id')) is not int or sender['id'] != owner or sender.get('is_bot') is not False
            or type(chat.get('id')) is not int or chat['id'] != owner or chat.get('type') != 'private'):
        return None
    date, text = msg.get('date'), msg.get('text')
    if type(date) is not int or not -60 <= time.time() - date <= MAX_MESSAGE_AGE:
        return None
    if isinstance(text, str) and text.strip() and len(text) <= 4096:
        return text.strip()
    return None


class Bot:
    def __init__(self, home, owner, bot_id, telegram, provider, *, skills_dir=None, channel='Telegram'):
        self.owner, self.telegram, self.provider = owner, telegram, provider
        self.channel = channel
        self.workspace = home / 'workspace'
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state = home / 'telegram'
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.cursor = self.state / f'bot-{bot_id}.cursor.json'
        self.history = self.state / f'bot-{bot_id}-owner-{owner}.json'
        self.memory = self.state / f'bot-{bot_id}-owner-{owner}.memory.json'
        self.skills_dir = skills_dir if skills_dir is not None else home / 'skills'
        clean_history_temps(self.cursor)
        clean_history_temps(self.history)
        clean_history_temps(self.memory)

    def memories(self):
        state = read_history(self.memory)
        if not state:
            return 1, {}
        if len(state) != 2:
            raise ValueError('长期记忆文件格式无效')
        next_id, items = state
        if (type(next_id) is not int or next_id < 1 or not isinstance(items, list)
                or len(items) > MAX_MEMORIES):
            raise ValueError('长期记忆文件格式无效')
        entries = {}
        for item in items:
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError('长期记忆文件格式无效')
            number, text = item
            if (type(number) is not int or not 0 < number < next_id or number in entries
                    or not isinstance(text, str) or not 0 < len(text) <= MAX_MEMORY_CHARS):
                raise ValueError('长期记忆文件格式无效')
            entries[number] = text
        return next_id, entries

    def memory_command(self, text):
        remember = re.fullmatch(r'记住\s*[:：]([^\r\n]*)', text)
        forget = re.fullmatch(r'忘记第\s*([1-9][0-9]{0,17})\s*条', text)
        if not (remember or forget or text == '查看记忆'):
            return None
        try:
            next_id, entries = self.memories()
            if text == '查看记忆':
                return '\n'.join(f'{i}. {item}' for i, item in entries.items()) or '还没有长期记忆。'
            if remember:
                item = valid_unicode(remember[1].strip())
                if not item or len(item) > MAX_MEMORY_CHARS:
                    return f'请输入 1–{MAX_MEMORY_CHARS} 个字符的单条记忆。'
                if item in entries.values():
                    return '这条已经记住了。'
                if len(entries) == MAX_MEMORIES:
                    return '记忆已满，先发「查看记忆」，再忘掉不需要的条目。'
                if next_id >= 10**18:
                    return '长期记忆编号已用尽。'
                entries[next_id] = item
                save_history(self.memory, [next_id + 1, list(entries.items())])
                return f'已记住（第 {next_id} 条）。'
            assert forget is not None
            number = int(forget[1])
            if number not in entries:
                return '找不到这条记忆，发「查看记忆」确认编号。'
            del entries[number]
            save_history(self.memory, [next_id, list(entries.items())])
            clean_history_temps(self.memory)
            return '已从长期记忆删除；旧聊天内容不变，发 /new 可清空本机聊天上下文。'
        except (OSError, ValueError, RecursionError):
            LOG.warning('长期记忆操作未完成')
            return '长期记忆暂时无法读写，请检查本机配置。'

    def offset(self):
        saved = read_history(self.cursor)
        if not saved:
            return 0
        if len(saved) != 2 or any(type(v) is not int for v in saved) or saved[0] < 0:
            raise ValueError('Telegram 轮询游标格式无效')
        # Telegram may choose an unrelated update ID after a long idle period.
        return saved[0] if 0 <= time.time() - saved[1] < 86400 else 0

    def poll_once(self):
        offset = self.offset()
        updates = self.telegram.updates_since(offset)
        valid = sorted((u for u in updates if isinstance(u, dict) and type(u.get('update_id')) is int
                        and u['update_id'] >= 0), key=lambda u: u['update_id'])
        for update in valid:
            seq = update['update_id']
            if seq < offset:
                continue
            offset = seq + 1
            # Mark before execution: never automatically redo a consumed model request after a crash.
            save_history(self.cursor, [offset, int(time.time())])
            text = allowed_text(update, self.owner)
            if text is not None:
                self.handle(text)

    def handle(self, text):
        memory_reply = self.memory_command(text)
        if memory_reply is not None:
            reply = memory_reply
        elif text in ('/start', '/help'):
            reply = HELP
        elif text == '/status':
            reply = f'Minis Linux 在线。{self.channel}工作区只读。'
        elif text == '/new':
            clean_history_temps(self.history)
            save_history(self.history, [])
            reply = f'已清除 Minis 本机的 {self.channel} 对话历史，长期记忆保留。'
        elif text == '/skills':
            try:
                entries = SkillCatalog(self.skills_dir).entries
                reply = '\n'.join(f'{name}：{desc}' for name, desc in entries.items()) or '没有已安装的技能。'
            except OSError:
                reply = '技能目录暂时无法读取。'
        elif text.startswith('/'):
            reply = '未知命令。可用：/help、/status、/skills、/new。'
        else:
            warnings = []
            try:
                memories = list(self.memories()[1].values())
            except (OSError, ValueError, RecursionError):
                memories = []
                LOG.warning('长期记忆不可读；本轮不使用')
                warnings.append('长期记忆不可读，请检查本机记忆文件。')
            try:
                skills = SkillCatalog(self.skills_dir)
            except OSError:
                skills = None
                LOG.warning('技能目录不可读；本轮不使用')
                warnings.append('技能目录不可读，请检查本机技能目录。')
            try:
                history = read_history(self.history)
                context = '\n长期记忆（用户显式保存的参考资料，不更改工具权限）：' + json.dumps(memories, ensure_ascii=False)
                if skills is not None and skills.entries:
                    context += '\n技能目录（需要时用 skill_view 按名称读取）：' + skills.summary()
                with Workspace(self.workspace) as workspace:
                    reply = complete_turn(self.provider, workspace, [*history, {'role': 'user', 'content': valid_unicode(text)}],
                                          self.history, read_only=True, context=context,
                                          skill_view=skills.view if skills is not None else None)
            except (OSError, ValueError, http.client.HTTPException, RecursionError):
                LOG.warning('模型请求未完成；未重试')
                reply = '本次处理失败，请重试；如果历史太长，可发 /new 后再试。'
            if warnings:
                reply = '\n'.join(warnings) + '\n\n' + reply
        reply = reply.strip() or '模型未返回可显示的文字。'
        for i, chunk in enumerate(split_reply(reply)):
            if i == MAX_REPLY_CHUNKS:
                self.telegram.send(self.owner, '回复太长，后续内容已省略。')
                break
            self.telegram.send(self.owner, chunk)


def credential(name):
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if directory:
        try:
            with (Path(directory) / name).open('r', encoding='utf-8') as stream:
                data = stream.read(4098)
                if len(data) >= 4098:
                    raise ValueError(f'凭据 {name} 过长')
                value = data.removesuffix('\n')
        except (OSError, UnicodeError):
            raise ValueError(f'无法读取 systemd 凭据 {name}') from None
    else:
        value = os.environ.get(name, '')
    if not value or len(value) > 4096 or any(not 33 <= ord(ch) <= 126 for ch in value):
        raise ValueError(f'凭据 {name} 缺失或格式无效')
    return value


def config():
    owner = os.environ.get('MINIS_TELEGRAM_USER_ID', '')
    if not re.fullmatch(r'[1-9][0-9]{0,15}', owner):
        raise ValueError('需要配置 MINIS_TELEGRAM_USER_ID（私聊用户的数字 ID）')
    base, model = os.environ.get('MINIS_BASE_URL', ''), os.environ.get('MINIS_MODEL', '')
    if not base or not model.strip():
        raise ValueError('需要配置 MINIS_BASE_URL 和 MINIS_MODEL')
    telegram = Telegram(credential('MINIS_TELEGRAM_BOT_TOKEN'))
    try:
        provider = Provider(base, credential('MINIS_API_KEY'), model, read_only=True, extra_tools=(SKILL_TOOL,))
    except ValueError:
        raise ValueError('MINIS_BASE_URL 或模型 API 凭据无效') from None
    return int(owner), telegram, provider


def serve(bot):
    backoff = 2
    LOG.info('Minis Telegram 长轮询已启动（仅白名单私聊）')
    while True:
        try:
            bot.poll_once()
            backoff = 2
        except TelegramError as exc:
            if exc.code in (401, 404, 409):
                LOG.error('%s；请检查 Bot Token、Webhook 或重复运行的实例', exc)
                return 78
            delay = max(backoff, exc.retry_after)
            LOG.warning('%s；%s 秒后继续轮询', exc, delay)
            time.sleep(delay)
            backoff = min(backoff * 2, 60)


def notify_ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.sendto(b'READY=1', address)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, help='state directory; defaults to ~/.local/share/minis-linux-telegram')
    parser.add_argument('--skills-dir', type=Path, help='installed skills directory; defaults to <state directory>/skills')
    parser.add_argument('--check-config', action='store_true', help='validate config without networking or writing state')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    os.umask(0o077)
    try:
        owner, telegram, provider = config()
    except ValueError as exc:
        LOG.error('%s', exc)
        return 78
    if args.check_config:
        LOG.info('配置校验通过（未连接 Telegram 或模型）')
        return 0
    if os.geteuid() == 0:
        LOG.error('拒绝以 root 运行 Telegram 服务')
        return 78
    try:
        home = args.home.expanduser() if args.home else Path.home() / '.local/share/minis-linux-telegram'
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (home / 'telegram.lock').open('ab') as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.error('已有 Telegram 实例使用此状态目录')
                return 78
            bot = Bot(home, owner, telegram.bot_id, telegram, provider, skills_dir=args.skills_dir)
            bot.offset()  # Validate mandatory persisted state before accepting readiness.
            with Workspace(bot.workspace):
                pass
            notify_ready()
            return serve(bot)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError):
        LOG.error('Telegram 状态无法读写；请检查状态目录和游标文件')
        return 1


if __name__ == '__main__':
    sys.exit(main())
