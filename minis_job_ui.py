"""Telegram scheduling adapter. Call command() only for authenticated owner text.

The parent checks the private chat and sender before dispatch; neither model
output nor forwarded content may be sent to command(). The bot's state path
and configured bot/owner are trusted and fixed for the lifetime of the adapter.
Job text lives in the private scheduler DB, not the operation log. Enable INFO
on this module's logger for successful mutations and run outcomes; only IDs,
operation names and outcomes are logged. Claims are not replayed after a crash.
"""

from contextlib import nullcontext
from dataclasses import dataclass
import logging
from pathlib import Path
import re
from secrets import token_hex
import sqlite3
from threading import Event, Lock, Thread
from time import monotonic
from typing import Callable, ContextManager
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from minis import Provider, Workspace, complete_turn, safe_display
from minis_schedule import ACTION, MAX_TEXT, PromptRunner, Scheduler


LOG = logging.getLogger(__name__)
ID = r'[0-9a-f]{32}'
COMMAND = re.compile(r'/schedule(?=\s|$)')

TTL = 60
PAGE_SIZE = 12
HELP = ('定时任务：/schedule <时间和内容>；例如「10分钟后提醒我喝水」或 '
        '“daily at 09:00 remind me: check calendar”。\n'
        '只读模型任务：/schedule every 2 hours read-only prompt: <问题>。\n'
        '/schedule list [页码]；/schedule pause|resume|remove|run <任务ID>。\n'
        '新建和修改须发送预览中的完整确认命令。')


def _literal(text):
    # Prefix every line so job contents cannot impersonate the confirmation.
    escaped = safe_display(text).replace('\u2028', r'\u2028').replace('\u2029', r'\u2029')
    return '│ ' + escaped.replace('\n', '\n│ ')


@dataclass
class _ActiveRun:
    stopped: bool = False
    sending: bool = False


