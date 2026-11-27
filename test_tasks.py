from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from minis import read_history
from minis_telegram import Bot, TelegramError
from test_telegram import FakeTelegram, message


class SlowProvider:
    def __init__(self, *, reply=None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.requests = []
        self.result = reply or {'content': '迟到结果'}

    def reply(self, messages):
        self.requests.append(messages)
        self.started.set()
        if not self.release.wait(3):
            raise TimeoutError('fixture wait expired')
        return self.result


class TaskControlTest(unittest.TestCase):
    def test_outbound_rate_limit_is_respected_without_blocking_inbound_poll(self):
        from minis_telegram import Telegram

        api = Telegram('1000:' + 'X' * 32)

        def request(method, body, timeout=15):
            if method == 'sendMessage':
                raise TelegramError(method, 429, 30)
            return []

        with patch.object(api, 'request', side_effect=request) as transport, \
                patch('minis_telegram.time.monotonic', return_value=100):
            with self.assertRaises(TelegramError):
                api.send(123, 'first')
            self.assertEqual(api.updates_since(0), [])
            with self.assertRaises(TelegramError):
                api.send(123, 'second')
            self.assertEqual([call.args[0] for call in transport.call_args_list],
                             ['sendMessage', 'getUpdates'])

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.client = FakeTelegram([])
        self.provider = SlowProvider()
        self.bot = Bot(self.home, 123, '1000', self.client, self.provider)

    def controller(self):
        from minis_telegram import TaskControl
        return TaskControl(self.bot)

    def feed(self, tasks, *updates):
        self.client.updates = list(updates)
        self.bot.poll_once(tasks.handle)

    def wait_for_worker(self, tasks):
        task = tasks.active
        assert task is not None and task.worker is not None
        worker = task.worker
        self.addCleanup(worker.join, 3)
        self.addCleanup(self.provider.release.set)
        self.assertTrue(self.provider.started.wait(2))
        return worker

    def test_stop_and_status_work_while_model_is_blocked_and_drop_late_result(self):
        tasks = self.controller()
        self.feed(tasks, message(1, '做个慢任务'))
        worker = self.wait_for_worker(tasks)
        self.feed(tasks, message(2, '/status'), message(3, '/stop', owner=999), message(4, '/stop'))
        texts = [text for _, text in self.client.sent]
        self.assertIn('已收到', texts[0])
        self.assertIn('等待模型', texts[1])
        self.assertIn('已停止', texts[2])
        self.assertEqual(len(texts), 3)
        self.assertFalse(self.provider.release.is_set())
        self.assertTrue(worker.is_alive())
        self.provider.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([text for _, text in self.client.sent], texts)
        self.assertEqual(read_history(self.bot.history), [])

    def test_busy_turn_is_not_executed_or_silently_queued(self):
        tasks = self.controller()
        self.feed(tasks, message(10, '先做这条'))
        worker = self.wait_for_worker(tasks)
        self.feed(tasks, message(11, '别并发'), message(12, '/stop'), message(13, '也别并发'))
        self.assertIn('忙', self.client.sent[-3][1])
        self.assertIn('忙', self.client.sent[-1][1])
        self.provider.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.provider.requests), 1)

    def test_rate_limited_status_reply_does_not_skip_stop_in_same_batch(self):
        tasks = self.controller()
        self.feed(tasks, message(80, '慢任务'))
        worker = self.wait_for_worker(tasks)
        original = self.client.send

        def limited_send(owner, text):
            if '等待模型' in text:
                raise TelegramError('sendMessage', 429, 30)
            original(owner, text)

        self.client.send = limited_send
        with self.assertRaises(TelegramError) as error:
            self.feed(tasks, message(81, '/status'), message(82, '/stop'))
        self.assertEqual(error.exception.retry_after, 30)
        self.assertIn('已停止', self.client.sent[-1][1])
        self.assertEqual(self.bot.offset(), 83)
        self.provider.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(read_history(self.bot.history), [])
        self.assertNotIn('迟到结果', [text for _, text in self.client.sent])

    def test_service_shutdown_waits_for_history_commit_before_releasing_state(self):
        from minis import save_history

        tasks = self.controller()
        self.feed(tasks, message(90, '最后一条'))
        worker = self.wait_for_worker(tasks)
        saving, allow_save, closing, closed = (threading.Event() for _ in range(4))

        def slow_save(path, history):
            saving.set()
            if not allow_save.wait(3):
                raise TimeoutError('fixture save timeout')
            save_history(path, history)

        def shutdown():
            closing.set()
            tasks.close()
            closed.set()

        closer = threading.Thread(target=shutdown, daemon=True)
        with patch('minis_telegram.save_history', side_effect=slow_save):
            try:
                self.provider.release.set()
                self.assertTrue(saving.wait(2))
                closer.start()
                self.assertTrue(closing.wait(2))
                self.assertFalse(closed.wait(0.1), 'shutdown returned with history write pending')
            finally:
                allow_save.set()
                worker.join(2)
                if closer.ident is not None:
                    closer.join(2)
        self.assertTrue(closed.is_set())
        self.assertEqual(read_history(self.bot.history)[-1]['content'], '迟到结果')

    def test_cancelled_turn_does_not_continue_through_tool_round(self):
        self.provider.result = {'tool_calls': [{'id': 'list', 'type': 'function', 'function': {
            'name': 'list_files', 'arguments': '{}'}}]}
        tasks = self.controller()
        self.feed(tasks, message(14, '列一下文件'))
        worker = self.wait_for_worker(tasks)
        self.feed(tasks, message(15, '/stop'))
        self.provider.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(read_history(self.bot.history), [])

    def test_completed_task_saves_history_and_next_turn_sees_it(self):
        tasks = self.controller()
        self.feed(tasks, message(20, '第一条'))
        worker = self.wait_for_worker(tasks)
        self.provider.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.client.sent[-1][1], '迟到结果')
        self.assertEqual([row['content'] for row in read_history(self.bot.history)], ['第一条', '迟到结果'])
        tasks.handle('/status')
        self.assertIn('已完成', self.client.sent[-1][1])

    def test_reset_after_stop_is_not_overwritten_by_cancelled_turn(self):
        from minis import save_history
        save_history(self.bot.history, [{'role': 'user', 'content': '旧会话'}])
        tasks = self.controller()
        self.feed(tasks, message(30, '慢任务'))
        worker = self.wait_for_worker(tasks)
        self.feed(tasks, message(31, '/new'), message(32, '/stop'), message(33, '/new'))
        self.assertIn('/stop', self.client.sent[-3][1])
        self.assertIn('已清除', self.client.sent[-1][1])
        self.provider.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(read_history(self.bot.history), [])

    def test_telegram_send_failure_does_not_wedge_worker(self):
        tasks = self.controller()
        self.feed(tasks, message(40, '慢任务'))
        worker = self.wait_for_worker(tasks)
        original = self.client.send

        def fail(owner, text):
            raise TelegramError('sendMessage', 502)

        self.client.send = fail
        self.provider.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.client.send = original
        tasks.handle('/status')
        self.assertIn('回复发送状态不明', self.client.sent[-1][1])

    def test_service_shutdown_rejects_uncommitted_result(self):
        tasks = self.controller()
        self.feed(tasks, message(50, '系统要退出'))
        worker = self.wait_for_worker(tasks)
        tasks.close()
        self.provider.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(read_history(self.bot.history), [])

    def test_unexpected_model_failure_sends_safe_notice_and_frees_slot(self):
        def fail(messages):
            self.provider.started.set()
            self.provider.release.wait(3)
            raise RuntimeError('fixture private details')

        self.provider.reply = fail
        tasks = self.controller()
        self.feed(tasks, message(55, '模拟故障'))
        worker = self.wait_for_worker(tasks)
        self.provider.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIn('处理失败', self.client.sent[-1][1])
        self.assertNotIn('private details', self.client.sent[-1][1])
        self.assertIsNone(tasks.active)

    def test_stop_during_delivery_does_not_claim_to_recall_reply(self):
        sending = threading.Event()
        delivered = threading.Event()
        original = self.client.send

        def slow_send(owner, text):
            if text == '迟到结果':
                sending.set()
                if not delivered.wait(2):
                    raise TelegramError('sendMessage')
            original(owner, text)

        self.client.send = slow_send
        self.addCleanup(delivered.set)
        tasks = self.controller()
        self.feed(tasks, message(60, '查发送状态'))
        worker = self.wait_for_worker(tasks)
        self.provider.release.set()
        self.assertTrue(sending.wait(2))
        self.feed(tasks, message(61, '/stop'))
        self.assertIn('不能再撤回', self.client.sent[-1][1])
        delivered.set()
        worker.join(2)
        self.assertEqual(self.client.sent[-1][1], '迟到结果')

    def test_loopback_http_stop_remains_responsive_while_model_is_blocked(self):
        from minis import Provider
        from minis_telegram import Telegram

        updates = [message(70, '慢任务')]
        sent = []
        model_started, release_model = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.endswith('/getUpdates'):
                    data = {'ok': True, 'result': [u for u in updates if u['update_id'] >= body['offset']]}
                elif self.path.endswith('/sendMessage'):
                    sent.append(body['text'])
                    data = {'ok': True, 'result': {'message_id': len(sent)}}
                elif self.path == '/v1/chat/completions':
                    model_started.set()
                    if not release_model.wait(4):
                        self.send_error(503)
                        return
                    data = {'choices': [{'message': {'content': '模型迟到'}}]}
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            try:
                from minis_telegram import TaskControl
                base = f'http://127.0.0.1:{server.server_port}'
                with patch('minis_telegram.TELEGRAM_HOST', base):
                    telegram = Telegram('1000:' + 'X' * 32)
                provider = Provider(base + '/v1', 'fixture-key', 'fixture-model', read_only=True)
                bot = Bot(self.home, 123, '1000', telegram, provider)
                tasks = TaskControl(bot)
                bot.poll_once(tasks.handle)
                task = tasks.active
                assert task is not None and task.worker is not None
                self.addCleanup(task.worker.join, 2)
                self.assertTrue(model_started.wait(2))
                updates.extend([message(71, '/status'), message(72, '/stop')])
                bot.poll_once(tasks.handle)
                self.assertIn('等待模型', sent[1])
                self.assertIn('已停止', sent[2])
                self.assertFalse(release_model.is_set())
                release_model.set()
                task.worker.join(2)
                self.assertFalse(task.worker.is_alive())
                self.assertEqual(len(sent), 3)
                self.assertEqual(read_history(bot.history), [])
            finally:
                release_model.set()
                server.shutdown()
                serving.join(2)
