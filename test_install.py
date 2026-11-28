from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
from pathlib import Path
import os
import socket
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch


class InstallTest(unittest.TestCase):
    def test_user_only_install_reuses_legacy_config_and_runs_from_other_directory(self):
        from install import install_user
        legacy = self.root / '.local/share/minis-linux-terminal'
        legacy.mkdir(parents=True, mode=0o700)
        config = legacy / 'terminal-config.json'
        config.write_text('["https://example.invalid/v1", "saved-model", "fixture-key"]')
        config.chmod(0o600)
        initial = config.read_bytes()
        session = legacy / 'telegram/bot-terminal-owner-1.json'
        session.parent.mkdir(mode=0o700)
        session.write_text('[{"role":"user","content":"saved conversation"}]')
        initial_session = session.read_bytes()
        install_user(self.source, self.root)
        install_user(self.source, self.root)
        state = self.root / '.minis/terminal/terminal-config.json'
        self.assertEqual(state.read_bytes(), initial)
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual((state.parent / session.relative_to(legacy)).read_bytes(), initial_session)
        self.assertFalse(legacy.exists())
        launcher = self.root / '.local/bin/minis'
        run = subprocess.run([str(launcher), 'model', '--show'], cwd='/',
                             env={**os.environ, 'HOME': str(self.root)}, text=True,
                             capture_output=True, check=True)
        self.assertIn('saved-model', run.stdout)
        self.assertNotIn('fixture-key', run.stdout)
        self.assertFalse((self.root / '.hermes').exists())

    def test_user_only_install_does_not_overwrite_foreign_launcher(self):
        from install import install_user
        launcher = self.root / '.local/bin/minis'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('unrelated')
        with self.assertRaises(ValueError):
            install_user(self.source, self.root)
        self.assertEqual(launcher.read_text(), 'unrelated')
        self.assertFalse((self.root / '.minis').exists())

    def test_user_install_does_not_move_state_while_chat_is_running(self):
        from install import install_user
        from minis_cli import terminal_lock
        legacy = self.root / '.local/share/minis-linux-terminal'
        with terminal_lock(legacy), self.assertRaisesRegex(ValueError, '已有 Minis 终端会话'):
            install_user(self.source, self.root)
        self.assertTrue(legacy.is_dir())
        self.assertFalse((self.root / '.minis/terminal').exists())
        self.assertFalse((self.root / '.local/bin/minis').exists())

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = Path(__file__).resolve().parent

    def test_install_and_upgrade_preserve_credentials_and_state(self):
        from install import install
        ask = Mock(return_value=('123', 'https://api.example.com/v1', 'test/model',
                                 '123:' + 'X' * 32, 'fixture-key'))
        run = Mock()
        install(self.source, self.root, ask=ask, run=run)
        conf = self.root / 'etc/minis-linux'
        before = {p.name: p.read_bytes() for p in conf.iterdir()}
        self.assertEqual((conf / 'telegram.token').stat().st_mode & 0o777, 0o600)
        self.assertEqual(conf.stat().st_mode & 0o777, 0o700)
        state = self.root / 'var/lib/minis-linux-telegram'
        state.mkdir(parents=True)
        (state / 'history').write_text('old history')
        install(self.source, self.root, ask=ask, run=run)
        ask.assert_called_once()
        self.assertEqual({p.name: p.read_bytes() for p in conf.iterdir()}, before)
        self.assertEqual((state / 'history').read_text(), 'old history')
        self.assertTrue((self.root / 'opt/minis-linux/minis_skills.py').is_file())
        self.assertTrue((self.root / 'opt/minis-linux/skills/weekly-report/SKILL.md').is_file())
        self.assertEqual((self.root / 'opt/minis-linux/VERSION').read_bytes(), (self.source / 'VERSION').read_bytes())
        launcher = self.root / 'usr/local/bin/minis'
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
        self.assertIn('/opt/minis-linux/minis_cli.py', launcher.read_text())
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(['systemctl', 'enable', 'minis-linux-telegram.service'], calls)
        self.assertIn(['systemctl', 'restart', 'minis-linux-telegram.service'], calls)

    def test_interrupted_upgrade_keeps_old_version_and_recoverable_snapshot(self):
        from install import install, recover_pending, UNIT
        ask = Mock(return_value=('123', 'https://api.example.com/v1', 'model',
                                 '123:' + 'X' * 32, 'fixture-key'))
        install(self.source, self.root, ask=ask, run=Mock())
        app = self.root / 'opt/minis-linux'
        path = app / 'minis.py'
        version = app / 'VERSION'
        path.write_bytes(b'old code')
        version.write_text('0.0.0\n')

        def run(command, **kwargs):
            if command == ['systemctl', 'restart', UNIT]:
                self.assertEqual(version.read_text(), '0.0.0\n')
                raise SystemExit(99)
            return True

        with self.assertRaises(SystemExit):
            install(self.source, self.root, ask=ask, run=run)
        self.assertNotEqual(path.read_bytes(), b'old code')
        retry = Mock(return_value=True)
        self.assertTrue(recover_pending(self.root, run=retry))
        self.assertEqual(path.read_bytes(), b'old code')
        self.assertEqual(version.read_text(), '0.0.0\n')
        self.assertIn(['systemctl', 'start', UNIT], [call.args[0] for call in retry.call_args_list])
        self.assertFalse(recover_pending(self.root, run=retry))

    def test_interrupted_same_version_reinstall_still_rolls_back(self):
        from install import install, recover_pending, UNIT
        ask = Mock(return_value=('123', 'https://api.example.com/v1', 'model',
                                 '123:' + 'X' * 32, 'fixture-key'))
        install(self.source, self.root, ask=ask, run=Mock())
        app = self.root / 'opt/minis-linux/minis.py'
        app.write_bytes(b'old code with the same VERSION')

        def run(command, *, probe=False):
            if command == ['systemctl', 'enable', UNIT]:
                raise SystemExit(99)
            return True

        with self.assertRaises(SystemExit):
            install(self.source, self.root, ask=ask, run=run)
        self.assertNotEqual(app.read_bytes(), b'old code with the same VERSION')
        self.assertEqual(recover_pending(self.root, run=Mock(return_value=True)), 'rolled-back')
        self.assertEqual(app.read_bytes(), b'old code with the same VERSION')
        ask.assert_called_once()

    def test_interrupted_initial_install_rolls_back_and_keeps_credentials_for_retry(self):
        from install import install, recover_pending, UNIT, MANAGED
        ask = Mock(return_value=('123', 'https://api.example.com/v1', 'model',
                                 '123:' + 'X' * 32, 'fixture-key'))

        def run(command, *, probe=False):
            if command == ['systemctl', 'enable', UNIT]:
                raise SystemExit(99)
            return not probe

        with self.assertRaises(SystemExit):
            install(self.source, self.root, ask=ask, run=run)
        retry = Mock(return_value=True)  # enable may have taken effect before interruption
        self.assertEqual(recover_pending(self.root, run=retry), 'rolled-back')
        self.assertFalse(any((self.root / name).exists() for name in MANAGED))
        commands = [call.args[0] for call in retry.call_args_list]
        self.assertIn(['systemctl', 'disable', UNIT], commands)
        self.assertNotIn(['systemctl', 'start', UNIT], commands)
        install(self.source, self.root, ask=ask, run=Mock())
        self.assertEqual((self.root / 'etc/minis-linux/model.key').read_text(), 'fixture-key\n')
        ask.assert_called_once()

    def test_update_rechecks_installed_version_without_running_service_commands(self):
        from install import install
        ask = Mock(return_value=('123', 'https://api.example.com/v1', 'test/model',
                                 '123:' + 'X' * 32, 'fixture-key'))
        install(self.source, self.root, ask=ask, run=Mock())
        (self.root / 'opt/minis-linux/VERSION').write_text('9.0.0\n')
        run = Mock()
        install(self.source, self.root, ask=ask, run=run,
                upgrade_to=(self.source / 'VERSION').read_text().strip())
        run.assert_not_called()
        ask.assert_called_once()

    def test_newer_version_activates_with_same_credentials(self):
        from install import install
        ask = Mock(return_value=('123', 'https://api.example.com/v1', 'test/model',
                                 '123:' + 'X' * 32, 'fixture-key'))
        run = Mock()
        install(self.source, self.root, ask=ask, run=run)
        (self.root / 'opt/minis-linux/VERSION').write_text('0.0.0\n')
        run.reset_mock()
        version = (self.source / 'VERSION').read_text().strip()
        install(self.source, self.root, ask=ask, run=run, upgrade_to=version)
        self.assertEqual((self.root / 'opt/minis-linux/VERSION').read_text().strip(), version)
        self.assertEqual((self.root / 'etc/minis-linux/model.key').read_text(), 'fixture-key\n')
        self.assertIn(['systemctl', 'restart', 'minis-linux-telegram.service'],
                      [call.args[0] for call in run.call_args_list])
        ask.assert_called_once()

    def test_failed_upgrade_restores_previous_app_and_unit_and_restarts_old_service(self):
        from install import install, UNIT
        ask = Mock(return_value=('123', 'https://api.example.com/v1', 'test/model',
                                 '123:' + 'X' * 32, 'fixture-key'))
        install(self.source, self.root, ask=ask, run=Mock())
        app = self.root / 'opt/minis-linux/minis.py'
        unit = self.root / 'etc/systemd/system' / UNIT
        app.write_bytes(b'previous app')
        unit.write_bytes(b'previous unit')
        snapshots = []
        rate_limited = False

        def run(command, *, probe=False):
            nonlocal rate_limited
            if command == ['systemctl', 'reset-failed', UNIT]:
                self.assertEqual(unit.read_bytes(), b'previous unit')
                rate_limited = False
            if command in (['systemctl', 'restart', UNIT], ['systemctl', 'start', UNIT]):
                snapshots.append((app.read_bytes(), unit.read_bytes()))
                if len(snapshots) == 1:
                    rate_limited = True
                    raise subprocess.CalledProcessError(1, command)
                if rate_limited:
                    raise subprocess.CalledProcessError(1, command)
            return True

        with self.assertRaises(subprocess.CalledProcessError):
            install(self.source, self.root, ask=ask, run=run)
        self.assertEqual(len(snapshots), 2)
        self.assertNotEqual(snapshots[0], (b'previous app', b'previous unit'))
        self.assertEqual(snapshots[1], (b'previous app', b'previous unit'))

    def test_unit_waits_for_application_readiness(self):
        from install import UNIT
        unit = (self.source / 'systemd' / UNIT).read_text()
        self.assertIn('\nType=notify\n', unit)
        self.assertIn('\nNotifyAccess=main\n', unit)

    def test_journal_cleanup_sync_error_does_not_roll_back_a_completed_upgrade(self):
        import install as installer
        ask = Mock(return_value=('123', 'https://api.example.com/v1', 'model',
                                 '123:' + 'X' * 32, 'fixture-key'))
        installer.install(self.source, self.root, ask=ask, run=Mock())
        version = self.root / 'opt/minis-linux/VERSION'
        version.write_text('0.0.0\n')
        sync = installer.sync_directory
        journal = self.root / installer.PENDING
        durable = None
        unlinked = []
        unlink = Path.unlink

        def record_unlink(path, *args, **kwargs):
            if path == journal:
                unlinked.append(path.read_bytes())
            return unlink(path, *args, **kwargs)

        def flaky_sync(path):
            nonlocal durable
            if path == journal.parent and unlinked and not journal.exists():
                raise OSError('fixture: directory sync failed during journal cleanup')
            sync(path)
            if path == journal.parent and journal.exists():
                durable = journal.read_bytes()

        with redirect_stderr(io.StringIO()), patch.object(Path, 'unlink', record_unlink), \
                patch.object(installer, 'sync_directory', side_effect=flaky_sync):
            installer.install(self.source, self.root, ask=ask, run=Mock(return_value=True))
        self.assertIsNotNone(durable)
        self.assertEqual(unlinked, [durable])
        self.assertFalse(journal.exists())

        # Model a power loss bringing back the last journal entry that was
        # present before unlink, when the cleanup fsync did not succeed.
        installer.atomic_write(journal, durable, 0o600)
        run = Mock()
        installer.recover_pending(self.root, run=run)
        self.assertEqual(version.read_bytes(), (self.source / 'VERSION').read_bytes())
        run.assert_not_called()
        self.assertFalse(journal.exists())

    def test_journal_unlink_error_does_not_undo_a_committed_same_version_reinstall(self):
        import install as installer
        ask = Mock(return_value=('123', 'https://api.example.com/v1', 'model',
                                 '123:' + 'X' * 32, 'fixture-key'))
        installer.install(self.source, self.root, ask=ask, run=Mock())
        journal = self.root / installer.PENDING
        app = journal.with_name('minis.py')
        app.write_bytes(b'older build, same version')
        unlink = Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path == journal:
                raise PermissionError('fixture: journal unlink failed')
            return unlink(path, *args, **kwargs)

        with redirect_stderr(io.StringIO()), patch.object(Path, 'unlink', fail_unlink):
            installer.install(self.source, self.root, ask=ask, run=Mock())
        self.assertTrue(journal.is_file())
        run = Mock()
        self.assertEqual(installer.recover_pending(self.root, run=run), 'activated')
        run.assert_not_called()
        self.assertEqual(app.read_bytes(), (self.source / app.name).read_bytes())
        self.assertFalse(journal.exists())

    def test_initial_install_recovers_when_version_publication_is_interrupted(self):
        import install as installer
        for renamed in (False, True):
            with self.subTest(renamed=renamed), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = Path(tmp)
                journal = root / installer.PENDING
                version = journal.with_name('VERSION')
                ask = Mock(return_value=('123', 'https://api.example.com/v1', 'model',
                                         '123:' + 'X' * 32, 'fixture-key'))
                replace = Path.replace

                def interrupt_replace(path, target):
                    if target == version:
                        if renamed:
                            replace(path, target)
                        raise SystemExit(99)
                    return replace(path, target)

                with patch.object(Path, 'replace', interrupt_replace), self.assertRaises(SystemExit):
                    installer.install(self.source, root, ask=ask, run=Mock())
                self.assertEqual(version.exists(), renamed)
                self.assertTrue(journal.exists())
                run = Mock()
                self.assertEqual(installer.recover_pending(root, run=run), 'activated')
                run.assert_not_called()
                self.assertEqual(version.read_bytes(), (self.source / 'VERSION').read_bytes())
                self.assertEqual(journal.with_name('minis.py').read_bytes(), (self.source / 'minis.py').read_bytes())
                self.assertFalse(journal.exists())

    def test_activation_record_failure_recovers_the_journal_that_survived(self):
        import install as installer
        for renamed in (False, True):
            with self.subTest(renamed=renamed), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = Path(tmp)
                ask = Mock(return_value=('123', 'https://api.example.com/v1', 'model',
                                         '123:' + 'X' * 32, 'fixture-key'))
                installer.install(self.source, root, ask=ask, run=Mock())
                journal = root / installer.PENDING
                app, version = journal.with_name('minis.py'), journal.with_name('VERSION')
                app.write_bytes(b'old code')
                version.write_bytes(b'0.0.0\n')
                replace, sync = Path.replace, installer.sync_directory
                journal_renames = []

                def replace_journal(path, target):
                    if target == journal:
                        journal_renames.append(path)
                        if len(journal_renames) == 2 and not renamed:
                            raise OSError('fixture: activation record rename failed')
                    return replace(path, target)

                def sync_journal(path):
                    if path == journal.parent and len(journal_renames) == 2:
                        raise OSError('fixture: activation record fsync failed after rename')
                    return sync(path)

                with patch.object(Path, 'replace', replace_journal), \
                        patch.object(installer, 'sync_directory', side_effect=sync_journal), \
                        self.assertRaisesRegex(OSError, '--recover'):
                    installer.install(self.source, root, ask=ask, run=Mock())
                self.assertEqual(version.read_bytes(), b'0.0.0\n')
                run = Mock(return_value=True)
                result = installer.recover_pending(root, run=run)
                if renamed:
                    self.assertEqual(result, 'activated')
                    self.assertEqual(version.read_bytes(), (self.source / 'VERSION').read_bytes())
                    self.assertEqual(app.read_bytes(), (self.source / 'minis.py').read_bytes())
                    run.assert_not_called()
                else:
                    self.assertEqual(result, 'rolled-back')
                    self.assertEqual(version.read_bytes(), b'0.0.0\n')
                    self.assertEqual(app.read_bytes(), b'old code')
                    run.assert_any_call(['systemctl', 'start', installer.UNIT])
                self.assertFalse(journal.exists())

    def test_rollback_starts_restored_unit_even_if_systemd_unloaded_it(self):
        from install import activate, UNIT
        path = self.root / UNIT
        path.write_bytes(b'old unit')
        commands = []

        def run(command, *, probe=False):
            action = command[1]
            commands.append(action)
            if action == 'restart':
                raise subprocess.CalledProcessError(1, command)
            if action == 'reset-failed':
                if probe:
                    return False
                raise subprocess.CalledProcessError(1, command, stderr='Unit not loaded')
            if action == 'start':
                self.assertEqual(path.read_bytes(), b'old unit')
            return action != 'is-enabled'

        with self.assertRaises(subprocess.CalledProcessError):
            activate({path: (b'new unit', 0o644)}, run=run)
        self.assertEqual(commands[-3:], ['reset-failed', 'start', 'is-active'])

    def test_application_can_signal_readiness(self):
        from minis_telegram import notify_ready
        name = f'\0minis-test-{os.getpid()}'
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(name)
            server.settimeout(2)
            with patch.dict(os.environ, {'NOTIFY_SOCKET': '@' + name[1:]}):
                notify_ready()
            self.assertEqual(server.recv(64), b'READY=1')

    def test_credential_files_do_not_require_secret_input(self):
        from install import ask_config
        token = self.root / 'telegram.token'
        key = self.root / 'model.key'
        for path, value in ((token, '123:' + 'X' * 32), (key, 'fixture-key')):
            path.write_text(value + '\n', encoding='ascii')
            path.chmod(0o600)
        with patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=['123', 'https://api.example.com/v1', 'test/model']), \
                patch('minis_cli.model_catalog', return_value=[]), \
                patch('install.ask_secret') as secret_prompt:
            values = ask_config(self.root)
            secret_prompt.assert_not_called()
        self.assertEqual(values[3:], ('123:' + 'X' * 32, 'fixture-key'))

    def test_first_install_picks_model_from_gateway_with_supplied_key(self):
        from install import ask_config, config_bytes
        requests = []

        class Catalog(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                body = json.dumps({'data': [{'id': 'sample/a'}, {'id': 'sample/b'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        token = '123:' + 'X' * 32
        with HTTPServer(('127.0.0.1', 0), Catalog) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}/v1'
                with patch('sys.stdin.isatty', return_value=True), redirect_stdout(io.StringIO()) as output, \
                        patch('builtins.input', side_effect=['123', base, '2']), \
                        patch('install.ask_secret', side_effect=['fixture-key', token]):
                    config, _, _ = config_bytes(ask_config())
            finally:
                server.shutdown()
                thread.join(2)
        self.assertEqual(requests, [('/v1/models', 'Bearer fixture-key')])
        self.assertIn(b'MINIS_MODEL="sample/b"', config)
        self.assertNotIn('fixture-key', output.getvalue())
        self.assertNotIn(token, output.getvalue())

    def test_cancelling_model_selection_does_not_write_install_files(self):
        from install import ask_config, install
        run = Mock()
        with patch('sys.stdin.isatty', return_value=True), redirect_stdout(io.StringIO()), \
                patch('builtins.input', side_effect=['123', 'https://api.example.com/v1', '']), \
                patch('install.ask_secret', return_value='fixture-key'), \
                patch('minis_cli.model_catalog', return_value=['sample/a']):
            with self.assertRaisesRegex(ValueError, '取消'):
                install(self.source, self.root, ask=ask_config, run=run)
        self.assertFalse((self.root / 'etc/minis-linux').exists())
        self.assertFalse((self.root / 'opt').exists())
        run.assert_not_called()

    def test_partial_config_refused_without_overwriting(self):
        from install import install
        conf = self.root / 'etc/minis-linux'
        conf.mkdir(parents=True, mode=0o700)
        key = conf / 'model.key'
        key.write_text('keep me')
        with self.assertRaisesRegex(ValueError, '不完整'):
            install(self.source, self.root, ask=Mock(), run=Mock())
        self.assertEqual(key.read_text(), 'keep me')
        self.assertFalse((self.root / 'opt').exists())

    def test_bad_new_configuration_stops_before_writes(self):
        from install import install
        ask = Mock(return_value=('@someone', 'https://api.example.com/v1', 'model', 'bad', 'fixture'))
        run = Mock()
        with self.assertRaises(ValueError):
            install(self.source, self.root, ask=ask, run=run)
        self.assertEqual(list(self.root.iterdir()), [])
        run.assert_not_called()

    def test_preflight_uses_existing_unit_without_contacting_network(self):
        from install import preflight
        run = Mock()
        preflight(self.source, run=run)
        run.assert_called_once_with(['systemd-analyze', 'verify', str(self.source / 'systemd/minis-linux-telegram.service')])


if __name__ == '__main__':
    unittest.main()
