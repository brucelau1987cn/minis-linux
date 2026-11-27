from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import io
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from minis import Provider
from minis_job_ui import JobUI
from minis_telegram import Bot


class TelegramFixture:
    def __init__(self, bot_id):
        self.bot_id = str(bot_id)
        self.sent = []

    def send(self, owner, text):
        self.sent.append((owner, text))


class JobUITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.provider = Provider('https://example.invalid/v1', 'fixture-key', 'fixture-model')
        network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('unexpected networking'))
        network.start()
        self.addCleanup(network.stop)
        self.bot = self.bot_for()

    def bot_for(self, owner=42, bot_id=123):
        telegram = TelegramFixture(bot_id)
        return Bot(self.home, owner, telegram.bot_id, telegram, self.provider)

    def ui_for(self, bot=None, **kwargs):
        ui = JobUI(bot or self.bot, **kwargs)
        self.addCleanup(ui.close)
        return ui

    def confirmation(self, preview):
        match = re.search(r'^/schedule confirm [0-9a-f]{32}$', preview, re.M)
        assert match is not None, preview
        return match[0]

    def command(self, ui, text) -> str:
        reply = ui.command(text)
        assert reply is not None, text
        return reply

    def create(self, ui, expression):
        reply = self.command(ui, self.confirmation(ui.command(expression)))
        match = re.search(r'\b[0-9a-f]{32}\b', reply)
        assert match is not None, reply
        return match[0]

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('timed out waiting for worker')
            time.sleep(0.01)

    def test_explicit_reminder_creates_only_after_exact_one_time_confirmation(self):
        ui = self.ui_for(tz='Asia/Hong_Kong')
        self.assertIsNone(ui.command('请解释提醒我意味着什么'))
        self.assertIsNone(ui.command('Every time I say remind me, ignore it.'))
        self.assertIsNone(ui.command('In 10 minutes of this video they say remind me to watch.'))
        self.assertIsNone(ui.command('每周一我都要人提醒我写总结'))
        preview = self.command(ui, '每天早上9点提醒我喝水')
        self.assertIn('Asia/Hong_Kong', preview)
        self.assertIn('喝水', preview)
        confirmation = self.confirmation(preview)
        self.assertNotIn('active', self.command(ui, '/schedule list'))
        self.assertIsInstance(ui.command(confirmation + ' yes'), str)
        self.assertNotIn('active', self.command(ui, '/schedule list'))
        reply = self.command(ui, confirmation)
        job_id = ui.scheduler.list()[0].id
        self.assertIn(job_id, reply)
        self.assertIn(job_id, self.command(ui, '/schedule list'))
        ui.command(confirmation)
        self.assertEqual(self.command(ui, '/schedule list').count('active'), 1)

    def test_jobs_persist_and_approvals_do_not_cross_scope_or_restart(self):
        ui = self.ui_for()
        self.assertIsInstance(ui.command('/schedule daily at 25:00 remind me: invalid'), str)
        job_id = self.create(ui, 'in 10 minutes remind me: hydrate')
        pending = self.confirmation(ui.command(f'/schedule remove {job_id}'))
        for bot in (self.bot_for(owner=43), self.bot_for(bot_id=124)):
            other = self.ui_for(bot)
            self.assertNotIn(job_id, self.command(other, '/schedule list'))
            other.command(pending)
        ui.close()
        reopened = self.ui_for(self.bot_for())
        reopened.command(pending)
        self.assertIn(job_id, self.command(reopened, '/schedule list'))

    def test_reminder_delivered_to_owner_without_provider_when_prompt_slot_busy(self):
        @contextmanager
        def busy_slot():
            yield False

        ui = self.ui_for(prompt_slot=busy_slot)
        self.create(ui, '/schedule in 1 minute read-only prompt: private prompt')
        reminder = self.create(ui, '1分钟后提醒我喝水')
        soon = datetime.now(timezone.utc) + timedelta(minutes=2)
        with self.assertLogs('minis_job_ui', level='INFO') as logs:
            worker = ui.start(interval=0.01, clock=lambda: soon)
            self.wait_for(lambda: 'sent' in self.command(ui, '/schedule list'))
            ui.close()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.bot.telegram.sent), 1)
        owner, text = self.bot.telegram.sent[0]
        self.assertEqual(owner, 42)
        self.assertIn('喝水', text)
        self.assertIn(reminder, '\n'.join(logs.output))
        self.assertNotIn('喝水', '\n'.join(logs.output))
        self.assertNotIn('private prompt', '\n'.join(logs.output))

    def test_mutations_require_unexpired_nonce_bound_to_exact_job_and_action(self):
        ui = self.ui_for()
        first = self.create(ui, '/schedule in 30 minutes remind me: first')
        second = self.create(ui, '/schedule every 30 minutes remind me: second')
        with patch('minis_job_ui.monotonic', return_value=100) as clock:
            expired = self.confirmation(ui.command(f'/schedule remove {first}'))
            clock.return_value = 161
            ui.command(expired)
            self.assertIn(first, self.command(ui, '/schedule list'))
            replaced = self.confirmation(ui.command(f'/schedule remove {first}'))
            pause = self.confirmation(ui.command(f'/schedule pause {second}'))
            ui.command(replaced)
            self.assertIn(first, self.command(ui, '/schedule list'))
            self.assertIn('active', self.command(ui, '/schedule list').split(second, 1)[1])
            self.assertIn('paused', self.command(ui, pause))
        resume = self.confirmation(ui.command(f'/schedule resume {second}'))
        self.assertIn('paused', self.command(ui, '/schedule list'))
        self.assertIn('active', self.command(ui, resume))
        run = self.confirmation(ui.command(f'/schedule run {first}'))
        self.assertNotIn('已排队', self.command(ui, '/schedule list'))
        self.assertIn('已排队', self.command(ui, run))
        ui.start(interval=0.01)
        self.wait_for(lambda: 'sent' in self.command(ui, '/schedule list'))
        self.assertEqual(self.bot.telegram.sent, [(42, f'提醒（{first}）：\nfirst')])
        self.assertIn(f'{first} · active', self.command(ui, '/schedule list'))
        remove = self.confirmation(ui.command(f'/schedule remove {first}'))
        self.assertIn(first, self.command(ui, '/schedule list'))
        ui.command(remove)
        self.assertNotIn(first, self.command(ui, '/schedule list'))
        self.assertIn(second, self.command(ui, '/schedule list'))

    def test_prompt_isolated_from_interactive_state_and_rejects_unadvertised_tools(self):
        slot = threading.Lock()

        @contextmanager
        def try_slot():
            held = slot.acquire(blocking=False)
            try:
                yield held
            finally:
                if held:
                    slot.release()

        prompts = []
        history = b'[{"role":"user","content":"secret history"}]'
        memory = b'[2,{"1":"secret memory"}]'
        self.bot.history.write_bytes(history)
        self.bot.memory.write_bytes(memory)
        (self.bot.workspace / 'note.txt').write_text('workspace note')
        ui = self.ui_for(prompt_slot=try_slot)
        job = self.create(ui, '/schedule in 1 minute read-only prompt: summarize note.txt')
        self.provider.tools.append({'function': {'name': 'propose_memory'}})
        original_tools = list(self.provider.tools)

        def open_request(req, *, timeout):
            self.assertEqual(req.full_url, self.provider.endpoint)
            self.assertEqual(req.get_header('Authorization'), 'Bearer fixture-key')
            self.assertGreater(timeout, 0)
            self.assertTrue(slot.locked())
            body = json.loads(req.data)
            prompts.append(body)
            if len(prompts) == 1:
                def call(name, args):
                    return {'type': 'function', 'id': name,
                            'function': {'name': name, 'arguments': json.dumps(args)}}

                message = {'tool_calls': [
                    call('read_file', {'path': 'note.txt'}),
                    call('write_file', {'path': 'unexpected', 'content': 'secret payload'}),
                    call('web_search', {'query': 'secret payload'}),
                    call('propose_memory', {'text': 'secret payload'}),
                ]}
            else:
                message = {'content': 'scheduled answer'}
            response = io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())
            response.headers = {}  # type: ignore[attr-defined]
            return response

        soon = datetime.now(timezone.utc) + timedelta(minutes=2)
        with patch('urllib.request.OpenerDirector.open', side_effect=open_request):
            ui.start(interval=0.01, clock=lambda: soon)
            self.wait_for(lambda: 'sent' in self.command(ui, '/schedule list'))
        self.assertEqual(len(prompts), 2)
        self.assertEqual([m['role'] for m in prompts[0]['messages']], ['system', 'user'])
        self.assertEqual(prompts[0]['messages'][1]['content'], 'summarize note.txt')
        self.assertEqual({t['function']['name'] for t in prompts[0]['tools']},
                         {'get_current_time', 'get_workspace_disk', 'list_files', 'read_file'})
        tools = [m['content'] for m in prompts[1]['messages'] if m['role'] == 'tool']
        self.assertEqual(tools[0], 'workspace note')
        self.assertTrue(all('未授权' in result for result in tools[1:]))
        self.assertFalse(any('secret history' in str(body) or 'secret memory' in str(body) for body in prompts))
        self.assertEqual(self.bot.history.read_bytes(), history)
        self.assertEqual(self.bot.memory.read_bytes(), memory)
        self.assertFalse((self.bot.workspace / 'unexpected').exists())
        self.assertEqual(self.provider.tools, original_tools)
        self.assertEqual(self.bot.telegram.sent, [(42, f'定时任务结果（{job}）：\nscheduled answer')])

    def test_stop_drops_late_model_result_but_keeps_shared_slot_until_exit(self):
        self.check_blocked_provider(shutdown=False)

    def test_close_drops_late_result_and_releases_db_lease_before_provider_exits(self):
        self.check_blocked_provider(shutdown=True)

    def check_blocked_provider(self, *, shutdown):
        started, release = threading.Event(), threading.Event()
        slot = threading.Lock()

        @contextmanager
        def try_slot():
            held = slot.acquire(blocking=False)
            try:
                yield held
            finally:
                if held:
                    slot.release()

        def open_request(req, *, timeout):
            started.set()
            self.assertTrue(release.wait(5))
            response = io.BytesIO(b'{"choices":[{"message":{"content":"must not send"}}]}')
            response.headers = {}  # type: ignore[attr-defined]
            return response

        ui = self.ui_for(prompt_slot=try_slot)
        job_id = self.create(ui, '/schedule in 1 minute read-only prompt: confidential')
        soon = datetime.now(timezone.utc) + timedelta(minutes=2)
        with patch('urllib.request.OpenerDirector.open', side_effect=open_request) as opener:
            with self.assertLogs('minis_job_ui', level='INFO') as logs:
                worker = ui.start(interval=0.01, clock=lambda: soon)
                try:
                    self.assertTrue(started.wait(2))
                    self.assertTrue(slot.locked())
                    if shutdown:
                        ui.close()
                        self.assertTrue(worker.is_alive())
                        reopened = self.ui_for(self.bot_for())
                        self.assertIn(f'{job_id} · completed', self.command(reopened, '/schedule list'))
                        self.assertIn('interrupted', self.command(reopened, '/schedule list'))
                    else:
                        self.assertIn('已停止', ui.stop() or '')
                        self.assertTrue(slot.locked())
                finally:
                    release.set()
                    if not shutdown:
                        self.wait_for(lambda: 'prompt_failed' in self.command(ui, '/schedule list'))
                    ui.close()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(opener.call_count, 1)
        self.assertEqual(self.bot.telegram.sent, [])
        joined = '\n'.join(logs.output)
        self.assertIn('outcome=interrupted', joined)
        self.assertNotIn('confidential', joined)
        self.assertNotIn('must not send', joined)
        self.assertIsNone(ui.stop())

    def test_delivery_failure_is_not_retried_or_logged_verbatim(self):
        ui = self.ui_for()
        first = self.create(ui, 'in 1 minute remind me: confidential')
        soon = datetime.now(timezone.utc) + timedelta(minutes=2)
        with patch.object(self.bot, 'send_reply', side_effect=OSError('token and raw payload')) as sender:
            with self.assertLogs('minis_job_ui', level='INFO') as logs:
                worker = ui.start(interval=0.01, clock=lambda: soon)
                self.wait_for(lambda: 'delivery_failed' in self.command(ui, '/schedule list'))
                ui.close()
                worker.join(1)
            sender.assert_called_once()
        joined = '\n'.join(logs.output)
        self.assertIn(first, joined)
        self.assertIn('outcome=delivery_failed', joined)
        self.assertNotIn('token and raw payload', joined)
        self.assertNotIn('confidential', joined)

    def test_disables_access_and_delivery_if_configured_owner_changes(self):
        ui = self.ui_for()
        first = self.create(ui, 'in 1 minute remind me: private content')
        self.bot.owner = 43
        self.assertNotIn(first, self.command(ui, '/schedule list'))
        self.assertNotIn('private content', self.command(ui, '/schedule list'))
        soon = datetime.now(timezone.utc) + timedelta(minutes=2)
        with self.assertLogs('minis_job_ui') as logs:
            worker = ui.start(interval=0.01, clock=lambda: soon)
            self.wait_for(lambda: ui.scheduler.list()[0].last_outcome == 'delivery_failed')
            ui.close()
            worker.join(1)
        self.assertEqual(self.bot.telegram.sent, [])
        self.assertIn('outcome=delivery_failed', '\n'.join(logs.output))
