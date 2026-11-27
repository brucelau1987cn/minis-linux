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
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from minis import (NoRedirect, Provider, Workspace, clean_history_temps, complete_turn,
                   read_history, save_history, valid_unicode)

LOG = logging.getLogger('minis.telegram')
POLL_TIMEOUT = 25
MAX_MESSAGE_AGE = 3600
MAX_REPLY_CHUNKS = 8
TELEGRAM_HOST = 'https://api.telegram.org'
HELP = 'Minis Linux 已就绪。发一条文字即可对话；工作区只读。\n/status 查看状态，/new 清除本入口的对话历史。'


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
    def __init__(self, home, owner, bot_id, telegram, provider):
        self.owner, self.telegram, self.provider = owner, telegram, provider
        self.workspace = home / 'workspace'
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state = home / 'telegram'
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.cursor = self.state / f'bot-{bot_id}.cursor.json'
        self.history = self.state / f'bot-{bot_id}-owner-{owner}.json'
        clean_history_temps(self.cursor)
        clean_history_temps(self.history)

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
        if text in ('/start', '/help'):
            reply = HELP
        elif text == '/status':
            reply = 'Minis Linux 在线。Telegram 工作区只读。'
        elif text == '/new':
            clean_history_temps(self.history)
            save_history(self.history, [])
            reply = '已清除 Minis 本机的 Telegram 对话历史。'
        elif text.startswith('/'):
            reply = '未知命令。可用：/help、/status、/new。'
        else:
            try:
                history = read_history(self.history)
                with Workspace(self.workspace) as workspace:
                    reply = complete_turn(self.provider, workspace, [*history, {'role': 'user', 'content': valid_unicode(text)}],
                                          self.history, read_only=True)
            except (OSError, ValueError, http.client.HTTPException, RecursionError):
                LOG.warning('模型请求未完成；未重试')
                reply = '本次处理失败，请重试；如果历史太长，可发 /new 后再试。'
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
        provider = Provider(base, credential('MINIS_API_KEY'), model, read_only=True)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, help='state directory; defaults to ~/.local/share/minis-linux-telegram')
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
            return serve(Bot(home, owner, telegram.bot_id, telegram, provider))
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError):
        LOG.error('Telegram 状态无法读写；请检查状态目录和游标文件')
        return 1


if __name__ == '__main__':
    sys.exit(main())
