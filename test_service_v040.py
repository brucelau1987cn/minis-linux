import fcntl
import select
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from unittest.mock import patch

from minis import read_history
from minis_schedule import Scheduler
from minis_telegram import Bot, TaskControl, Telegram, TelegramError
from minis_transport import MediaResult
from test_telegram import FakeProvider, FakeTelegram, message


class ServiceV040Test(unittest.TestCase):
    def test_successful_web_reply_reports_model_without_saving_history(self):
        from minis_web import _WebContext

        class WebFixture:
            model = 'fixture-model'

            def reply(self, _messages):
                started.set()
                if not release.wait(3):
                    raise TimeoutError('fixture not released')
                return {'content': '网页摘要'}

        started, release = threading.Event(), threading.Event()
        self.bot.provider = WebFixture()
        with patch('minis_telegram.prepare_web', return_value=_WebContext(
                'fixture page', [(1, 'https://example.org/page')])):
            try:
                self.tasks.handle('打开 https://example.org/page')
                self.assertTrue(started.wait(1))
                active = self.tasks.active
                assert active is not None and active.worker is not None
                worker = active.worker
            finally:
                release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertIn('本轮模型：fixture-model', self.api.sent[-1][1])
        self.tasks.handle('/status')
        self.assertIn('已完成', self.api.sent[-1][1])
        self.assertEqual(read_history(self.bot.history), [])

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.api = FakeTelegram([])
        self.bot = Bot(Path(tmp.name), 123, '1000', self.api,
                       FakeProvider({'content': '普通回复'}), features=True)
        self.tasks = TaskControl(self.bot)
        self.addCleanup(self.tasks.close)

    def feed(self, *updates):
        self.api.updates = list(updates)
        self.bot.poll_once(self.tasks.handle, dispatch_media=self.tasks.handle_media)

    def test_attachment_authorization_and_caption_is_not_a_command(self):
        document = {'file_id': 'fixture', 'file_name': 'notes.txt'}
        release, started = threading.Event(), threading.Event()
        def media(_telegram, raw, **_):
            self.assertEqual(raw['caption'], '/new')
            started.set()
            release.wait(2)
            return MediaResult('voice', reply='本机转写结果')
        with patch('minis_telegram.dispatch_media', side_effect=media) as adapter:
            try:
                self.feed(message(1, '', owner=444, document=document, caption='/new'),
                          message(2, '', document=document, caption='/new'))
                self.assertTrue(started.wait(1))
                active = self.tasks.active
                assert active is not None and active.worker is not None
                worker = active.worker
            finally:
                release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            adapter.assert_called_once()
        self.assertEqual(self.api.sent[-1], (123, '本机转写结果'))
        assert self.bot.sessions is not None
        self.assertEqual(len(self.bot.sessions.list()), 1)
        self.assertEqual(read_history(self.bot.history), [])

    def test_workspace_attachment_sends_only_previewed_bytes_once_to_owner(self):
        import re

        path = self.bot.workspace / 'report.txt'
        path.write_text('previewed', encoding='utf-8')
        with patch('minis_telegram.send_document') as send:
            self.tasks.handle('/send report.txt')
            preview = self.api.sent[-1][1]
            match = re.search(r'/approve ([0-9a-f]{32})', preview)
            self.assertIsNotNone(match, preview)
            assert match is not None
            send.assert_not_called()
            path.write_text('changed', encoding='utf-8')
            command = '/approve ' + match[1]
            self.feed(message(10, command, owner=999), message(11, command + ' '))
            send.assert_not_called()
            self.feed(message(12, command), message(13, command))
            send.assert_called_once_with(self.api, 123, 'report.txt', b'previewed')

    def test_busy_service_defers_scheduled_model(self):
        with self.tasks.prompt_slot() as reserved:
            self.assertTrue(reserved)
            self.tasks.handle('交互任务')
            self.assertIn('当前忙', self.api.sent[-1][1])
        with self.tasks.prompt_slot() as reserved:
            self.assertTrue(reserved)

    def test_stop_after_stream_preview_never_commits_or_sends_late_result(self):
        class StreamingFixture:
            def reply(self, _messages, *, on_delta, checkpoint):
                on_delta('首段预览')
                started.set()
                if not release.wait(3):
                    raise TimeoutError('test did not release model')
                checkpoint('receive more')
                on_delta('late-fixture-delta')
                return {'content': 'late-fixture-final'}

        started, release = threading.Event(), threading.Event()
        api = Telegram('1000:' + 'x' * 32)
        bot = Bot(self.bot.home, 123, '1000', api, StreamingFixture(), features=True)
        tasks = TaskControl(bot)
        self.addCleanup(tasks.close)
        with patch.object(api, 'request', return_value={'message_id': 123}) as request:
            try:
                tasks.handle('普通聊天')
                self.assertTrue(started.wait(1))
                active = tasks.active
                assert active is not None and active.worker is not None
                worker = active.worker
                tasks.handle('/stop')
            finally:
                release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            shown = [call.args[1]['text'] for call in request.call_args_list]
        self.assertIn('首段预览', shown)
        self.assertTrue(any('已停止本地任务' in part for part in shown))
        self.assertFalse(any('late-fixture-' in part for part in shown))
        self.assertEqual(read_history(bot.history), [])

    def test_preview_429_defers_only_the_unsent_final_suffix(self):
        class StreamingFixture:
            def reply(self, _messages, *, on_delta, checkpoint):
                on_delta(prefix)
                now[0] += 1
                on_delta(suffix)
                return {'content': prefix + suffix}

        prefix, suffix = '前缀', ' 🙂后续'
        now = [100.0]
        sent = []
        api = Telegram('1000:' + 'x' * 32)
        bot = Bot(self.bot.home, 123, '1000', api, StreamingFixture(), features=True)
        tasks = TaskControl(bot)

        def request(method, body, timeout=15):
            sent.append((method, body['text'], now[0]))
            if method == 'editMessageText':
                raise TelegramError(method, 429, 2)
            return {'message_id': 123}

        def finish_cooldown(delay):
            self.assertEqual(read_history(bot.history)[-1]['content'], prefix + suffix)
            now[0] += delay
            return False

        # Run the model worker inline; a fixture advances the cooldown only
        # when the worker waits, after the authoritative history is committed.
        with patch.object(api, 'request', side_effect=request), \
                patch('minis_telegram.time.monotonic', side_effect=lambda: now[0]), \
                patch('minis_telegram.threading.Thread', side_effect=InlineThread), \
                patch.object(tasks._shutdown, 'wait', side_effect=finish_cooldown):
            tasks.handle('聊天')
        self.assertEqual(sent[1:], [
            ('sendMessage', prefix, 100), ('editMessageText', prefix + suffix, 101),
            ('sendMessage', suffix, 103),
        ])
        self.assertEqual(tasks.last, '已完成')

    def run_chunk_fixture(self, reject, wait):
        now = [100.0]
        chunks = ['A' * 4000, '🙂' * 2000, 'last chunk']
        api = Telegram('1000:' + 'x' * 32)
        bot = Bot(self.bot.home, 123, '1000', api, FakeProvider({'content': ''.join(chunks)}), features=True)
        tasks = TaskControl(bot)
        attempts, waits = [], []

        def request(method, body, timeout=15):
            if not body['text'].startswith('已收到'):
                attempts.append(body['text'])
                if body['text'] == chunks[1]:
                    reject(method, attempts.count(chunks[1]))
            return {'message_id': 1}

        def during_wait(delay):
            waits.append(delay)
            now[0] += delay
            return wait(tasks)

        with patch.object(api, 'request', side_effect=request), \
                patch('minis_telegram.time.monotonic', side_effect=lambda: now[0]), \
                patch('minis_telegram.threading.Thread', side_effect=InlineThread), \
                patch.object(tasks._shutdown, 'wait', side_effect=during_wait):
            tasks.handle('聊天')
        self.assertEqual(read_history(bot.history)[-1]['content'], ''.join(chunks))
        return tasks, chunks, attempts, waits

    def test_confirmed_429_in_middle_retries_just_that_chunk(self):
        def reject(method, count):
            if count == 1:
                raise TelegramError(method, 429, 2)

        tasks, (first, second, last), attempts, waits = self.run_chunk_fixture(reject, lambda _: False)
        self.assertEqual(attempts, [first, second, second, last])
        self.assertEqual(waits, [2])
        self.assertEqual(tasks.last, '已完成')

    def test_unconfirmed_middle_chunk_is_not_retried_or_followed(self):
        def reject(method, _):
            raise TelegramError(method, 502)

        tasks, chunks, attempts, waits = self.run_chunk_fixture(reject, lambda _: False)
        self.assertEqual(attempts, chunks[:2])
        self.assertEqual(waits, [])
        self.assertIn('回复发送状态不明', tasks.last or '')

    def test_excessive_cooldown_fails_without_waiting(self):
        def reject(method, _):
            raise TelegramError(method, 429, 60)

        tasks, chunks, attempts, waits = self.run_chunk_fixture(reject, lambda _: False)
        self.assertEqual(attempts, chunks[:2])
        self.assertEqual(waits, [])
        self.assertIn('回复发送失败', tasks.last or '')

    def test_repeated_short_rejections_cannot_defer_indefinitely(self):
        def reject(method, _):
            raise TelegramError(method, 429, 1)

        tasks, (first, second, _), attempts, waits = self.run_chunk_fixture(reject, lambda _: False)
        self.assertEqual(attempts, [first] + [second] * 4)
        self.assertEqual(waits, [1, 1, 1])
        self.assertIn('回复发送失败', tasks.last or '')

    def test_close_interrupts_cooldown_without_sending_pending_chunks(self):
        def reject(method, _):
            raise TelegramError(method, 429, 2)

        def close(tasks):
            tasks.close()
            return tasks._shutdown.is_set()

        tasks, chunks, attempts, waits = self.run_chunk_fixture(reject, close)
        self.assertEqual(attempts, chunks[:2])
        self.assertEqual(waits, [2])
        self.assertIn('回复发送失败', tasks.last or '')

    def test_sigterm_interrupts_poll_but_waits_for_committed_history_under_lease(self):
        # Exercise actual main/serve/signal handling in a disposable process.
        # All network I/O is replaced; DB and history writes are real.
        script = dedent('''
            import json
            import sys
            import threading
            from datetime import datetime, timedelta, timezone
            from unittest.mock import Mock, patch
            from minis import save_history
            import minis_telegram as service
            from test_delivery_v040 import Response, accepted
            from test_telegram import FakeProvider, message

            polling = threading.Event()
            reminder_sending = threading.Event()
            telegram = service.Telegram('2000:' + 'x' * 32)

            def open_(req, **_):
                method = req.full_url.rsplit('/', 1)[-1]
                body = json.loads(req.data)
                if method == 'getUpdates':
                    if body['offset'] == 0:
                        return Response(req, {'ok': True, 'result': [message(1, 'fixture turn')]})
                    polling.set()
                    threading.Event().wait(30)
                    raise AssertionError('poll should be interrupted by SIGTERM')
                if method == 'sendMessage' and body['text'].startswith('已收到'):
                    return Response(req, accepted())
                if method == 'sendMessage' and body['text'].startswith('提醒（'):
                    reminder_sending.set()
                    threading.Event().wait(30)
                raise AssertionError('unexpected request')

            def slow_save(path, history):
                if path.name.endswith('.cursor.json'):
                    return save_history(path, history)
                assert polling.wait(3)
                assert reminder_sending.wait(3)
                print('committing while polling', flush=True)
                assert sys.stdin.readline() == 'commit\\n'
                save_history(path, history)

            def close_jobs(jobs):
                original_close(jobs)
                print('scheduler closed', flush=True)

            def start_jobs(jobs):
                now = datetime.now(timezone.utc) - timedelta(minutes=2)
                proposal = jobs.scheduler.propose('in 1 minute remind me: fixture', now=now)
                jobs.scheduler.create(proposal, confirmed=True, now=now)
                return original_start(jobs)

            original_close = service.JobUI.close
            original_start = service.JobUI.start
            telegram.opener.open = open_
            local_os = Mock(wraps=service.os)
            local_os.environ = {}
            local_os.geteuid.return_value = 65534
            with (patch.object(service, 'config', return_value=(123, telegram, FakeProvider({'content': 'saved answer'}))),
                  patch.object(service, 'notify_ready'), patch.object(service, 'os', local_os),
                  patch.object(service.JobUI, 'start', start_jobs),
                  patch.object(service, 'save_history', slow_save), patch.object(service.JobUI, 'close', close_jobs)):
                sys.exit(service.main())
        ''')
        with subprocess.Popen([sys.executable, '-EsB', '-c', script, '--home', str(self.bot.home)],
                              cwd=Path(__file__).resolve().parent, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            output = proc.stdout
            assert output is not None and proc.stderr is not None
            try:
                def line():
                    self.assertTrue(select.select([output], [], [], 4)[0], 'missing lifecycle marker')
                    result = output.readline()
                    if not result:
                        self.fail(f'child exited: {proc.communicate(timeout=2)[1].decode()}')
                    return result

                self.assertEqual(line(), b'committing while polling\n')
                proc.terminate()
                self.assertEqual(line(), b'scheduler closed\n')
                with (self.bot.home / 'telegram.lock').open('rb') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # The scheduler has released its own DB lease, even
                    # though a committed history write still holds the service lease.
                    db, = (self.bot.home / 'telegram').glob('bot-2000-owner-123.jobs.sqlite3')
                    with Scheduler(db) as reopened:
                        self.assertEqual(reopened.list()[0].last_outcome, 'interrupted')
                        self.assertIsNone(reopened.tick(deliver=lambda *_: self.fail('reminder replayed')))
                    _, stderr = proc.communicate(b'commit\n', timeout=4)
                    self.assertEqual(proc.returncode, 0, stderr.decode())
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertEqual(read_history(db.with_name('bot-2000-owner-123.json'))[-1]['content'],
                                     'saved answer')
            finally:
                if proc.poll() is None:
                    proc.kill()


class InlineThread:
    def __init__(self, *, target, args, **_):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)
