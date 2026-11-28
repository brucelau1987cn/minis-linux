"""Exercise Bot API acknowledgements and service shutdown without live I/O."""
from email.message import Message
import fcntl
import io
import json
from pathlib import Path
import select
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from minis import read_history
from minis_schedule import Scheduler
from minis_telegram import Bot, TaskControl, Telegram, TelegramError
from test_service_v040 import InlineThread
from test_telegram import FakeProvider, message


class Response(io.BytesIO):
    def __init__(self, request, payload):
        super().__init__(json.dumps(payload).encode())
        self.url = request.full_url
        self.headers = {}

    def geturl(self):
        return self.url


def accepted(message_id: object = 1):
    return {'ok': True, 'result': {'message_id': message_id}}


class DeliveryV040Test(unittest.TestCase):
    def setUp(self):
        home = TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.api = Telegram('2000:' + 'x' * 32)
        self.bot = Bot(Path(home.name), 123, '2000', self.api, FakeProvider(), features=True)
        self.tasks = TaskControl(self.bot)
        self.addCleanup(self.tasks.close)

    def test_send_message_requires_a_positive_integer_id(self):
        for payload in ({'ok': True}, {'ok': True, 'result': []}, {'ok': True, 'result': {}},
                        *(accepted(value) for value in (None, True, 0, -1, '3', 3.0))):
            with self.subTest(payload=payload), patch.object(
                    self.api.opener, 'open', side_effect=lambda req, **_: Response(req, payload)) as open_:
                with self.assertRaises(TelegramError) as caught:
                    self.api.send(123, 'fixture')
                self.assertIsNone(caught.exception.code)
                open_.assert_called_once()
        with patch.object(self.api.opener, 'open', side_effect=lambda req, **_: Response(req, accepted(3))):
            self.assertEqual(self.api.send(123, 'fixture'), 3)

    def test_unconfirmed_preview_cannot_arm_even_if_approval_is_fetched_in_same_batch(self):
        (self.bot.workspace / 'notes.txt').write_text('fixture', encoding='utf-8')
        preview = self.bot.local_command('/send notes.txt')
        _, ident, _ = preview.receipt
        sent = []

        def open_(req, **_):
            method = req.full_url.rsplit('/', 1)[-1]
            if method == 'getUpdates':
                return Response(req, {'ok': True, 'result': [message(1, '/preview ' + ident),
                                                               message(2, '/approve ' + ident)]})
            self.assertEqual(method, 'sendMessage')
            sent.append(json.loads(req.data)['text'])
            return Response(req, {'ok': True, 'result': []} if len(sent) == 1 else accepted())

        with patch.object(self.api.opener, 'open', side_effect=open_), patch('minis_telegram.send_document') as send:
            with self.assertRaises(TelegramError):
                self.bot.poll_once(self.tasks.handle)
        send.assert_not_called()
        self.assertEqual(self.bot.offset(), 3)
        self.assertEqual(len(sent), 2)
        self.assertIn('未确认', sent[-1])

    def run_final_chunks(self, first_middle_reply):
        chunks = ['a' * 4000, '🙂' * 2000, 'last chunk']
        text = ''.join(chunks)
        self.bot.provider = FakeProvider({'content': text})
        now, attempts, waits = [100.0], [], []

        def open_(req, **_):
            self.assertTrue(req.full_url.endswith('/sendMessage'))
            part = json.loads(req.data)['text']
            if not part.startswith('已收到'):
                attempts.append(part)
                if part == chunks[1] and attempts.count(part) == 1:
                    return first_middle_reply(req)
            return Response(req, accepted())

        def wait(seconds):
            waits.append(seconds)
            now[0] += seconds
            return False

        with (patch.object(self.api.opener, 'open', side_effect=open_),
              patch('minis_telegram.threading.Thread', side_effect=InlineThread),
              patch('minis_telegram.time.monotonic', side_effect=lambda: now[0]),
              patch.object(self.tasks._shutdown, 'wait', side_effect=wait)):
            self.tasks.handle('fixture turn')
        self.assertEqual(read_history(self.bot.history)[-1]['content'], text)
        return chunks, attempts, waits

    def test_ambiguous_middle_chunk_is_neither_replayed_nor_followed(self):
        chunks, attempts, waits = self.run_final_chunks(lambda req: Response(req, {'ok': True, 'result': []}))
        self.assertEqual(attempts, chunks[:2])
        self.assertEqual(waits, [])
        self.assertEqual(self.tasks.last, '回复发送状态不明')

    def test_contradictory_success_envelope_does_not_trigger_rate_limit_retry(self):
        # A contradictory error_code must not turn an unconfirmed write into
        # a known-unsent rate-limited chunk.
        chunks, attempts, waits = self.run_final_chunks(lambda req: Response(req, {
            'ok': True, 'error_code': 429, 'parameters': {'retry_after': 2},
        }))
        self.assertEqual(attempts, chunks[:2])
        self.assertEqual(waits, [])
        self.assertEqual(self.tasks.last, '回复发送状态不明')

    def test_verified_telegram_429_retries_only_the_known_unsent_chunk(self):
        chunks, attempts, waits = self.run_final_chunks(lambda req: Response(req, {
            'ok': False, 'error_code': 429, 'parameters': {'retry_after': 2},
        }))
        self.assertEqual(attempts, chunks[:2] + chunks[1:])
        self.assertEqual(waits, [2])
        self.assertEqual(self.tasks.last, '已完成')

    def test_http_429_without_a_telegram_envelope_is_still_known_unsent(self):
        def rate_limit(req):
            raise HTTPError(req.full_url, 429, '', Message(), io.BytesIO(b''))

        chunks, attempts, waits = self.run_final_chunks(rate_limit)
        self.assertEqual(attempts, chunks[:2] + chunks[1:])
        self.assertEqual(waits, [1])
        self.assertEqual(self.tasks.last, '已完成')

    def test_unconfirmed_document_is_audited_unknown_and_not_replayed(self):
        (self.bot.workspace / 'notes.txt').write_text('fixture', encoding='utf-8')
        proposal = self.bot.local_command('/send notes.txt')
        path, ident, _ = proposal.receipt
        command = '/approve ' + ident

        def open_(req, **_):
            if req.full_url.endswith('/sendMessage'):
                return Response(req, accepted())
            self.assertTrue(req.full_url.endswith('/sendDocument'))
            return Response(req, accepted(True))

        with patch.object(self.api.opener, 'open', side_effect=open_) as request:
            self.bot.send_reply(proposal)
            self.assertIn('无法确认', self.bot.local_command(command))
            self.assertIn('未确认', self.bot.local_command(command))
            self.assertEqual(request.call_count, 2)
        self.assertEqual([entry['status'] for entry in self.bot._queue(path).audit()], ['unknown', 'started'])

    def test_sigterm_unwinds_approved_document_open_and_read_and_releases_leases(self):
        self.check_document_shutdown('open', 'unknown')
        self.check_document_shutdown('read', 'unknown')

    def test_sigterm_allows_approved_document_audit_write_to_finish(self):
        self.check_document_shutdown('audit', 'applied')

    def check_document_shutdown(self, phase, expected_status):
        script = dedent('''
            import json
            import os
            import re
            import sys
            import threading
            from unittest.mock import Mock, patch
            from minis_actions import ActionQueue
            import minis_telegram as service
            from test_delivery_v040 import Response, accepted
            from test_telegram import FakeProvider, message

            phase = sys.argv.pop(1)
            telegram = service.Telegram('2000:' + 'x' * 32)
            approval = None
            original_finish = ActionQueue._finish
            original_signal = service._ServiceStop.on_signal

            def finish(queue, fd, state, item, status):
                if phase == 'audit':
                    os.write(1, b'blocked in audit\\n')
                    assert sys.stdin.readline() == 'finish\\n'
                return original_finish(queue, fd, state, item, status)

            def shutdown(stop, sig, frame):
                original_signal(stop, sig, frame)
                if phase == 'audit':
                    # SIGTERM can arrive before the preceding marker write
                    # returns; avoid Python's buffered stdout in this handler.
                    os.write(1, b'audit signal recorded\\n')

            def block():
                print(f'blocked in {phase}', flush=True)
                threading.Event().wait(30)
                raise AssertionError('SIGTERM did not interrupt transport')

            class BlockingResponse(Response):
                def read(self, _n=-1):
                    block()

            def open_(req, **_):
                global approval
                method = req.full_url.rsplit('/', 1)[-1]
                if method == 'getUpdates':
                    offset = json.loads(req.data)['offset']
                    if offset == 0:
                        updates = [message(1, '/send notes.txt')]
                    else:
                        assert offset == 2 and approval is not None
                        updates = [message(2, approval), message(3, '/new must not run')]
                    return Response(req, {'ok': True, 'result': updates})
                if method == 'sendMessage':
                    assert approval is None, 'unexpected send during shutdown'
                    text = json.loads(req.data)['text']
                    approval = re.search(r'/approve [0-9a-f]{32}', text)[0]
                    return Response(req, accepted())
                assert method == 'sendDocument' and b'fixture bytes' in req.data
                if phase == 'open':
                    block()
                if phase == 'audit':
                    return Response(req, accepted())
                return BlockingResponse(req, accepted())

            telegram.opener.open = open_
            local_os = Mock(wraps=service.os)
            local_os.environ = {}
            local_os.geteuid.return_value = 65534
            with (patch.object(service, 'config', return_value=(123, telegram, FakeProvider())),
                  patch.object(ActionQueue, '_finish', finish),
                  patch.object(service._ServiceStop, 'on_signal', shutdown),
                  patch.object(service, 'notify_ready'), patch.object(service, 'os', local_os)):
                sys.exit(service.main())
        ''')
        with self.subTest(phase=phase), TemporaryDirectory() as tmp:
            home = Path(tmp)
            bot = Bot(home, 123, self.api.bot_id, self.api, FakeProvider(), features=True)
            (bot.workspace / 'notes.txt').write_text('fixture bytes', encoding='utf-8')
            with subprocess.Popen([sys.executable, '-EsB', '-c', script, phase, '--home', str(home)],
                                  cwd=Path(__file__).resolve().parent, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0) as proc:
                assert proc.stdout is not None
                output = proc.stdout

                def line():
                    self.assertTrue(select.select([output], [], [], 4)[0], 'missing lifecycle marker')
                    return output.readline()

                try:
                    self.assertEqual(line(), f'blocked in {phase}\n'.encode())
                    proc.terminate()
                    if phase == 'audit':
                        self.assertEqual(line(), b'audit signal recorded\n')
                        with (home / 'telegram.lock').open('rb') as lock:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    _, stderr = proc.communicate(b'finish\n' if phase == 'audit' else None, timeout=4)
                    self.assertEqual(proc.returncode, 0, stderr.decode())
                finally:
                    if proc.poll() is None:
                        proc.kill()

            with (home / 'telegram.lock').open('rb') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with Scheduler(bot.state / 'bot-2000-owner-123.jobs.sqlite3'):
                pass  # DB lease is immediately available after shutdown.
            self.assertEqual(bot.offset(), 3)
            audit = bot._queue(bot.history).audit()
            self.assertEqual([(entry['kind'], entry['status']) for entry in audit],
                             [('attachment_send', expected_status), ('attachment_send', 'started')])
            self.assertEqual(audit[0]['id'], audit[1]['id'])
            approval = '/approve ' + audit[0]['id']
            with (patch.object(self.api, 'updates_since', return_value=[message(2, approval)]),
                  patch.object(self.api.opener, 'open', side_effect=AssertionError('unexpected request'))):
                bot.poll_once()  # A consumed Telegram update isn't dispatched again.
            self.assertIn('未确认', bot.local_command(approval))
