import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from minis import read_history
from minis_telegram import Bot, TaskControl, Telegram
from minis_transport import MediaResult
from test_telegram import FakeProvider, FakeTelegram, message


class ServiceV040Test(unittest.TestCase):
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