class JobUI:
    def __init__(self, bot, *, tz='UTC', prompt_slot: Callable[[], ContextManager[bool]] | None = None):
        self.bot = bot
        try:
            self.tz = ZoneInfo(tz)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            raise ValueError('invalid or unavailable scheduler time zone') from None
        if prompt_slot is not None and not callable(prompt_slot):
            raise ValueError('prompt_slot must be a context manager factory')
        self.prompt_slot = prompt_slot
        self.owner = bot.owner
        # Bot has no bot_id field at baseline. Unlike history (which can be
        # switched), the bot cursor filename is fixed, with its numeric ID.
        cursor = re.fullmatch(r'bot-([1-9][0-9]{0,19})\.cursor\.json', Path(bot.cursor).name)
        if cursor is None or type(self.owner) is not int or not 0 < self.owner < 10**20:
            raise ValueError('scheduler requires a numeric bot and owner scope')
        path = Path(bot.state) / f'bot-{cursor[1]}-owner-{self.owner}.jobs.sqlite3'
        self.scheduler = Scheduler(path)
        self._lock = Lock()
        self._closed = False
        self._pending = None
        self._shutdown = Event()
        self._worker: Thread | None = None
        self._active: _ActiveRun | None = None

    def close(self):
        """Stop claiming runs and drop late results without waiting for I/O."""
        with self._lock:
            self._closed = True
            self._shutdown.set()
            self._pending = None
            self.scheduler.close()

    def stop(self) -> str | None:
        """Handle /stop for a scheduled run; None if no scheduled run is active."""
        with self._lock:
            active = self._active
            if self._closed or active is None:
                return None
            if active.sending:
                return '定时任务已进入结果发送阶段，不能再撤回。'
            active.stopped = True
            return '已停止本地定时任务，不会发送迟到结果；模型网关可能仍在处理。'

    def start(self, *, interval=1.0, clock=None) -> Thread:
        """Start the daemon worker; return its Thread. The slot factory must not block.

        prompt_slot() yields True only if it reserved the shared model slot.
        It is entered for each tick and exited after any delivery. When it
        yields False, reminders still run and prompts are left unclaimed.
        Without a slot factory, the parent asserts no mutual exclusion is needed.
        """
        if (type(interval) not in (float, int) or not 0.01 <= interval <= 60
                or (clock is not None and not callable(clock))):
            raise ValueError('invalid job worker configuration')
        with self._lock:
            if self._closed:
                raise RuntimeError('job service is closed')
            if self._worker is not None and self._worker.is_alive():
                return self._worker
            self._worker = Thread(target=self._work, args=(interval, clock), name='minis-jobs', daemon=True)
            self._worker.start()
            return self._worker

    def _work(self, interval, clock):
        prompt = PromptRunner(self._prompt, read_only=True, allow_execute=False, allow_write=False)
        while not self._shutdown.is_set():
            result = None
            try:
                slot = self.prompt_slot() if self.prompt_slot is not None else nullcontext(True)
                with slot as acquired:
                    result = self.scheduler.tick(deliver=self._deliver, prompt=prompt,
                                                 allow_prompt=acquired is True, now=clock() if clock else None)
            except Exception:
                LOG.error('job worker stopped due to a state or integration error')
                return
            finally:
                with self._lock:
                    active, self._active = self._active, None
                    interrupted = active is not None and not active.sending and (active.stopped or self._closed)
            if result is not None:
                run = result.run
                # Scheduler records callback exceptions generically; the log
                # can distinguish a local stop without writing extra DB state.
                LOG.info('job run job=%s run=%s kind=%s trigger=%s outcome=%s',
                         run.job_id, run.id, run.kind, run.trigger, 'interrupted' if interrupted else result.outcome)
            self._shutdown.wait(interval)

    def _prompt(self, _run, text, *, read_only, allow_execute, allow_write):
        if read_only is not True or allow_execute is not False or allow_write is not False:
            raise RuntimeError('unsafe job runner flags')
        with self._lock:
            if self._closed or self.bot.owner != self.owner:
                raise RuntimeError('job service is unavailable')
            active = self._active = _ActiveRun()

        def checkpoint(_phase):
            with self._lock:
                if self._closed or active.stopped or self.bot.owner != self.owner:
                    raise RuntimeError('job stopped')

        source = self.bot.provider
        if not isinstance(source, Provider):
            raise RuntimeError('scheduled prompts require a configured Provider')
        provider = Provider(source.endpoint.removesuffix('/chat/completions'), source.key, source.model,
                            read_only=True)
        messages = [{'role': 'user', 'content': text}]
        with Workspace(self.bot.workspace) as workspace:
            reply = complete_turn(provider, workspace, messages, None, read_only=True, checkpoint=checkpoint,
                                  context='\n这是单独运行的定时任务；没有聊天历史、长期记忆或技能。')
        return reply if len(reply) <= MAX_TEXT else reply[:MAX_TEXT - 1] + '…'

    def _deliver(self, run, text):
        with self._lock:
            if self._closed or self.bot.owner != self.owner:
                raise RuntimeError('job delivery is unavailable')
            if self._active is None:
                self._active = _ActiveRun()
            if self._active.stopped:
                raise RuntimeError('job stopped')
            self._active.sending = True  # Close/stop cannot recall a committed send.
        label = '提醒' if run.kind == 'reminder' else '定时任务结果'
        self.bot.send_reply(f'{label}（{run.job_id}）：\n{text}')

    def _at(self, value):
        return value.astimezone(self.tz).isoformat(timespec='seconds') if value else '—'

    def _list(self, page):
        jobs = self.scheduler.list()
        if not jobs:
            return '没有定时任务。'
        pages = (len(jobs) + PAGE_SIZE - 1) // PAGE_SIZE
        if page > pages:
            return f'页码超出范围（共 {pages} 页）。'
        lines = [f'定时任务（{page}/{pages}，{self.tz.key}）：']
        for job in jobs[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
            label = ' '.join(job.proposal.text.split())
            if len(label) > 60:
                label = label[:60] + '…'
            detail = f' · 上次：{job.last_outcome}' if job.last_outcome else ''
            if job.manual_at:
                detail += ' · 已排队手动运行'
            lines.append(f'{job.id} · {job.status} · {job.proposal.kind} · 下次 {self._at(job.next_at)}{detail}')
            lines.append(_literal(label))
        return '\n'.join(lines)

    def _preview(self, operation, target, detail):
        nonce = token_hex(16)
        self._pending = (nonce, monotonic() + TTL, operation, target)
        return f'{detail}\n如确认，请在 {TTL} 秒内单独发送：\n/schedule confirm {nonce}'

    def _confirm(self, nonce):
        pending = self._pending
        if pending is None or pending[0] != nonce or monotonic() >= pending[1]:
            return '确认已过期、已使用或不匹配；请重新申请。'
        _, _, operation, target = pending
        self._pending = None  # Consume before the mutation; never replay this nonce.
        if operation == 'create':
            job = self.scheduler.create(target, confirmed=True)
        else:
            mutation = self.scheduler.run_now if operation == 'run' else getattr(self.scheduler, operation)
            job = mutation(target, confirmed=True)
        LOG.info('job mutation action=%s job=%s', operation, job.id)
        if operation == 'run':
            return f'已排队手动运行：{job.id}'
        if operation == 'remove':
            return f'已移除定时任务：{job.id}'
        return f'定时任务 {job.id} · {job.status} · 下次 {self._at(job.next_at)}'

    def command(self, text) -> str | None:
        """Return an owner-facing reply; None means this is not a job command."""
        slash = COMMAND.match(text)
        if len(text) > 4096:
            return '定时任务命令过长。' if slash else None
        proposal = None
        if slash is None:
            if not ACTION.search(text):
                return None
            # A bare message is a command only if the scheduler recognizes a
            # full expression. Use /schedule for syntax/validation feedback.
            try:
                proposal = self.scheduler.propose(text, tz=self.tz.key)
            except ValueError:
                return None
        with self._lock:
            if self._closed:
                return '定时任务服务已关闭。'
            if self.bot.owner != self.owner:
                return '定时任务服务不可用；配置的所有者已变更。'
            expr = text[slash.end():].strip() if slash else text
            try:
                if slash:
                    if expr in ('', 'help'):
                        return HELP
                    if match := re.fullmatch(r'/schedule confirm (' + ID + ')', text):
                        return self._confirm(match[1])
                    if match := re.fullmatch(r'list(?: ([1-9][0-9]{0,2}))?', expr):
                        return self._list(int(match[1] or '1'))
                    if match := re.fullmatch(r'(pause|resume|remove|run) (' + ID + ')', expr):
                        operation, job_id = match.groups()
                        job = next((j for j in self.scheduler.list() if j.id == job_id), None)
                        if job is None:
                            return '找不到此定时任务，请发送 /schedule list。'
                        detail = f'{operation} · {job.id} · {job.proposal.kind}\n{_literal(job.proposal.text)}'
                        if operation in ('pause', 'remove'):
                            detail += '\n不会撤回已经领取的运行。'
                        return self._preview(operation, job_id, detail)
                    if expr.split(maxsplit=1)[0] in ('confirm', 'list', 'pause', 'resume', 'remove', 'run'):
                        return HELP
                if proposal is None:
                    proposal = self.scheduler.propose(expr, tz=self.tz.key)
                schedule = proposal.schedule
                cadence = (f' · 每 {schedule.interval_minutes} 分钟' if schedule.interval_minutes else
                           (' · 每日' if schedule.weekday is None else ' · 每周') if schedule.tz else ' · 单次')
                detail = f'{proposal.kind}{cadence} · 首次 {self._at(schedule.first_at)} ({self.tz.key})'
                if proposal.kind == 'prompt':
                    detail += '\n模型只能读取专用工作区；问题及读取的资料可发送给配置的模型。'
                detail += '\n内容会以明文保存在本机。\n' + _literal(proposal.text)
                return self._preview('create', proposal, detail)
            except (ValueError, OverflowError, RuntimeError, KeyError):
                return '无法使用该时间、任务或当前状态；请检查 /schedule help 或重新预览。'
            except (OSError, sqlite3.Error):
                LOG.warning('job command failed due to local scheduler state')
                return '定时任务状态暂时不可用。'
