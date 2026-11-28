"""Durable, per-owner scheduling. The parent supplies authorization and I/O.

Use a separate DB path in a trusted private state directory per bot/owner. The
parent binds confirmation to the exact Proposal, authorizes every mutation and
keeps its own operation log. Treat stored action text as plaintext. Nothing in
this module interprets action text as a command or grants tools.

Example expressions: ``10分钟后提醒我喝水``, ``每隔2小时提醒我起来活动``,
``每天早上9点提醒我吃药``, ``每周一08:30只读提问：想一个写作题目``,
and ``2026-12-01T09:00+08:00 提醒我开会``. English forms include
``in 10 minutes``, ``every 2 hours``, ``daily at 09:00`` and
``weekly on monday at 09:00``, followed by ``remind me:`` or
``read-only prompt:`` and text.

Ticks consume at most one occurrence. After downtime they coalesce missed
recurrences to the latest due occurrence and expire any run older than 24h.
One-shot state and the next recurrence advance before I/O. A crash can lose a
delivery, but never automatically replays a claimed occurrence.
"""

import fcntl
import logging
import math
import os
import re
import sqlite3
import stat
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, ContextManager
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MICROSECOND = timedelta(microseconds=1)
MAX_JOBS = 100
MAX_TEXT = 3000
MAX_PERIOD = 10_000
STALE_AFTER = timedelta(hours=24)
WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
CN_WEEKDAYS = '一二三四五六日'
ACTION = re.compile(r'(提醒我|只读提问|\bremind me\b|\bread-only prompt\b)\s*[:：]?\s*', re.I)
ISO = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?(?:Z|[+-][0-9]{2}:[0-9]{2})')
LOG = logging.getLogger(__name__)


def _now(value):
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('an aware datetime is required')
    value = value.astimezone(UTC)
    if not 1970 <= value.year <= 9997:
        raise ValueError('datetime out of range')
    return value


def _stamp(value):
    return (value - EPOCH) // MICROSECOND


def _datetime(value: int) -> datetime:
    return EPOCH + value * MICROSECOND


def _optional_time(value):
    return None if value is None else _datetime(value)


