from pathlib import Path
import os
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


class InstallTest(unittest.TestCase):
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
        cleanup_errors = []

        def flaky_sync(path):
            if path == version.parent and not (self.root / installer.PENDING).exists():
                cleanup_errors.append(path)
                raise OSError('fixture: directory sync failed during journal cleanup')
            return sync(path)

        run = Mock(return_value=True)
        with patch.object(installer, 'sync_directory', side_effect=flaky_sync):
            installer.install(self.source, self.root, ask=ask, run=run)
        self.assertEqual(len(cleanup_errors), 1)
        self.assertEqual(version.read_bytes(), (self.source / 'VERSION').read_bytes())
        self.assertNotIn(['systemctl', 'stop', installer.UNIT], [call.args[0] for call in run.call_args_list])

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
                patch('getpass.getpass') as secret_prompt:
            values = ask_config(self.root)
            secret_prompt.assert_not_called()
        self.assertEqual(values[3:], ('123:' + 'X' * 32, 'fixture-key'))

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
