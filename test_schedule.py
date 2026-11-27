import os
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from minis_schedule import MAX_JOBS, PromptRunner, Scheduler


UTC = timezone.utc
BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'owner.sqlite'
        self.scheduler = Scheduler(self.path)
        self.addCleanup(self.scheduler.close)
        self.sent = []

    def deliver(self, run, text):
        self.sent.append((run, text))

    def test_preview_confirm_restart_and_deliver_one_shot_once(self):
        proposal = self.scheduler.propose('10分钟后提醒我喝水', now=BASE)
        self.assertEqual(proposal.schedule.first_at, BASE + timedelta(minutes=10))
        self.assertEqual((proposal.kind, proposal.text), ('reminder', '喝水'))
        self.assertEqual(self.scheduler.list(), [])
        with self.assertRaises(PermissionError):
            self.scheduler.create(proposal, now=BASE)
        job = self.scheduler.create(proposal, confirmed=True, now=BASE)
        self.scheduler.close()
        self.scheduler = Scheduler(self.path)
        self.addCleanup(self.scheduler.close)
        self.assertEqual(self.scheduler.list(), [job])
        self.assertIsNone(self.scheduler.tick(deliver=self.deliver, now=BASE))
        result = self.scheduler.tick(deliver=self.deliver, now=proposal.schedule.first_at)
        assert result is not None
        self.assertEqual(result.outcome, 'sent')
        self.assertEqual(self.sent, [(result.run, '喝水')])
        self.assertEqual(self.scheduler.list()[0].status, 'completed')
        self.assertIsNone(self.scheduler.tick(deliver=self.deliver, now=proposal.schedule.first_at))

    def create(self, expression, **options):
        proposal = self.scheduler.propose(expression, now=BASE, **options)
        return self.scheduler.create(proposal, confirmed=True, now=BASE)

    def test_restart_coalesces_intervals_to_latest_due(self):
        job = self.create('每隔10分钟提醒我喝水')
        self.scheduler.close()
        with Scheduler(self.path) as restarted:
            now = BASE + timedelta(days=8, minutes=35)
            result = restarted.tick(deliver=self.deliver, now=now)
            assert result is not None
            self.assertEqual(result.run.scheduled_at, now - timedelta(minutes=5))
            self.assertEqual(restarted.list()[0].id, job.id)
            self.assertEqual(restarted.list()[0].next_at, now + timedelta(minutes=5))
            self.assertIsNone(restarted.tick(deliver=self.deliver, now=now))

    def test_expired_one_shot_is_not_sent(self):
        self.create('in 2 hours remind me: check the oven')
        result = self.scheduler.tick(deliver=self.deliver, now=BASE + timedelta(days=2))
        assert result is not None
        self.assertEqual(result.outcome, 'expired')
        self.assertEqual(self.scheduler.list()[0].status, 'completed')
        self.assertEqual(self.sent, [])

    def test_explicit_iso_timezone_is_not_host_local_time(self):
        job = self.create('2026-01-06T08:30:05+05:30 提醒我：打电话')
        self.assertEqual(job.next_at, BASE + timedelta(days=1, hours=3, seconds=5))
        self.assertEqual(job.proposal.text, '打电话')

    def test_rejects_ambiguous_unbounded_and_unvetted_expressions(self):
        for expression in ('2026-01-06T08:30 提醒我喝水', '每天25:00 提醒我喝水', '周一提醒我喝水',
                           'every 0 minutes remind me: hi', 'every 10001 hours remind me: hi',
                           'every 2 hours execute echo hi', '10分钟后提醒我\0hi'):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.scheduler.propose(expression, now=BASE)

    def test_weekly_wall_time_survives_restart_and_dst_fold_runs_once(self):
        now = datetime(2026, 10, 30, 0, tzinfo=UTC)
        proposal = self.scheduler.propose('每周日凌晨1点30分提醒我喝水', now=now, tz='America/New_York')
        job = self.scheduler.create(proposal, confirmed=True, now=now)
        due = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        self.assertEqual(job.next_at, due)
        self.scheduler.close()
        with Scheduler(self.path) as restarted:
            result = restarted.tick(deliver=self.deliver, now=due)
            assert result is not None
            self.assertEqual(result.run.scheduled_at, due)
            self.assertIsNone(restarted.tick(deliver=self.deliver, now=due + timedelta(hours=1)))
            self.assertEqual(restarted.list()[0].next_at, due + timedelta(days=7, hours=1))

    def test_daily_wall_time_skips_spring_gap(self):
        now = datetime(2026, 3, 7, 12, tzinfo=UTC)
        proposal = self.scheduler.propose('每天02:30 提醒我喝水', now=now, tz='America/New_York')
        self.assertEqual(proposal.schedule.first_at, datetime(2026, 3, 9, 6, 30, tzinfo=UTC))

    def test_chinese_period_and_english_calendar_syntax(self):
        a = self.scheduler.propose('每天早上9点提醒我喝水', now=BASE, tz='Asia/Shanghai')
        b = self.scheduler.propose('daily at 09:00 remind me: 喝水', now=BASE, tz='Asia/Shanghai')
        self.assertEqual(a, b)
        self.assertEqual(a.schedule.first_at, BASE + timedelta(hours=1))

    def test_manage_jobs_and_bound_manual_queue(self):
        job = self.create('every 2 hours remind me: check the oven')
        with self.assertRaises(PermissionError):
            self.scheduler.run_now(job.id, now=BASE)
        queued = self.scheduler.run_now(job.id, confirmed=True, now=BASE)
        self.assertEqual(queued.manual_at, BASE)
        with self.assertRaises(RuntimeError):
            self.scheduler.run_now(job.id, confirmed=True, now=BASE)
        self.assertEqual(self.scheduler.pause(job.id, confirmed=True).status, 'paused')
        self.assertIsNone(self.scheduler.tick(deliver=self.deliver, now=BASE + timedelta(hours=5)))
        resumed = self.scheduler.resume(job.id, confirmed=True, now=BASE + timedelta(hours=5))
        self.assertEqual(resumed.next_at, BASE + timedelta(hours=6))
        self.assertIsNone(resumed.manual_at)
        self.scheduler.remove(job.id, confirmed=True)
        with self.assertRaises(KeyError):
            self.scheduler.run_now(job.id, confirmed=True, now=BASE)

    def test_run_now_survives_restart_without_changing_normal_due_time(self):
        job = self.create('2小时后提醒我休息')
        self.scheduler.run_now(job.id, confirmed=True, now=BASE)
        self.scheduler.close()
        with Scheduler(self.path) as restarted:
            result = restarted.tick(deliver=self.deliver, now=BASE)
            assert result is not None
            self.assertEqual(result.run.trigger, 'manual')
            self.assertEqual(restarted.list()[0].next_at, job.next_at)
            self.assertIsNone(restarted.tick(deliver=self.deliver, now=BASE))

    def test_delivery_failure_is_not_retried(self):
        job = self.create('1分钟后提醒我喝水')
        def uncertain(run, text):
            self.deliver(run, text)
            raise TimeoutError('could have been delivered')
        result = self.scheduler.tick(deliver=uncertain, now=job.next_at)
        assert result is not None
        self.assertEqual(result.outcome, 'delivery_failed')
        self.scheduler.close()
        with Scheduler(self.path) as restarted:
            self.assertEqual(restarted.list()[0].last_outcome, 'delivery_failed')
            self.assertIsNone(restarted.tick(deliver=self.deliver, now=job.next_at))
        self.assertEqual(len(self.sent), 1)

    def test_claim_is_visible_and_not_replayed_after_abrupt_exit(self):
        job = self.create('每1分钟提醒我喝水')
        self.scheduler.close()
        pid = os.fork()
        if pid == 0:
            # No Python cleanup, SQLite rollback/close, or explicit lease release.
            try:
                crash = Scheduler(self.path)
                crash.tick(deliver=lambda *_: os._exit(23 if crash.list()[0].last_outcome == 'claimed' else 24),
                           now=job.next_at)
            finally:
                os._exit(25)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        with Scheduler(self.path) as restarted:
            self.assertEqual(restarted.list()[0].last_outcome, 'interrupted')
            self.assertIsNone(restarted.tick(deliver=self.deliver, now=job.next_at))
            self.assertIsNotNone(restarted.tick(deliver=self.deliver, now=BASE + timedelta(minutes=2)))

    def test_prompt_policy_is_explicit_and_busy_defers_without_claiming(self):
        job = self.create('every 1 minute read-only prompt: give me a short idea')
        reminder = self.create('2分钟后提醒我喝水')
        args = []
        def generate(run, prompt, **flags):
            args.append((run, prompt, flags))
            return 'idea'
        with self.assertRaises(ValueError):
            PromptRunner(generate, read_only=True, allow_execute=True, allow_write=False)
        prompt = PromptRunner(generate, read_only=True, allow_execute=False, allow_write=False)
        result = self.scheduler.tick(deliver=self.deliver, prompt=prompt, allow_prompt=False, now=reminder.next_at)
        assert result is not None
        self.assertEqual(result.run.job_id, reminder.id)
        self.assertEqual(self.scheduler.list()[0], job)
        result = self.scheduler.tick(deliver=self.deliver, prompt=prompt, now=reminder.next_at)
        assert result is not None
        self.assertEqual(args, [(result.run, job.proposal.text,
                                 {'read_only': True, 'allow_execute': False, 'allow_write': False})])
        self.assertEqual(self.sent[-1], (result.run, 'idea'))

    def test_worker_does_not_block_management_or_send_late_prompt_on_close(self):
        job = self.create('1分钟后只读提问：给我一个点子')
        entered, release = threading.Event(), threading.Event()
        def generate(*args, **kwargs):
            entered.set()
            release.wait(3)
            return 'late result'
        @contextmanager
        def slot():
            yield True
        thread = self.scheduler.start_worker(deliver=self.deliver,
                                             prompt=PromptRunner(generate, True, False, False),
                                             prompt_slot=slot, clock=lambda: job.next_at)
        try:
            self.assertTrue(entered.wait(2))
            self.assertIsNone(self.scheduler.tick(deliver=self.deliver, now=job.next_at))
            self.assertEqual(self.scheduler.list()[0].last_outcome, 'claimed')
            with self.assertRaises(BlockingIOError):
                Scheduler(self.path)
            self.scheduler.close()
            with Scheduler(self.path) as restarted:
                self.assertEqual(restarted.list()[0].last_outcome, 'interrupted')
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.sent, [])

    def test_worker_busy_slot_still_delivers_reminder(self):
        job = self.create('1分钟后只读提问：给我一个点子')
        reminder = self.create('2分钟后提醒我喝水')
        delivered = threading.Event()

        def deliver(run, text):
            self.deliver(run, text)
            delivered.set()

        @contextmanager
        def unavailable():
            yield False

        thread = self.scheduler.start_worker(deliver=deliver, prompt=PromptRunner(lambda *a, **k: 'bad', True, False, False),
                                             prompt_slot=unavailable, clock=lambda: reminder.next_at)
        try:
            self.assertTrue(delivered.wait(2))
            self.assertEqual([(run.job_id, text) for run, text in self.sent], [(reminder.id, '喝水')])
            self.assertEqual(self.scheduler.list()[0], job)
        finally:
            self.scheduler.close()
            thread.join(2)

    def test_prompt_failure_does_not_deliver_or_retry(self):
        job = self.create('1分钟后只读提问：给我一个点子')

        def generate(*args, **kwargs):
            raise TimeoutError('upstream')

        prompt = PromptRunner(generate, True, False, False)
        result = self.scheduler.tick(deliver=self.deliver, prompt=prompt, now=job.next_at)
        assert result is not None
        self.assertEqual(result.outcome, 'prompt_failed')
        self.assertEqual(self.scheduler.list()[0].last_outcome, 'prompt_failed')
        self.assertIsNone(self.scheduler.tick(deliver=self.deliver, prompt=prompt, now=job.next_at))
        self.assertEqual(self.sent, [])

    def test_state_scope_permissions_and_job_limit(self):
        proposal = self.scheduler.propose('1分钟后提醒我喝水', now=BASE)
        for _ in range(MAX_JOBS):
            self.scheduler.create(proposal, confirmed=True, now=BASE)
        with self.assertRaises(OverflowError):
            self.scheduler.create(proposal, confirmed=True, now=BASE)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with Scheduler(self.path.with_name('other-owner.sqlite')) as other:
            self.assertEqual(other.list(), [])
            with self.assertRaises(KeyError):
                other.remove(self.scheduler.list()[0].id, confirmed=True)