def _text(value, limit=MAX_TEXT):
    if (not isinstance(value, str) or not 0 < len(value) <= limit
            or not value.strip() or any(c == '\0' or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('invalid or oversized text')
    return value


def _approval(confirmed):
    if confirmed is not True:
        raise PermissionError('explicit user confirmation required')


@dataclass(frozen=True)
class Schedule:
    first_at: datetime
    interval_minutes: int | None = None
    tz: str | None = None
    wall_minute: int | None = None
    weekday: int | None = None  # None means daily when tz is set; Monday is 0.


@dataclass(frozen=True)
class Proposal:
    schedule: Schedule
    kind: str
    text: str


@dataclass(frozen=True)
class Job:
    id: str
    proposal: Proposal
    status: str
    next_at: datetime | None
    manual_at: datetime | None
    last_outcome: str | None


@dataclass(frozen=True)
class Run:
    id: str
    job_id: str
    kind: str
    trigger: str
    scheduled_at: datetime


@dataclass(frozen=True)
class RunResult:
    run: Run
    outcome: str


@dataclass(frozen=True)
class PromptRunner:
    """Trusted parent adapter. These flags are assertions, not a sandbox.

    callback(run, text, *, read_only, allow_execute, allow_write) -> str.
    The parent must enforce read-only tools at dispatch (no exec/write), use
    isolated context with no history writes, and set finite I/O timeouts.
    """

    callback: Callable[..., str]
    read_only: bool
    allow_execute: bool
    allow_write: bool

    def __post_init__(self):
        self._validate()

    def _validate(self):
        if (not callable(self.callback) or self.read_only is not True
                or self.allow_execute is not False or self.allow_write is not False):
            raise ValueError('scheduled prompts require a read-only provider without execution or write tools')


def _callbacks(deliver, prompt):
    if not callable(deliver):
        raise ValueError('delivery callback is required')
    if prompt is not None:
        if type(prompt) is not PromptRunner:
            raise ValueError('prompt must be a PromptRunner')
        prompt._validate()


def _job(row):
    return Job(row['id'], Proposal(_schedule(row), row['kind'], row['text']), row['status'],
               _optional_time(row['next_at']), _optional_time(row['manual_at']), row['last_outcome'])


def _schedule(row):
    return Schedule(_datetime(row['first_at']), row['interval_minutes'], row['tz'], row['wall_minute'], row['weekday'])


def _zone(tz):
    if not isinstance(tz, str) or not 0 < len(tz) <= 128 or '\0' in tz:
        raise ValueError('invalid IANA time zone')
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('invalid or unavailable IANA time zone') from None


def _calendar_time(reference, tz, wall_minute, weekday, *, forward):
    zone = _zone(tz)
    date = reference.astimezone(zone).date()
    direction = 1 if forward else -1
    stride = 1 if weekday is None else 7
    if weekday is not None:
        date += timedelta(days=direction * ((weekday - date.weekday()) * direction % 7))
    for _ in range(8):  # Bound lookup even if tzdata is unusual.
        local = datetime(date.year, date.month, date.day, *divmod(wall_minute, 60), tzinfo=zone)
        utc = local.astimezone(UTC)  # fold=0: use the first copy of an ambiguous time.
        if utc.astimezone(zone) == local:  # Nonexistent local times fail the round trip.
            if (forward and utc > reference) or (not forward and utc <= reference):
                return utc
        date += timedelta(days=stride * direction)
    raise ValueError('unable to resolve calendar occurrence')


def _wall_clock(value):
    if match := re.fullmatch(r'([0-9]{1,2}):([0-9]{2})', value):
        hour, minute = map(int, match.groups())
    elif match := re.fullmatch(r'(凌晨|早上|上午|中午|下午|晚上)?([0-9]{1,2})点(?:([0-9]{1,2})分?)?', value):
        period, h, m = match.groups()
        hour, minute = int(h), int(m or '0')
        if period:
            if not 1 <= hour <= 12:
                raise ValueError('time of day requires a 1–12 hour')
            hour = hour % 12 + (12 if period in ('中午', '下午', '晚上') else 0)
    else:
        raise ValueError('invalid wall-clock time')
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError('invalid wall-clock time')
    return hour * 60 + minute


def _parse_time(spec, now, tz):
    if ISO.fullmatch(spec):
        # fromisoformat can normalize an invalid offset such as +00:99.
        if not spec.endswith('Z') and (int(spec[-5:-3]) >= 24 or int(spec[-2:]) >= 60):
            raise ValueError('invalid ISO offset')
        first = datetime.fromisoformat(spec[:-1] + '+00:00' if spec.endswith('Z') else spec)
        return Schedule(_now(first))
    en = spec.lower()
    match = re.fullmatch(r'(every|in)\s+([1-9][0-9]{0,4})\s+(minutes?|hours?)', en)
    cn = re.fullmatch(r'(每隔|每)?\s*([1-9][0-9]{0,4})\s*(分钟|小时)\s*(后)?', spec)
    if match or (cn and bool(cn[1]) != bool(cn[4])):
        if match:
            repeat, count, hours = match[1] == 'every', int(match[2]), match[3].startswith('hour')
        else:
            assert cn is not None
            repeat, count, hours = bool(cn[1]), int(cn[2]), cn[3] == '小时'
        if count > MAX_PERIOD:
            raise ValueError('interval is too large')
        minutes = count * (60 if hours else 1)
        return Schedule(now + timedelta(minutes=minutes), minutes if repeat else None)
    weekday = None
    if en.startswith('daily at '):
        clock = en.removeprefix('daily at ')
    elif match := re.fullmatch(r'weekly on (' + '|'.join(WEEKDAYS) + r') at (.+)', en):
        weekday, clock = WEEKDAYS.index(match[1]), match[2]
    elif spec.startswith('每天'):
        clock = spec.removeprefix('每天').strip()
    elif match := re.fullmatch(r'每(?:周|星期)([一二三四五六日天])\s*(.+)', spec):
        weekday, clock = CN_WEEKDAYS.index(match[1].replace('天', '日')), match[2]
    else:
        raise ValueError('unsupported time; use a relative time, daily/weekly time or ISO with offset')
    wall = _wall_clock(clock)
    return Schedule(_calendar_time(now, tz, wall, weekday, forward=True), tz=tz, wall_minute=wall, weekday=weekday)


def _validate_schedule(schedule):
    if type(schedule) is not Schedule or not isinstance(schedule.first_at, datetime):
        raise ValueError('invalid schedule')
    first = _now(schedule.first_at)
    interval, tz, wall, weekday = schedule.interval_minutes, schedule.tz, schedule.wall_minute, schedule.weekday
    if tz is not None:
        if (interval is not None or type(wall) is not int or not 0 <= wall < 1440
                or (weekday is not None and (type(weekday) is not int or not 0 <= weekday < 7))):
            raise ValueError('invalid calendar schedule')
        if _calendar_time(first, tz, wall, weekday, forward=False) != first:
            raise ValueError('invalid first calendar occurrence')
    elif (wall is not None or weekday is not None or (interval is not None and
          (type(interval) is not int or not 1 <= interval <= MAX_PERIOD * 60))):
        raise ValueError('invalid interval schedule')
    return Schedule(first, interval, tz, wall, weekday)


def _catch_up(schedule, due, now):
    """Return latest missed occurrence and first future occurrence, without replay loops."""
    if schedule.interval_minutes is not None:
        step = timedelta(minutes=schedule.interval_minutes)
        latest = due + ((now - due) // step) * step
        return latest, latest + step
    if schedule.tz is not None:
        args = schedule.tz, schedule.wall_minute, schedule.weekday
        return (max(due, _calendar_time(now, *args, forward=False)), _calendar_time(now, *args, forward=True))
    return due, None


def _open_private(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        os.close(fd)
        raise ValueError('scheduler state must be a private file owned by the service user')
    return fd


class Scheduler:
    """Use close() (or a with block) to release the exclusive DB lease."""

    def __init__(self, path):
        self.path = Path(path).absolute()
        self._lock = threading.Lock()
        self._closed = False
        self._busy = False
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._lease = _open_private(self.path.with_name(self.path.name + '.lock'))
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.close(_open_private(self.path))
            self._db = sqlite3.connect(str(self.path), timeout=0.25, check_same_thread=False)
            try:
                self._db.row_factory = sqlite3.Row
                self._db.execute('PRAGMA synchronous=FULL')
                self._initialize()
            except BaseException:
                self._db.close()
                raise
        except BaseException:
            os.close(self._lease)
            raise

    def _initialize(self):
        version = self._db.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            with self._db:
                self._db.execute('BEGIN IMMEDIATE')
                self._db.execute('''CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('reminder', 'prompt')), text TEXT NOT NULL,
                    first_at INTEGER NOT NULL, interval_minutes INTEGER, tz TEXT,
                    wall_minute INTEGER, weekday INTEGER, next_at INTEGER, manual_at INTEGER,
                    status TEXT NOT NULL, run_id TEXT, last_outcome TEXT
                )''')
                self._db.execute('PRAGMA user_version=1')
        elif version != 1:
            raise ValueError('unsupported scheduler schema version')
        with self._db:
            self._db.execute('''UPDATE jobs SET run_id=NULL, last_outcome='interrupted'
                                WHERE run_id IS NOT NULL''')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Stop new claims/sends, close the DB and release its lease without joining I/O.

        An already-started provider call cannot be stopped here, but its result
        is dropped. A send past its commit point may still complete. Neither
        callback may access the scheduler DB or write other parent state.
        """
        with self._lock:
            if not self._closed:
                self._closed = True
                self._stop.set()
                try:
                    self._db.close()
                finally:
                    os.close(self._lease)  # Do not unlink the lock file.

    def _check_open(self):
        if self._closed:
            raise RuntimeError('scheduler is closed')

    def propose(self, expression, *, now=None, tz='UTC') -> Proposal:
        """Preview only. tz is an IANA zone for daily/weekly wall times (default UTC)."""
        expression = _text(expression, 3500).strip()
        match = ACTION.search(expression)
        if not match:
            raise ValueError('use a reminder or an explicitly read-only prompt')
        text = _text(expression[match.end():])
        kind = 'reminder' if match[1].lower() in ('提醒我', 'remind me') else 'prompt'
        reference = _now(now)
        try:
            schedule = _validate_schedule(_parse_time(expression[:match.start()].strip(), reference, tz))
        except OverflowError:
            raise ValueError('schedule time out of range') from None
        if schedule.first_at <= reference:
            raise ValueError('the schedule must be in the future')
        return Proposal(schedule, kind, text)

    def create(self, proposal, *, confirmed=False, now=None) -> Job:
        _approval(confirmed)
        if type(proposal) is not Proposal or proposal.kind not in ('reminder', 'prompt'):
            raise ValueError('invalid proposal')
        _text(proposal.text)
        schedule = _validate_schedule(proposal.schedule)
        proposal = Proposal(schedule, proposal.kind, proposal.text)
        first = schedule.first_at
        if first <= _now(now):
            raise ValueError('the preview has passed; propose a new schedule')
        job = Job(uuid4().hex, proposal, 'active', first, None, None)
        with self._lock:
            self._check_open()
            if self._db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= MAX_JOBS:
                raise OverflowError('scheduler job limit reached; remove a job first')
            with self._db:
                self._db.execute('''INSERT INTO jobs(id, kind, text, first_at, interval_minutes, tz, wall_minute,
                                                     weekday, next_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                 (job.id, proposal.kind, proposal.text, _stamp(first), schedule.interval_minutes,
                                  schedule.tz, schedule.wall_minute, schedule.weekday, _stamp(first), job.status))
        return job

    def list(self) -> list[Job]:
        with self._lock:
            self._check_open()
            return [_job(row) for row in self._db.execute('SELECT * FROM jobs ORDER BY first_at, id')]

    def _get(self, job_id):
        self._check_open()
        if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise KeyError('unknown schedule ID')
        row = self._db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise KeyError('unknown schedule ID')
        return row

    def pause(self, job_id, *, confirmed=False) -> Job:
        """Cancel pending manual runs and pause future claims, not a run already claimed."""
        _approval(confirmed)
        with self._lock:
            row = self._get(job_id)
            if row['status'] == 'completed':
                raise RuntimeError('completed job cannot be paused')
            with self._db:
                self._db.execute("UPDATE jobs SET status='paused', manual_at=NULL WHERE id=?", (job_id,))
            return _job(self._get(job_id))

    def resume(self, job_id, *, confirmed=False, now=None) -> Job:
        """Resume at the next future occurrence; do not replay time missed while paused."""
        _approval(confirmed)
        now = _now(now)
        with self._lock:
            row = self._get(job_id)
            if row['status'] != 'paused':
                raise RuntimeError('job is not paused')
            due = _datetime(row['next_at'])
            if due > now:
                next_at = due
            else:
                _, next_at = _catch_up(_schedule(row), due, now)
            with self._db:
                if next_at is None:
                    self._db.execute("""UPDATE jobs SET status='completed', next_at=NULL, last_outcome='expired'
                                        WHERE id=?""", (job_id,))
                else:
                    self._db.execute("UPDATE jobs SET status='active', next_at=? WHERE id=?", (_stamp(next_at), job_id))
            return _job(self._get(job_id))

    def remove(self, job_id, *, confirmed=False) -> Job:
        """Delete the job and any pending runs. Does not retract an already claimed run."""
        _approval(confirmed)
        with self._lock:
            row = self._get(job_id)
            with self._db:
                self._db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            return _job(row)

    def run_now(self, job_id, *, confirmed=False, now=None) -> Job:
        """Queue one extra run durably without changing the normal schedule."""
        _approval(confirmed)
        at = _stamp(_now(now))
        with self._lock:
            row = self._get(job_id)
            if row['status'] == 'paused' or row['manual_at'] is not None or row['run_id'] is not None:
                raise RuntimeError('job is paused, queued or running')
            with self._db:
                self._db.execute('UPDATE jobs SET manual_at=? WHERE id=?', (at, job_id))
            return _job(self._get(job_id))

    def tick(self, *, deliver, prompt=None, allow_prompt=True,
             prompt_slot: Callable[[], ContextManager[bool]] | None = None, now=None) -> RunResult | None:
        """Attempt at most one due run. Call off the polling thread.

        deliver(run, text) must raise on failure and not retry uncertain sends.
        Prompt work is deferred unclaimed if no adapter or no model slot is
        available. A slot factory is entered only for a due prompt, under the
        state lock before claiming; it must not block or reenter the scheduler.
        None means idle, busy, or closed. A claim commits before I/O:
        after a crash the run can be lost, but is never replayed automatically.
        """
        _callbacks(deliver, prompt)
        with ExitStack() as slot:
            return self._tick(deliver, prompt if allow_prompt is True else None, now, prompt_slot, slot)

    def _tick(self, deliver, prompt, now, prompt_slot, slot):
        now = _now(now)
        at = _stamp(now)
        with self._lock:
            if self._closed or self._busy:
                return None
            def next_due(include_prompts):
                return self._db.execute('''SELECT *, CASE WHEN manual_at IS NULL THEN next_at
                                          WHEN next_at IS NULL THEN manual_at ELSE min(next_at, manual_at) END AS due_at
                                      FROM jobs WHERE run_id IS NULL AND status != 'paused' AND (kind='reminder' OR ?)
                                      AND (next_at <= ? OR manual_at <= ?)
                                      ORDER BY due_at, id LIMIT 1''', (include_prompts, at, at)).fetchone()

            row = next_due(prompt is not None)
            if row is not None and row['kind'] == 'prompt' and prompt_slot is not None:
                if slot.enter_context(prompt_slot()) is not True:
                    row = next_due(False)
            if row is None:
                return None
            manual = row['due_at'] == row['manual_at'] and row['due_at'] != row['next_at']
            if manual:
                due, next_at = _datetime(row['manual_at']), _optional_time(row['next_at'])
            else:
                due, next_at = _catch_up(_schedule(row), _datetime(row['next_at']), now)
            run = Run(uuid4().hex, row['id'], row['kind'], 'manual' if manual else 'scheduled', due)
            expired = now - due > STALE_AFTER
            # Commit the claim before calling anything outside the store.
            with self._db:
                self._db.execute('''UPDATE jobs SET next_at=?, manual_at=?, status=?, run_id=?, last_outcome=? WHERE id=?''',
                                 (_stamp(next_at) if next_at is not None else None, None if manual else row['manual_at'],
                                  'active' if next_at is not None else 'completed',
                                  None if expired else run.id, 'expired' if expired else 'claimed', run.job_id))
            if expired:
                return RunResult(run, 'expired')
            self._busy = True
        try:
            outcome = self._execute(run, row['text'], deliver, prompt)
            with self._lock:
                if not self._closed:
                    with self._db:
                        self._db.execute('UPDATE jobs SET run_id=NULL, last_outcome=? WHERE id=? AND run_id=?',
                                         (outcome, run.job_id, run.id))
            return RunResult(run, outcome)
        finally:
            with self._lock:
                self._busy = False

    def _execute(self, run, text, deliver, prompt):
        if self._stop.is_set():
            return 'interrupted'
        if run.kind == 'prompt':
            if prompt is None:
                return 'prompt_failed'
            try:
                text = _text(prompt.callback(run, text, read_only=True, allow_execute=False, allow_write=False))
            except Exception:
                return 'prompt_failed'
        # Crossing this commit point authorizes one send. close() stops late
        # results, but cannot retract a send that crossed it before shutdown.
        if self._stop.is_set():
            return 'interrupted'
        try:
            deliver(run, text)
        except Exception:
            return 'delivery_failed'
        return 'sent'

    def start_worker(self, *, deliver, prompt=None, prompt_slot: Callable[[], ContextManager[bool]] | None = None,
                     interval=1.0, clock=None) -> threading.Thread:
        """Start a single daemon worker and return its Thread; close() signals it to stop.

        prompt_slot(), if supplied, is a nonblocking context manager yielding
        True only if it reserved the parent's model slot. Used only before a
        prompt claim; a busy slot merely defers prompts, not reminders. When
        omitted, the parent asserts the prompt adapter need not share a slot.
        Slow callbacks occupy the single worker, not the bot's polling thread.
        A state/integration error stops the worker; the parent can monitor the
        returned thread's liveness. Ordinary callback failures are recorded.
        """
        _callbacks(deliver, prompt)
        if (type(interval) not in (int, float) or not math.isfinite(interval) or not 0.01 <= interval <= 60
                or (clock is not None and not callable(clock))
                or (prompt_slot is not None and (prompt is None or not callable(prompt_slot)))):
            raise ValueError('invalid worker configuration')

        def work():
            while not self._stop.is_set():
                try:
                    now = clock() if clock is not None else None
                    self.tick(deliver=deliver, prompt=prompt, prompt_slot=prompt_slot, now=now)
                except Exception:
                    # Do not expose callback data or state paths in logs.
                    LOG.error('scheduler worker stopped after a state or integration error')
                    return
                self._stop.wait(interval)

        with self._lock:
            self._check_open()
            if self._worker is not None and self._worker.is_alive():
                raise RuntimeError('scheduler worker already running')
            thread = threading.Thread(target=work, name='minis-scheduler', daemon=True)
            thread.start()
            self._worker = thread
            return thread
