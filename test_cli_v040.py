"""Terminal composer, streaming, and model configuration integration tests."""
from contextlib import redirect_stderr, redirect_stdout
import fcntl
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import termios
from threading import Thread
from time import monotonic
import unittest
from unittest.mock import patch

import minis_cli as cli
from minis import Provider, read_history, save_history
from minis_input import Prompt


class ContractBot:
    """Parallel Bot integration contract; records exactly what the CLI forwards."""
    def __init__(self, home, owner, bot_id, terminal, provider, *, skills_dir, channel, features=False):
        self.telegram, self.provider, self.skills_dir = terminal, provider, skills_dir
        self.features = features
        self.commands, self.generated, self.committed, self.handled = [], [], [], []
        self.rounds = None
        self.footer = ''
        self.fail = False

    def local_command(self, text):
        self.commands.append(text)
        return 'local reply' if text.startswith('/') else None

    def generate(self, text, *, checkpoint=None, on_delta=None, on_notice=None, on_model=None,
                 proposals=None, fallback_model=None):
        self.generated.append((text, text.allow_commands, fallback_model))
        rounds = self.rounds if self.rounds is not None else [(str(text), True)]
        for content, stream in rounds:
            if checkpoint is not None:
                checkpoint('等待模型')
            if stream and on_delta is not None:
                for ch in content:
                    on_delta(ch)
        if on_model is not None:
            on_model(self.provider.model)
        if self.fail:
            return 'request failed', None
        answer = rounds[-1][0]
        history = [{'role': 'assistant', 'content': answer}]
        return answer + self.footer, history

    def commit_turn(self, history, proposals):
        self.committed.append(history)
        return []

    def send_reply(self, reply):
        self.telegram.send(1, reply.strip())

    def handle(self, text, *, allow_commands=True, on_delta=None, on_notice=None, on_model=None,
               fallback_model=None):
        self.handled.append((text, allow_commands, on_delta, fallback_model))
        self.send_reply('whole reply')


class TerminalTurnTest(unittest.TestCase):
    def chat(self, *prompts, rounds=None, footer='', fail=False, plain=False, fallback=None):
        output, created = io.StringIO(), []
        provider = cli.make_provider('https://gateway.invalid/v1', 'primary', 'fixture-key')
        provider.gateway_profile = 'Fixture'

        def make_bot(*args, **kwargs):
            bot = ContractBot(*args, **kwargs)
            bot.rounds, bot.footer, bot.fail = rounds, footer, fail
            created.append(bot)
            return bot

        with TemporaryDirectory() as directory, redirect_stdout(output), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('minis_cli.unicode_menu', return_value=True), patch.dict(os.environ, TERM='xterm'), \
                patch('minis_cli.terminal_provider', return_value=provider), \
                patch('minis_cli.terminal_fallbacks', return_value={'Fixture': (provider.endpoint, fallback)}), \
                patch('minis_cli.Bot', side_effect=make_bot), \
                patch('minis_input.read_prompt', side_effect=[*prompts, Prompt('/exit', allow_commands=True)]) as read, \
                patch('builtins.input', side_effect=AssertionError('composer was bypassed')), \
                patch('minis_cli.search_web') as search, patch('minis_cli.session_model') as model:
            self.assertEqual(cli.terminal_chat(Path(directory), plain=plain), 0)
            search.assert_not_called()
            model.assert_not_called()
            self.assertEqual(read.call_args.kwargs['plain'], plain)
        return output.getvalue(), created[0]

    def test_pasted_commands_are_chat_not_local_commands(self):
        texts = ('/exit', '/search fixture', '/model other', '/new', '/approve 123', '记住：not a preference')
        output, bot = self.chat(*(Prompt(text, allow_commands=False) for text in texts))
        self.assertTrue(bot.features)
        self.assertEqual(bot.commands, [])
        self.assertEqual(bot.generated, [(text, False, None) for text in texts])
        for text in texts:
            self.assertEqual(output.count('Minis › ' + text), 1)

    def test_final_stream_is_not_repeated_and_footer_is_preserved(self):
        output, bot = self.chat(Prompt('hello', allow_commands=True),
                                rounds=[('one \x1b[31manswer', True)], footer='\n\nSources: local fixture')
        self.assertEqual(output.count('one '), 1)
        self.assertEqual(output.count('Sources: local fixture'), 1)
        self.assertNotIn('\x1b[31m', output)
        self.assertEqual(len(bot.committed), 1)

    def test_json_final_is_printed_even_if_identical_to_earlier_stream(self):
        output, _ = self.chat(Prompt('hello', allow_commands=True), rounds=[('same text', True), ('same text', False)])
        self.assertEqual(output.count('Minis › same text'), 2)

    def test_streamed_final_after_streamed_tool_round_is_not_duplicated(self):
        output, _ = self.chat(Prompt('hello', allow_commands=True), rounds=[('before tools', True), ('final answer', True)])
        self.assertEqual(output.count('Minis › before tools'), 1)
        self.assertEqual(output.count('Minis › final answer'), 1)

    def test_partial_stream_keeps_error_and_does_not_commit(self):
        output, bot = self.chat(Prompt('hello', allow_commands=True), rounds=[('partial', True)], fail=True)
        self.assertIn('Minis › partial\nMinis › request failed', output)
        self.assertEqual(bot.committed, [])

    def test_plain_leaves_streaming_off_but_preserves_paste_flag(self):
        output, bot = self.chat(Prompt('/new', allow_commands=False), plain=True, fallback='backup')
        self.assertEqual(bot.handled, [('/new', False, None, 'backup')])
        self.assertIn('Minis › whole reply', output)


class FallbackConfigTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.config = self.home / 'terminal-config.json'
        self.write_config([0, [
            ['Alpha', 'https://shared.invalid/v1', 'one', 'fixture-key-a'],
            ['Beta', 'https://shared.invalid/v1', 'two', 'fixture-key-b'],
        ]])

    def write_config(self, data):
        self.config.write_text(json.dumps(data), encoding='utf-8')
        self.config.chmod(0o600)

    def model(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['minis', '--home', str(self.home), 'model', *args]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            return cli.main(), stdout.getvalue(), stderr.getvalue()

    def fallback(self, provider):
        return cli.gateway_fallback(provider, cli.terminal_fallbacks(self.home))

    def test_named_models_keep_profile_and_endpoint_binding(self):
        original = self.config.read_bytes()
        self.assertEqual(self.model('--fallback', 'fallback-alpha')[0], 0)
        self.assertEqual(self.model('--gateway', 'Beta', '--fallback', 'fallback-beta')[0], 0)
        result = self.model('--show-fallback', '--gateway', 'Beta')
        self.assertEqual(result[0], 0)
        self.assertIn('fallback-beta', result[1])
        self.assertEqual(self.config.read_bytes(), original)
        alpha = cli.terminal_provider(self.home)
        self.assertEqual(self.fallback(alpha), 'fallback-alpha')
        with patch('minis_cli.pick_index', return_value=1), patch('minis_cli.select_model', return_value='temp'), \
                redirect_stdout(io.StringIO()):
            beta = cli.session_model(self.home, alpha, None)
            self.assertEqual((beta.endpoint, beta.key, self.fallback(beta)),
                             (alpha.endpoint, 'fixture-key-b', 'fallback-beta'))
            beta_override = cli.session_model(self.home, beta, 'fallback-beta')
            self.assertIsNone(self.fallback(beta_override))
        # Even on the same host, a different Base URL is not the saved endpoint.
        active, rows = json.loads(original)
        rows[active][1] = 'https://shared.invalid/other'
        self.write_config([active, rows])
        self.assertIsNone(self.fallback(cli.terminal_provider(self.home)))
        # An equivalent one-off provider has no saved-profile identity.
        self.assertIsNone(self.fallback(Provider('https://shared.invalid/v1', 'fixture-key-a', 'one')))

    def test_legacy_config_supports_set_show_and_clear_without_migration(self):
        initial = ['https://legacy.invalid/v1/', 'primary', 'fixture-key-c']
        self.write_config(initial)
        self.assertEqual(self.model('--fallback', 'alternate')[0], 0)
        self.assertEqual(self.fallback(cli.terminal_provider(self.home)), 'alternate')
        self.assertIn('alternate', self.model('--show-fallback')[1])
        sidecar = next(self.home.glob('*fallback*.json'))
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('fixture-key-c', sidecar.read_text())
        self.assertEqual(self.model('--clear-fallback')[0], 0)
        self.assertIsNone(self.fallback(cli.terminal_provider(self.home)))
        self.assertEqual(json.loads(self.config.read_bytes()), initial)

    def test_refuses_unsaved_gateway_or_invalid_model(self):
        for args in (('--gateway', 'missing', '--fallback', 'alternate'),
                     ('--fallback', 'white space'), ('--fallback', 'one')):
            with self.subTest(args=args):
                self.assertEqual(self.model(*args)[0], 1)
        self.assertFalse(list(self.home.glob('*fallback*.json')))

    def test_ambiguous_profile_does_not_get_a_fallback(self):
        self.assertEqual(self.model('--fallback', 'fallback-alpha')[0], 0)
        active, rows = json.loads(self.config.read_bytes())
        rows[1] = ['alpha', *rows[0][1:3], 'fixture-other-key']
        self.write_config([active, rows])
        self.assertIsNone(self.fallback(cli.terminal_provider(self.home)))
        self.assertEqual(self.model('--show-fallback')[0], 1)


class LiveTerminalTest(unittest.TestCase):
    """Exercise the feature-capable Bot, not only the proposed call contract."""

    def test_loopback_fallback_stays_at_the_same_endpoint_and_streams_public_text(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers['Authorization'], data['model'], data['stream']))
                if data['model'] == 'primary':
                    self.send_error(429)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for delta, reason in [({'reasoning_content': 'PRIVATE TRACE'}, None),
                                      ({'content': 'fallback answer'}, None), ({}, 'stop')]:
                    frame = {'choices': [{'delta': delta, 'finish_reason': reason}]}
                    self.wfile.write(f'data: {json.dumps(frame)}\n\n'.encode())
                self.wfile.write(b'data: [DONE]\n\n')

        with HTTPServer(('127.0.0.1', 0), Handler) as server, TemporaryDirectory() as directory:
            home, output = Path(directory), io.StringIO()
            base = f'http://127.0.0.1:{server.server_port}/v1'
            save_history(home / 'terminal-config.json', [base, 'primary', 'fixture-only'])
            with redirect_stdout(io.StringIO()):
                cli.fallback_command(home, model_id='fallback')
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with redirect_stdout(output), patch('sys.stdin.isatty', return_value=True), \
                        patch('minis_cli.unicode_menu', return_value=True), patch.dict(os.environ, TERM='xterm'), \
                        patch('minis_input.read_prompt', side_effect=[Prompt('hello', allow_commands=True),
                                                                    Prompt('/exit', allow_commands=True)]):
                    self.assertEqual(cli.terminal_chat(home), 0)
            finally:
                server.shutdown()
                thread.join(timeout=3)
            history = read_history(home / 'telegram/bot-terminal-owner-1.json')
        self.assertEqual(requests, [('/v1/chat/completions', 'Bearer fixture-only', 'primary', True),
                                    ('/v1/chat/completions', 'Bearer fixture-only', 'fallback', True)])
        self.assertEqual(history[-1]['content'], 'fallback answer')
        value = output.getvalue()
        self.assertIn('本轮模型：fallback', value)
        self.assertEqual(value.count('fallback answer'), 1)
        self.assertNotIn('PRIVATE TRACE', value + repr(history))

    def test_demo_pty_paste_previews_and_does_not_run_exit(self):
        with TemporaryDirectory() as directory:
            master, slave = pty.openpty()
            self.addCleanup(os.close, master)
            self.addCleanup(os.close, slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
            original = termios.tcgetattr(slave)
            script = Path(cli.__file__)
            proc = subprocess.Popen([sys.executable, '-EsB', str(script), '--demo', '--home', directory],
                                    stdin=slave, stdout=slave, stderr=slave,
                                    env=dict(os.environ, TERM='xterm-256color', LC_ALL='C.UTF-8', NO_COLOR='1'))
            output = bytearray()

            def wait_for(marker, since=0):
                deadline = monotonic() + 3
                while output.find(marker, since) == -1:
                    wait = deadline - monotonic()
                    self.assertGreater(wait, 0, bytes(output))
                    if select.select([master], [], [], wait)[0]:
                        output.extend(os.read(master, 8192))
                return output.index(marker, since)

            try:
                wait_for(b'\x1b[?2004h')
                os.write(master, b'\x1b[200~/exit\x1b[201~')
                wait_for(b'Paste preview')
                self.assertNotIn('离线演示：/exit'.encode(), output)
                os.write(master, b'\r')
                replied = wait_for('Minis › 离线演示：/exit'.encode())
                wait_for(b'\x1b[?2004h', replied)
                os.write(master, b'/exit\r')
                self.assertEqual(proc.wait(timeout=3), 0)
                self.assertEqual(termios.tcgetattr(slave), original)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)
