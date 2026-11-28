import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import os

ROOT = Path(__file__).resolve().parent


class CliTest(unittest.TestCase):
    def test_offline_demo_never_searches(self):
        from contextlib import redirect_stdout
        import io
        from minis_cli import terminal_chat

        with TemporaryDirectory() as d, redirect_stdout(io.StringIO()) as output, \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=['/search Debian 安全公告', '/exit']):
            self.assertEqual(terminal_chat(Path(d), demo=True, plain=True), 0)
        self.assertIn('离线演示不联网搜索', output.getvalue())

    def test_explicit_search_does_not_call_model_or_write_chat_history(self):
        from contextlib import redirect_stdout
        import io
        from minis_cli import Provider, terminal_chat

        with TemporaryDirectory() as d, redirect_stdout(io.StringIO()) as output, \
                patch('sys.stdin.isatty', return_value=True), \
                patch('minis_cli.terminal_provider', return_value=Provider('https://example.invalid/v1', 'test-key', 'test-model')), \
                patch('minis_cli.search_web', return_value='Debian\nhttps://www.debian.org/') as search, \
                patch.object(Provider, 'reply', side_effect=AssertionError('called model')), \
                patch('builtins.input', side_effect=['/search Debian 安全公告', '/exit']):
            self.assertEqual(terminal_chat(Path(d), plain=True), 0)
            self.assertFalse((Path(d) / 'telegram/bot-terminal-owner-1.json').exists())
        search.assert_called_once_with('Debian 安全公告')
        self.assertIn('https://www.debian.org/', output.getvalue())

    def test_time_and_disk_commands_are_local(self):
        from contextlib import redirect_stdout
        import io
        from minis_cli import terminal_chat

        with TemporaryDirectory() as d, redirect_stdout(io.StringIO()) as output, \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=['/time', '/disk', '/exit']), \
                patch('minis_cli.EchoDemo.reply', side_effect=AssertionError('called model')):
            self.assertEqual(terminal_chat(Path(d), demo=True, plain=True), 0)
        self.assertIn('本机时间：', output.getvalue())
        self.assertIn('工作区所在文件系统', output.getvalue())

    def test_terminal_window_shows_live_model_and_legacy_plain_mode(self):
        from contextlib import redirect_stdout
        import io
        import json
        from minis_cli import main, Provider

        with TemporaryDirectory() as d:
            config = Path(d) / 'terminal-config.json'
            config.write_text(json.dumps(['https://example.invalid/v1', 'first-model', 'fixture-key']))
            config.chmod(0o600)
            for flags in ([], ['--plain']):
                with patch('sys.argv', ['minis', '--home', d, *flags]), \
                        patch('sys.stdin.isatty', return_value=True), \
                        patch.dict(os.environ, {'TERM': 'xterm', 'COLUMNS': '96'}), \
                        patch('minis_cli.unicode_menu', return_value=True), \
                        patch('builtins.input', side_effect=['/model next-model', '你好', '/exit']), \
                        patch.object(Provider, 'reply', return_value={'content': 'mock reply'}), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(), 0)
                value = output.getvalue()
                self.assertNotIn('fixture-key', value)
                self.assertIn('mock reply', value)
                if flags:
                    self.assertNotIn('当前模型：', value)
                else:
                    self.assertIn('可用工具', value)
                    self.assertIn('当前模型：first-model', value)
                    self.assertIn('当前模型：next-model', value)
                    self.assertIn('上轮耗时：', value)

    def test_terminal_window_falls_back_in_narrow_terminal(self):
        from contextlib import redirect_stdout
        import io
        from minis_cli import terminal_chat

        with TemporaryDirectory() as d, redirect_stdout(io.StringIO()) as output, \
                patch('sys.stdin.isatty', return_value=True), \
                patch('minis_cli.TerminalView.width', return_value=32), \
                patch('builtins.input', side_effect=['你好', '/exit']):
            self.assertEqual(terminal_chat(Path(d), demo=True), 0)
        self.assertIn('离线演示：你好', output.getvalue())
        self.assertNotIn('╭', output.getvalue())

    def test_version_reader_rejects_nonregular_and_oversized_files(self):
        from minis_cli import installed_version
        with TemporaryDirectory() as d:
            root = Path(d)
            version = root / 'VERSION'
            other = root / 'other'
            other.write_text('0.3.0\n')
            version.symlink_to(other)
            with self.assertRaises((OSError, ValueError)):
                installed_version(root)
            version.unlink()
            version.write_bytes(b'0.3.0' + b' ' * 128)
            with self.assertRaises(ValueError):
                installed_version(root)

    def test_env_key_is_not_saved_when_other_fields_are_prompted(self):
        from minis_cli import terminal_provider
        with TemporaryDirectory() as d, patch.dict(os.environ, {'MINIS_API_KEY': 'fixture-key'}, clear=True), \
                patch('builtins.input', side_effect=['https://api.example.com/v1', 'model']):
            provider = terminal_provider(Path(d))
            self.assertEqual(provider.key, 'fixture-key')
            self.assertFalse((Path(d) / 'terminal-config.json').exists())

    def test_demo_home_preserves_real_history(self):
        from minis_cli import main
        with TemporaryDirectory() as d:
            p = Path(d) / 'telegram/bot-terminal-owner-1.json'
            p.parent.mkdir()
            p.write_text('[{"role":"user","content":"real"}]')
            with patch('sys.argv', ['minis', '--demo', '--home', d]), \
                    patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', side_effect=['/new', '/exit']):
                self.assertEqual(main(), 0)
            self.assertIn('real', p.read_text())

    def test_version_matches_version_file(self):
        result = subprocess.run([sys.executable, str(ROOT / 'minis_cli.py'), '--version'],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), 'Minis Linux ' + (ROOT / 'VERSION').read_text().strip())

    def test_highest_stable_version_and_no_downgrade(self):
        from minis_cli import select_version
        records = '\n'.join(f'{"a" * 40}\trefs/tags/{tag}' for tag in ('v0.9.0', 'v0.10.0', 'v9.0.0-rc.1', 'v4.0', 'v02.0.0'))
        self.assertEqual(select_version(records, '0.2.0'), ('0.10.0', 'a' * 40))
        self.assertIsNone(select_version(records, '0.10.0'))
        self.assertIsNone(select_version(records, '1.0.0'))

    def test_interactive_demo_keeps_one_window_and_no_network(self):
        from minis_cli import terminal_chat
        from contextlib import redirect_stdout
        import io
        with TemporaryDirectory() as d, patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=['记住：用中文', '你好', '/new', '查看记忆', '/exit']), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(terminal_chat(Path(d), demo=True), 0)
        self.assertIn('Minis Linux', output.getvalue())
        self.assertIn('离线演示：你好', output.getvalue())
        self.assertIn('1. 用中文', output.getvalue())

    def test_update_check_does_not_install(self):
        from minis_cli import update
        from unittest.mock import Mock
        git = Mock(return_value=f'{"a" * 40}\trefs/tags/v9.0.0\n')
        run = Mock()
        self.assertEqual(update(check=True, current='0.2.0', git=git, run=run), 0)
        git.assert_called_once()
        run.assert_not_called()

    def test_pending_install_is_not_reported_as_up_to_date(self):
        import minis_cli
        from unittest.mock import Mock
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.pending-upgrade.json').touch()
            git, run = Mock(return_value=''), Mock()
            with patch.multiple(minis_cli, HERE=root, INSTALLED=root):
                with self.assertRaisesRegex(ValueError, '--recover'):
                    minis_cli.update(check=True, current='0.2.0', git=git, run=run)
            git.assert_not_called()
            run.assert_not_called()

    def test_installed_user_update_verifies_tag_then_runs_only_user_installer(self):
        import minis_cli as cli
        from unittest.mock import Mock
        with TemporaryDirectory() as directory:
            home = Path(directory)
            bundle = home / '.minis/app' / ('0.4.1-' + 'a' * 64)
            bundle.mkdir(parents=True)
            oid = 'b' * 40
            verified = []

            def git(args):
                if args[0] == 'ls-remote':
                    return f'{oid}\trefs/tags/v0.4.2\n'
                if args[0] == 'clone':
                    checkout = Path(args[-1]); checkout.mkdir()
                    (checkout / 'VERSION').write_text('0.4.2\n')
                    (checkout / 'install.py').touch()
                    return ''
                if 'verify-tag' in args:
                    if not signed:
                        raise subprocess.CalledProcessError(1, args)
                    verified.append(True)
                    return ''
                if 'cat-file' in args:
                    return f'object {oid}\ntype commit\ntag v0.4.2\n\nfixture'
                return oid + '\n'

            def run(args, **kwargs):
                self.assertEqual(args[:2], [cli.PYTHON, '-EsB'])
                self.assertEqual(args[3:], ['--user', '--upgrade-to', '0.4.2'])
                self.assertEqual(kwargs['env']['HOME'], str(home))
                self.assertTrue(verified)
                return subprocess.CompletedProcess(args, 0)

            runner = Mock(side_effect=run)
            with patch.object(cli, 'HERE', bundle), patch.object(Path, 'home', return_value=home), \
                    patch.dict('os.environ', HOME=str(home), SUDO_UID=''):
                signed = False
                with self.assertRaisesRegex(ValueError, '签名'):
                    cli.update(check=False, current='0.4.1', git=git, run=runner)
                runner.assert_not_called()
                signed = True
                self.assertEqual(cli.update(check=False, current='0.4.1', git=git, run=runner), 0)
                runner.assert_called_once()

    def test_update_verifies_tag_and_version_before_running_installer(self):
        import minis_cli
        from unittest.mock import Mock
        with TemporaryDirectory() as directory:
            root = Path(directory)
            oid = 'b' * 40

            def git(args):
                if args[0] == 'ls-remote':
                    return f'{oid}\trefs/tags/v0.3.0\n'
                if args[0] == 'clone':
                    checkout = Path(args[-1])
                    checkout.mkdir()
                    (checkout / 'VERSION').write_text('0.3.0\n')
                    (checkout / 'install.py').write_text('# inert test installer')
                    return ''
                if 'verify-tag' in args:
                    if not signed:
                        raise subprocess.CalledProcessError(1, args)
                    return ''
                if 'cat-file' in args:
                    return f'object {oid}\ntype commit\ntag {signed_name}\n\nfixture'
                return ('c' * 40 if wrong_tag else oid) + '\n'

            def run(args, **options):
                self.assertEqual(args[-2:], ['--upgrade-to', '0.3.0'])
                self.assertTrue(Path(args[-3]).is_file())
                return subprocess.CompletedProcess(args, 7)

            runner = Mock(side_effect=run)
            real_tempdir = TemporaryDirectory
            with patch.multiple(minis_cli, HERE=root, INSTALLED=root), \
                    patch('minis_cli.os.geteuid', return_value=0), \
                    patch('minis_cli.tempfile.TemporaryDirectory', side_effect=lambda **kw: real_tempdir(dir=root)), \
                    patch('minis_cli.installed_version', wraps=minis_cli.installed_version) as read_version:
                wrong_tag = True
                signed = True
                with self.assertRaisesRegex(ValueError, '标签'):
                    minis_cli.update(check=False, current='0.2.0', git=git, run=runner)
                runner.assert_not_called()
                wrong_tag = False
                signed = False
                with self.assertRaisesRegex(ValueError, '签名'):
                    minis_cli.update(check=False, current='0.2.0', git=git, run=runner)
                runner.assert_not_called()
                read_version.assert_not_called()
                signed = True
                signed_name = 'v0.2.9'
                with self.assertRaisesRegex(ValueError, '不匹配'):
                    minis_cli.update(check=False, current='0.2.0', git=git, run=runner)
                runner.assert_not_called()
                signed_name = 'v0.3.0'
                self.assertEqual(minis_cli.update(check=False, current='0.2.0', git=git, run=runner), 7)
                runner.assert_called_once()
