"""Offline integration tests for Bot's opt-in features."""
import json
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from minis import Provider, read_history, save_history
from minis_input import Prompt
from minis_media import MediaError
from minis_telegram import Bot, TelegramError
from minis_web import _WebContext


class Surface:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.updates = []

    def send(self, owner, text):
        if self.fail:
            raise TelegramError('sendMessage', 429)
        self.sent.append((owner, text))

    def updates_since(self, offset):
        return self.updates


class ScriptedModel:
    model = 'fixture'

    def __init__(self):
        self.requests = []
        self.responses = []

    def reply(self, messages, *, on_delta=None):
        self.requests.append(messages.copy())
        response = self.responses.pop(0) if self.responses else {'content': '回答'}
        if isinstance(response, Exception):
            raise response
        if on_delta and response.get('content'):
            on_delta(response['content'])
        return response


def tool(function_name, **args):
    return {'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {
        'name': function_name, 'arguments': json.dumps(args)}}]}


class BotFeaturesTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.surface, self.model = Surface(), ScriptedModel()
        self.bot = self.make_bot()

    def make_bot(self, *, owner=42, bot_id='17', channel='Telegram'):
        return Bot(self.home, owner, bot_id, self.surface, self.model,
                   channel=channel, features=True)

    def send_command(self, command):
        reply = self.bot.local_command(command)
        self.assertIsNotNone(reply)
        self.bot.send_reply(reply)
        return self.surface.sent[-1][1]

    def proposal_id(self, preview):
        match = re.search(r'/approve ([a-f0-9]{32})', preview)
        self.assertIsNotNone(match)
        assert match is not None
        return match[1]

    def test_legacy_history_is_session_one_and_new_keeps_memory_and_old_chat(self):
        self.bot.handle('记住：旧偏好')
        self.bot.handle('旧聊天')
        self.assertIn('2', self.send_command('/new Project 2'))
        self.bot.handle('新聊天')
        self.assertIn('旧偏好', self.model.requests[-1][0]['content'])
        self.assertNotIn('旧聊天', repr(self.model.requests[-1][1:]))
        self.assertIn('旧聊天', self.send_command('/history 旧聊天'))
        self.assertIn('旧聊天', read_history(self.bot.history)[0]['content'])
        self.assertIn('Project 2', self.send_command('/sessions'))
        self.send_command('/session 1')
        self.bot = self.make_bot()
        self.bot.handle('回到旧聊天')
        self.assertEqual(self.model.requests[-1][1]['content'], '旧聊天')
        other_owner = self.make_bot(owner=43)
        other_bot = self.make_bot(bot_id='18')
        for other in (other_owner, other_bot):
            self.assertNotIn('Project 2', other.local_command('/sessions'))
            self.assertNotIn('旧聊天', other.local_command('/history 旧聊天'))

    def test_commit_pins_original_history_even_after_session_switch(self):
        _, history = self.bot.generate('固定会话')
        self.bot.local_command('/new')
        self.bot.commit_turn(history, [])
        self.assertEqual(read_history(self.bot.history)[0]['content'], '固定会话')
        self.assertFalse(self.bot.sessions.active_path.exists())

    def test_proposals_are_pure_until_commit_and_need_a_delivered_preview(self):
        action = tool('propose_create_file', path='note.txt', content='draft')
        self.model.responses = [action]
        proposals = []
        _, history = self.bot.generate('请草拟新文件', proposals=proposals)
        self.assertFalse(self.bot.history.exists())
        self.assertFalse((self.home / '.minis-actions').exists())
        self.assertEqual(len(proposals), 1)
        previews = self.bot.commit_turn(history, proposals)
        ident = self.proposal_id(previews[0])
        self.assertNotIn(ident, repr(self.model.requests))
        self.assertNotIn('draft', self.bot.local_command('/actions'))
        path = self.bot.workspace / 'note.txt'
        self.bot.local_command('/approve ' + ident)
        self.assertFalse(path.exists())
        self.surface.fail = True
        with self.assertRaises(TelegramError):
            self.bot.send_reply(previews[0])
        self.surface.fail = False
        self.bot.local_command('/approve ' + ident)
        self.assertFalse(path.exists())
        self.send_command('/preview ' + ident)
        self.bot.local_command('/approve  ' + ident)
        self.bot.local_command('/approve ' + ident + ' extra')
        self.bot.local_command('/approve ' + ident + ' ')
        self.assertFalse(path.exists())
        self.bot.local_command('/approve ' + ident)
        self.assertEqual(path.read_text(), 'draft')
        self.assertIn('applied', self.bot.local_command('/audit'))
        path.unlink()
        self.bot.local_command('/approve ' + ident)
        self.assertFalse(path.exists())

    def test_restart_requires_repreview_for_durable_proposals(self):
        self.model.responses = [tool('propose_create_file', path='pending.txt', content='draft')]
        self.bot.handle('准备文件')
        ident = self.proposal_id(self.surface.sent[-1][1])
        self.bot = self.make_bot()
        self.send_command('/approve ' + ident)
        path = self.bot.workspace / 'pending.txt'
        self.assertFalse(path.exists())
        self.send_command('/preview ' + ident)
        self.send_command('/approve ' + ident)
        self.assertTrue(path.is_file())

    def test_large_preview_cannot_be_truncated_and_approved(self):
        self.model.responses = [tool('propose_create_file', path='large.txt', content='x' * 32768)]
        self.bot.handle('准备大文件')
        preview = self.surface.sent[-1][1]
        self.assertIn('预览超过显示上限', preview)
        match = re.search(r'/cancel ([a-f0-9]{32})', preview)
        assert match is not None
        ident = match[1]
        self.send_command('/approve ' + ident)
        self.assertFalse((self.bot.workspace / 'large.txt').exists())

    def test_edit_refuses_content_changed_since_preview_and_delete_can_be_restored(self):
        path = self.bot.workspace / 'existing.txt'
        path.write_text('before\n')
        self.model.responses = [tool('propose_edit_file', path=path.name, content='after\n')]
        self.bot.handle('编辑文件')
        preview = self.surface.sent[-1][1]
        self.assertIn('- "before\\n"\n+ "after\\n"', preview)
        ident = self.proposal_id(preview)
        path.write_text('changed since preview\n')
        self.send_command('/approve ' + ident)
        self.assertEqual(path.read_text(), 'changed since preview\n')
        self.model.responses = [tool('propose_delete_file', path=path.name)]
        self.bot.handle('移到回收站')
        ident = self.proposal_id(self.surface.sent[-1][1])
        self.send_command('/approve ' + ident)
        self.assertFalse(path.exists())
        self.assertIn(ident, self.send_command('/trash'))
        restore = self.proposal_id(self.send_command('/restore ' + ident))
        self.send_command('/approve ' + restore)
        self.assertEqual(path.read_text(), 'changed since preview\n')

    def test_failed_turn_or_failed_history_save_does_not_stage_proposals(self):
        self.model.responses = [tool('propose_memory', text='do not save'), ValueError('offline failure')]
        proposals = []
        _, history = self.bot.generate('failed turn', proposals=proposals)
        self.assertIsNone(history)
        self.assertEqual(proposals, [])
        self.assertFalse((self.home / '.minis-actions').exists())
        self.model.responses = [tool('propose_memory', text='do not save')]
        _, history = self.bot.generate('success until save', proposals=proposals)
        with patch('minis_telegram.save_history', side_effect=OSError):
            with self.assertRaises(OSError):
                self.bot.commit_turn(history, proposals)
        self.assertFalse((self.home / '.minis-actions').exists())

    def test_proposed_memory_is_saved_only_in_matching_session_after_approval(self):
        self.model.responses = [tool('propose_memory', text='周报用中文')]
        self.bot.handle('记忆建议')
        ident = self.proposal_id(self.surface.sent[-1][1])
        self.assertNotIn('周报用中文', self.bot.local_command('查看记忆'))
        self.send_command('/new')
        self.bot.local_command('/approve ' + ident)
        self.assertNotIn('周报用中文', self.bot.local_command('查看记忆'))
        self.send_command('/session 1')
        self.bot.local_command('/approve ' + ident)
        self.assertIn('1. 周报用中文', self.bot.local_command('查看记忆'))
        self.bot.local_command('忘记第 1 条')
        self.bot.local_command('记住：另一条')
        self.assertIn('2. 另一条', self.bot.local_command('查看记忆'))

    def test_memory_file_and_directory_are_synced_before_applied_audit(self):
        self.model.responses = [tool('propose_memory', text='fixture preference')]
        self.bot.handle('propose memory')
        ident = self.proposal_id(self.surface.sent[-1][1])
        synced = []
        sync = os.fsync

        def track(fd):
            path = Path(os.readlink(f'/proc/self/fd/{fd}'))
            if path.name == '.state.tmp' and self.bot.memory.exists():
                audit = json.loads(path.read_bytes())['audit']
                if audit[-1][1]['status'] == 'applied':
                    self.assertEqual(synced[-1], self.bot.memory.parent)
                    self.assertTrue(synced[-2].name.startswith('.' + self.bot.memory.name + '.'))
            synced.append(path)
            sync(fd)

        with patch('minis.os.fsync', side_effect=track):
            self.assertIn('已执行', self.send_command('/approve ' + ident))
        self.assertIn(self.bot.memory.parent, synced)
        self.assertIn('applied', self.send_command('/audit'))

    def test_memory_sync_failure_is_not_applied_or_retried(self):
        self.model.responses = [tool('propose_memory', text='fixture preference')]
        self.bot.handle('propose memory')
        ident = self.proposal_id(self.surface.sent[-1][1])
        sync = os.fsync

        def fail_memory_dir(fd):
            if Path(os.readlink(f'/proc/self/fd/{fd}')) == self.bot.memory.parent:
                raise OSError('fixture sync failure')
            sync(fd)

        with patch('minis.os.fsync', side_effect=fail_memory_dir):
            self.assertNotIn('已执行', self.send_command('/approve ' + ident))
        self.assertNotIn('applied', self.send_command('/audit'))
        with patch.object(self.bot, '_remember') as remember:
            self.send_command('/approve ' + ident)
            remember.assert_not_called()

    def test_skill_import_uses_workspace_file_and_merges_with_bundled(self):
        source = '---\nname: imported\ndescription: Example local skill\n---\nSome steps\n'
        (self.bot.workspace / 'import.md').write_text(source)
        self.assertNotIn('imported', self.bot.local_command('/skills'))
        self.assertNotIn('/approve ', self.bot.local_command('/skill import ../import.md'))
        ident = self.proposal_id(self.send_command('/skill import import.md'))
        self.assertNotIn('imported', self.bot.local_command('/skills'))
        self.send_command('/approve ' + ident)
        entries = self.bot.local_command('/skills')
        self.assertIn('imported', entries)
        self.assertIn('weekly-report', entries)
        self.model.responses = [tool('skill_view', name='imported')]
        self.bot.handle('读技能')
        self.assertIn('Some steps', self.model.requests[-1][-1]['content'])
        self.assertNotIn('Some steps', self.model.requests[-1][0]['content'])

    def test_import_and_draft_reject_configured_and_bundled_names(self):
        configured = self.home / 'configured'
        bundled = self.home / 'bundled'
        self.bot.skills_dir = configured
        document = '---\nname: {name}\ndescription: Fixture\n---\nPlain steps\n'
        for root in (configured, bundled):
            name = root.name + '-notes'
            (root / name).mkdir(parents=True)
            source = document.format(name=name)
            (root / name / 'SKILL.md').write_text(source)
            (self.bot.workspace / 'import.md').write_text(source)
            with patch('minis_telegram._BUNDLED_SKILLS', bundled):
                self.assertNotIn('/approve ', self.send_command('/skill import import.md'))
                self.model.responses = [tool('propose_skill', name=name, description='Fixture', content='Plain text')]
                self.bot.handle('draft skill')
                self.assertIn('未建立操作提议', self.surface.sent[-1][1])
        self.assertFalse(self.bot.skill_overlay.exists())

    def test_skill_approval_rechecks_a_name_added_after_preview(self):
        self.bot.skills_dir = self.home / 'configured'
        source = '---\nname: notes\ndescription: Fixture\n---\nPlain steps\n'
        (self.bot.workspace / 'import.md').write_text(source)
        ident = self.proposal_id(self.send_command('/skill import import.md'))
        configured = self.bot.skills_dir / 'notes'
        configured.mkdir(parents=True)
        (configured / 'SKILL.md').write_text(source)
        self.assertNotIn('已执行', self.send_command('/approve ' + ident))
        self.assertFalse((self.bot.skill_overlay / 'notes').exists())
        (configured / 'SKILL.md').unlink()
        configured.rmdir()
        self.send_command('/approve ' + ident)
        self.assertFalse((self.bot.skill_overlay / 'notes').exists())

    def test_web_uses_current_prompt_only_and_returns_a_separate_footer(self):
        save_history(self.bot.history, [{'role': 'user', 'content': 'https://old.invalid/'}])
        self.bot.local_command('记住：https://memory.invalid/')
        web = _WebContext('untrusted fixture excerpt', [(1, 'https://source.invalid/')])
        with patch('minis_telegram.prepare_web', return_value=web) as lookup:
            reply, history = self.bot.generate('搜索：新主题')
            lookup.assert_called_once_with('搜索：新主题')
            self.assertIn('untrusted fixture excerpt', self.model.requests[-1][0]['content'])
            self.assertIn(web.source_footer, reply)
            self.assertNotIn(web.source_footer, repr(history))
            self.assertNotIn('untrusted fixture excerpt', repr(history))
        with patch('minis_telegram.prepare_web') as lookup:
            self.bot.handle(Prompt('/new https://pasted.invalid/', allow_commands=False))
            self.bot.handle(Prompt('记住：https://pasted.invalid/', allow_commands=False))
            self.bot.generate('attachment content https://attachment.invalid/', allow_web=False)
            lookup.assert_not_called()
        self.assertEqual(len(self.bot.sessions.list()), 1)
        self.assertNotIn('pasted.invalid', self.bot.local_command('查看记忆'))

    def test_command_bypass_and_callbacks_for_synchronous_handle(self):
        fragments, models = [], []
        self.bot.handle('/new', allow_commands=False, on_delta=fragments.append, on_model=models.append)
        self.assertEqual(fragments, ['回答'])
        self.assertEqual(models, ['fixture'])
        self.assertEqual(read_history(self.bot.history)[0]['content'], '/new')

    def test_provider_advertises_proposals_but_not_direct_writes(self):
        provider = Provider('https://model.invalid', 'fixture-key', 'fixture')
        self.bot.provider = provider
        tools = {t['function']['name'] for t in self.bot.provider.tools}
        self.assertIn('skill_view', tools)
        self.assertIn('propose_create_file', tools)
        self.assertNotIn('write_file', tools)
        self.assertNotIn('send_document', tools)
        self.assertNotIn('propose_create_file', {t['function']['name'] for t in provider.tools})
        self.model.responses = [tool('write_file', path='bad.txt', content='never')]
        self.bot.provider = self.model
        self.bot.handle('attempt an unauthorized write')
        self.assertFalse((self.bot.workspace / 'bad.txt').exists())

    def test_terminal_export_is_confirmed_frozen_redacted_and_non_overwriting(self):
        self.bot = self.make_bot(channel='终端')
        self.bot.handle('api_key=fixture-secret\n![not an image](https://example.invalid/img)', allow_web=False)
        preview = self.send_command('/export')
        ident = self.proposal_id(preview)
        exports = self.home / 'exports'
        self.assertIn(str(exports), preview)
        self.assertFalse(exports.exists())
        self.assertEqual(list(self.bot.workspace.iterdir()), [])
        self.bot.handle('after snapshot')
        reply = self.send_command('/approve ' + ident)
        files = list(exports.glob('*.md'))
        self.assertEqual(len(files), 1)
        self.assertIn(str(files[0]), reply)
        self.assertEqual(stat.S_IMODE(exports.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        markdown = files[0].read_text()
        self.assertIn('    ![not an image]', markdown)
        self.assertNotIn('fixture-secret', markdown)
        self.assertNotIn('after snapshot', markdown)
        self.send_command('/approve ' + ident)
        self.assertEqual(list(exports.iterdir()), files)
        self.assertEqual(list(self.bot.workspace.iterdir()), [])

    def test_terminal_export_preserves_existing_destination(self):
        self.bot = self.make_bot(channel='终端')
        ident = self.proposal_id(self.send_command('/export'))
        exports = self.home / 'exports'
        exports.mkdir(mode=0o700)
        existing = exports / f'minis-session-1-{ident}.md'
        existing.write_text('keep me')
        self.assertNotIn('已导出', self.send_command('/approve ' + ident))
        self.assertEqual(list(exports.iterdir()), [existing])
        self.assertEqual(existing.read_text(), 'keep me')
        self.assertIn('failed', self.send_command('/audit'))

    def test_terminal_export_syncs_private_file_and_directory_before_applied(self):
        self.bot = self.make_bot(channel='终端')
        ident = self.proposal_id(self.send_command('/export'))
        exports = self.home / 'exports'
        final = exports / f'minis-session-1-{ident}.md'
        synced = []
        sync = os.fsync

        def track(fd):
            path = Path(os.readlink(f'/proc/self/fd/{fd}'))
            if path.parent == exports:
                self.assertFalse(final.exists())
                self.assertIn(b'# Minis', path.read_bytes())
            if path == exports:
                self.assertTrue(final.is_file())
                self.assertEqual(synced[-2], self.home)
                self.assertEqual(synced[-1].parent, exports)
            if path.name == '.state.tmp' and final.exists():
                self.assertIn(exports, synced)
            synced.append(path)
            sync(fd)

        with patch('minis_actions.os.fsync', side_effect=track):
            self.assertIn('已导出', self.send_command('/approve ' + ident))
        self.assertIn(f'applied · {ident}', self.send_command('/audit'))

    def test_terminal_export_directory_sync_failure_is_uncertain_and_not_retried(self):
        self.bot = self.make_bot(channel='终端')
        ident = self.proposal_id(self.send_command('/export'))
        sync = os.fsync

        def fail_export_dir(fd):
            if Path(os.readlink(f'/proc/self/fd/{fd}')) == self.home / 'exports':
                raise OSError('fixture sync failure')
            sync(fd)

        with patch('minis_actions.os.fsync', side_effect=fail_export_dir):
            self.assertIn('无法确认', self.send_command('/approve ' + ident))
        self.assertIn(f'unknown · {ident}', self.send_command('/audit'))
        with patch('minis_telegram.publish_export') as publish:
            self.send_command('/approve ' + ident)
            publish.assert_not_called()

    def test_telegram_export_audit_distinguishes_rejection_and_uncertainty(self):
        for code, status in ((400, 'failed'), (None, 'unknown'), (502, 'unknown')):
            with self.subTest(code=code):
                ident = self.proposal_id(self.send_command('/export'))
                error = MediaError('fixture failure', code=code)
                with patch('minis_telegram.send_markdown', side_effect=error) as send:
                    reply = self.send_command('/approve ' + ident)
                    self.assertIn('拒绝' if status == 'failed' else '无法确认', reply)
                    self.send_command('/approve ' + ident)
                    send.assert_called_once()
                self.assertIn(f'{status} · {ident}', self.send_command('/audit'))

    def test_export_reports_audit_failure_after_effect_and_never_retries(self):
        ident = self.proposal_id(self.send_command('/export'))
        queue_type = type(self.bot._queue(self.bot.history))
        save = queue_type._save

        def fail_final_audit(fd, state):
            if state['audit'][-1][1]['status'] == 'applied':
                raise OSError('fixture audit failure')
            save(fd, state)

        with patch('minis_telegram.send_markdown', return_value={}) as send, patch.object(
                queue_type, '_save', side_effect=fail_final_audit):
            reply = self.send_command('/approve ' + ident)
            self.send_command('/approve ' + ident)
            send.assert_called_once()
        self.assertIn('已发送', reply)
        self.assertIn('审计', reply)
        self.assertIn(f'started · {ident}', self.send_command('/audit'))
        self.assertNotIn(f'applied · {ident}', self.send_command('/audit'))

    def test_telegram_export_requires_approval_and_is_owner_only_and_one_shot(self):
        self.bot.handle('password=fixture-secret')
        ident = self.proposal_id(self.send_command('/export 1'))
        with patch('minis_telegram.send_markdown', side_effect=MediaError('fixture failure')) as send:
            self.assertEqual(send.call_count, 0)
            date = int(time.time())
            message = {'from': {'id': 43, 'is_bot': False}, 'chat': {'type': 'private', 'id': 43},
                       'date': date, 'text': '/approve ' + ident}
            self.surface.updates = [dict(update_id=1, message=message)]
            self.bot.poll_once()
            self.assertEqual(send.call_count, 0)
            message.update({'from': {'id': 42, 'is_bot': False},
                            'chat': {'type': 'private', 'id': 42},
                            'text': '/approve ' + ident + ' '})
            self.surface.updates[0]['update_id'] = 2
            self.bot.poll_once()
            self.assertEqual(send.call_count, 0)
            self.send_command('/approve ' + ident)
            self.send_command('/approve ' + ident)
            send.assert_called_once()
            telegram, owner, document = send.call_args.args
            self.assertIs(telegram, self.surface)
            self.assertEqual(owner, 42)
            self.assertNotIn('fixture-secret', document)
        self.assertEqual(list(self.bot.workspace.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
