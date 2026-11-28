from contextlib import redirect_stdout
import fcntl
from http.client import HTTPMessage
import io
import json
import os
import pty
import select
from pathlib import Path
import struct
import subprocess
import sys
import termios
from tempfile import TemporaryDirectory
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
import unittest
from unittest.mock import patch

from minis_cli import main, terminal_provider


class ModelCommandTest(unittest.TestCase):
    def run_secret_prompt(self, keys, expected, success_marker):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        original = termios.tcgetattr(slave)
        code = ('from minis_cli import ask_secret\n'
                'try:\n'
                f'    assert ask_secret("Key: ") == {expected!r}\n'
                'except KeyboardInterrupt:\n'
                '    print("cancelled", flush=True)\n'
                'except ValueError:\n'
                '    print("rejected", flush=True)\n'
                'else:\n'
                '    print("accepted", flush=True)\n')
        proc = subprocess.Popen([sys.executable, '-EsB', '-c', code], cwd=Path(__file__).resolve().parent,
                                stdin=slave, stdout=slave, stderr=slave)
        received = bytearray()
        deadline = time.monotonic() + 3

        def wait_for(needle):
            while needle not in received:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, bytes(received))
                if select.select([master], [], [], remaining)[0]:
                    received.extend(os.read(master, 4096))

        try:
            wait_for(b'Key: ')
            os.write(master, keys)
            wait_for(success_marker)
            self.assertEqual(proc.wait(timeout=3), 0)
            self.assertNotIn(b'fixture-', received)
            self.assertEqual(termios.tcgetattr(slave), original)
            return received
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

    def test_secret_prompt_masks_typing_and_restores_terminal(self):
        received = self.run_secret_prompt(b'fixture-xz\x7fy\r', 'fixture-xy', b'accepted')
        self.assertIn(b'**********\b \b*', received)

    def test_secret_prompt_ctrl_c_masks_input_and_restores_terminal(self):
        received = self.run_secret_prompt(b'fixture-\x03', None, b'cancelled')
        self.assertIn(b'********', received)

    def test_secret_prompt_accepts_bracketed_paste(self):
        received = self.run_secret_prompt(b'\x1b[200~fixture-xy\x1b[201~\r', 'fixture-xy', b'accepted')
        self.assertIn(b'**********', received)

    def test_secret_prompt_accepts_plain_paste(self):
        received = self.run_secret_prompt(b'fixture-xy\r', 'fixture-xy', b'accepted')
        self.assertIn(b'**********', received)

    def test_secret_prompt_rejects_multiline_bracketed_paste(self):
        self.run_secret_prompt(b'\x1b[200~fixture-xy\rfixture-z\x1b[201~\r', None, b'rejected')

    def test_secret_prompt_escape_still_cancels(self):
        self.run_secret_prompt(b'fixture-\x1b', None, b'cancelled')

    def test_catalog_auth_error_shows_status_not_response_body(self):
        self.save()
        error = HTTPError('https://api.example.com/v1/models', 401, 'private-error-detail',
                          HTTPMessage(), io.BytesIO(b'private-response-body'))
        with patch('sys.argv', ['minis', 'model', '--home', str(self.home)]), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=['1', '']), \
                patch('minis_cli.model_catalog', side_effect=error), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 0)
        self.assertIn('HTTP 401', output.getvalue())
        self.assertNotIn('private-', output.getvalue())
        self.assertEqual(terminal_provider(self.home).model, 'old')

    def setUp(self):
        d = TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = Path(d.name)
        self.config = self.home / 'terminal-config.json'

    def save(self, base='https://api.example.com/v1'):
        self.config.write_text(json.dumps([base, 'old', 'fixture-key']))
        self.config.chmod(0o600)

    def cli(self, *args):
        code = Path(__file__).with_name('minis_cli.py')
        return subprocess.run([sys.executable, str(code), 'model', *args, '--home', str(self.home)],
                              capture_output=True, text=True, timeout=5)

    def test_set_and_show_durable_default_without_network_or_key_output(self):
        self.save()
        changed = self.cli('vendor/new')
        self.assertEqual(changed.returncode, 0, changed.stderr)
        shown = self.cli('--show')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('vendor/new', shown.stdout)
        self.assertNotIn('fixture-key', changed.stdout + shown.stdout)
        p = terminal_provider(self.home)
        self.assertEqual((p.model, p.key), ('vendor/new', 'fixture-key'))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_invalid_model_id_leaves_existing_config_unchanged(self):
        self.save()
        original = self.config.read_bytes()
        changed = self.cli('invalid model')
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn('模型 ID 无效', changed.stderr)
        self.assertEqual(self.config.read_bytes(), original)

    def test_add_gateway_then_switch_back_without_reentering_key(self):
        self.save()
        with patch('sys.argv', ['minis', 'model', '--home', str(self.home)]), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('minis_cli.model_catalog', return_value=['vendor/new', 'old']) as catalog, \
                patch('minis_cli.ask_secret', return_value='second-key') as password, \
                patch('builtins.input', side_effect=['2', 'Backup', 'https://backup.example/v1', '1', 'y', '1', '2', 'y']), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 0)
            self.assertEqual(terminal_provider(self.home).endpoint, 'https://backup.example/v1/chat/completions')
            self.assertEqual(main(), 0)
        self.assertEqual(terminal_provider(self.home).model, 'old')
        self.assertEqual([(call.args[0].endpoint, call.args[0].key) for call in catalog.call_args_list],
                         [('https://backup.example/v1/chat/completions', 'second-key'),
                          ('https://api.example.com/v1/chat/completions', 'fixture-key')])
        password.assert_called_once()
        self.assertNotIn('second-key', output.getvalue())

    def test_gateway_change_requires_confirmation_before_writing_defaults(self):
        self.save()
        original = self.config.read_bytes()
        with patch('sys.argv', ['minis', 'model', '--home', str(self.home)]), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('minis_cli.model_catalog', return_value=['new']), \
                patch('minis_cli.ask_secret', return_value='second-key'), \
                patch('builtins.input', side_effect=['2', 'Backup', 'https://backup.example/v1', '1', '']), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 0)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIn('Backup · backup.example', output.getvalue())
        self.assertIn('聊天记录', output.getvalue())
        self.assertIn('长期记忆', output.getvalue())

    def test_slash_switch_affects_next_inference_without_saving(self):
        self.save()
        original = self.config.read_bytes()
        with patch('sys.argv', ['minis', '--home', str(self.home)]), patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=['/model vendor/new', 'hello', '/exit']), \
                patch('minis_cli.Provider.reply', autospec=True, return_value={'content': 'ok'}) as reply, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        reply.assert_called_once()
        self.assertEqual(reply.call_args.args[0].model, 'vendor/new')
        self.assertEqual(self.config.read_bytes(), original)

    def test_slash_menu_can_switch_saved_gateway_without_writing_defaults(self):
        self.config.write_text(json.dumps([0, [['Primary', 'https://api.example/v1', 'old', 'primary-key'],
                                              ['Backup', 'https://backup.example/v1', 'extra', 'backup-key']]]))
        self.config.chmod(0o600)
        original = self.config.read_bytes()
        with patch('sys.argv', ['minis', '--home', str(self.home)]), patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=['/model', '2', '1', 'hello', '/exit']), \
                patch('minis_cli.model_catalog', return_value=['alternate']), \
                patch('minis_cli.Provider.reply', autospec=True, return_value={'content': 'ok'}) as reply, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        provider = reply.call_args.args[0]
        self.assertEqual((provider.endpoint, provider.model, provider.key),
                         ('https://backup.example/v1/chat/completions', 'alternate', 'backup-key'))
        self.assertEqual(self.config.read_bytes(), original)

    def test_real_terminal_search_arrow_enter_and_escape(self):
        try:
            import curses
        except ImportError:
            self.skipTest('requires curses')
        try:
            curses.setupterm('xterm')
        except curses.error:
            self.skipTest('requires xterm terminfo')
        self.save()

        def run_picker(confirm, ascii_locale=False, launcher_flags=()):
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
            program = ('import sys; import minis_cli; '
                       'minis_cli.model_catalog = lambda p: ["alpha", "target-a", "target-b", "\\u6a21\\u578b"]; '
                       'sys.exit(minis_cli.main())')
            proc = subprocess.Popen([sys.executable, *launcher_flags, '-c', program, 'model', '--home', str(self.home)],
                                    cwd=Path(__file__).resolve().parent, stdin=slave, stdout=slave, stderr=slave,
                                    env={**os.environ, 'TERM': 'xterm', 'LC_ALL': 'C' if ascii_locale else 'C.UTF-8',
                                         'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'})
            os.close(slave)
            output = bytearray()
            deadline = time.monotonic() + 5

            def wait_for(text):
                needle = text.encode()
                while needle not in output:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, bytes(output))
                    if select.select([master], [], [], remaining)[0]:
                        try:
                            output.extend(os.read(master, 8192))
                        except OSError:
                            self.fail(f'Picker exited before showing {text}: {bytes(output)!r}')

            try:
                if ascii_locale:
                    wait_for('2. Add gateway')
                    self.assertIn(r'1. \u539f\u6709\u7f51\u5173'.encode(), output)
                    os.write(master, b'1\r')
                    wait_for(r'4. \u6a21\u578b')
                    self.assertTrue(bytes(output).isascii())
                    os.write(master, b'\r')
                    self.assertEqual(proc.wait(timeout=5), 0)
                    return
                wait_for('添加新网关')
                os.write(master, b'\r')
                wait_for('alpha')
                if confirm:
                    os.write(master, b'target\x1bOB\r')
                    wait_for('已保存终端默认模型：target-b')
                else:
                    os.write(master, b'\x1b')
                self.assertEqual(proc.wait(timeout=5), 0)
                self.assertNotIn(b'fixture-key', output)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2)
                os.close(master)

        original = self.config.read_bytes()
        run_picker(False, ascii_locale=True)
        self.assertEqual(self.config.read_bytes(), original)
        run_picker(False, ascii_locale=True, launcher_flags=('-EsB',))
        self.assertEqual(self.config.read_bytes(), original)
        run_picker(False)
        self.assertEqual(self.config.read_bytes(), original)
        run_picker(True)
        self.assertEqual(terminal_provider(self.home).model, 'target-b')

    def test_legacy_model_id_with_spaces_can_run_and_be_replaced(self):
        self.save()
        base, _, key = json.loads(self.config.read_text())
        self.config.write_text(json.dumps([base, 'My Local Model', key]))
        self.assertEqual(terminal_provider(self.home).model, 'My Local Model')
        with patch('sys.argv', ['minis', 'model', '--home', str(self.home)]), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('minis_cli.model_catalog', return_value=['vendor/new']), \
                patch('builtins.input', return_value='1'), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertEqual(terminal_provider(self.home).model, 'vendor/new')

    def test_empty_url_fragment_or_query_never_queries_catalog_or_saves_config(self):
        for suffix in ('#', '?'):
            with self.subTest(suffix=suffix), \
                    patch('sys.argv', ['minis', 'model', '--home', str(self.home)]), \
                    patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', side_effect=['Test', 'https://example.invalid/v1' + suffix, 'vendor/new']), \
                    patch('minis_cli.ask_secret', return_value='fixture-key'), \
                    patch('minis_cli.model_catalog', return_value=[]) as catalog, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
                catalog.assert_not_called()
                self.assertFalse(self.config.exists())

    def test_interactive_lists_models_from_configured_gateway(self):
        seen = []
        requested_models = []

        class Catalog(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers['Authorization'], self.headers['User-Agent']))
                data = json.dumps({'data': [{'id': 'vendor/alpha'}, {'id': 'vendor/beta'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requested_models.append((self.path, body['model'], self.headers['User-Agent']))
                data = b'{"choices":[{"message":{"content":"ok"}}]}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with HTTPServer(('127.0.0.1', 0), Catalog) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.save(f'http://127.0.0.1:{server.server_port}/v1')
                with patch('sys.argv', ['minis', 'model', '--home', str(self.home)]), \
                        patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=['1', '2']), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(), 0)
                self.assertEqual(terminal_provider(self.home).reply([{'role': 'user', 'content': 'test'}]),
                                 {'content': 'ok'})
            finally:
                server.shutdown()
                thread.join(2)
        expected_agent = 'Minis-Linux (+https://github.com/brucelau1987cn/minis-linux)'
        self.assertEqual(seen, [('/v1/models', 'Bearer fixture-key', expected_agent)])
        self.assertEqual(requested_models, [('/v1/chat/completions', 'vendor/beta', expected_agent)])
        self.assertIn('vendor/beta', output.getvalue())
        self.assertNotIn('fixture-key', output.getvalue())
        self.assertEqual(terminal_provider(self.home).model, 'vendor/beta')

    def test_catalog_failure_falls_back_to_manual_and_empty_selection_keeps_model(self):
        self.save()
        with patch('sys.argv', ['minis', 'model', '--home', str(self.home)]), \
                patch('sys.stdin.isatty', return_value=True), patch('minis_cli.model_catalog', side_effect=OSError), \
                patch('builtins.input', side_effect=['1', '', '1', 'vendor/custom']), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
            self.assertEqual(terminal_provider(self.home).model, 'old')
            self.assertEqual(main(), 0)
        self.assertEqual(terminal_provider(self.home).model, 'vendor/custom')

    def test_first_setup_uses_prompted_key_not_environment_key(self):
        with patch('sys.argv', ['minis', 'model', '--home', str(self.home)]), \
                patch('sys.stdin.isatty', return_value=True), \
                patch.dict(os.environ, {'MINIS_API_KEY': 'not-for-disk'}, clear=True), \
                patch('builtins.input', side_effect=['Test', 'https://api.example.com/v1', 'vendor/new']), \
                patch('minis_cli.ask_secret', return_value='fixture-new-key'), \
                patch('minis_cli.model_catalog', side_effect=OSError), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(self.config.read_text()),
                         [0, [['Test', 'https://api.example.com/v1', 'vendor/new', 'fixture-new-key']]])
