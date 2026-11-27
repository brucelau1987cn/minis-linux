"""Offline integration tests for Bot's opt-in features."""
import json
from pathlib import Path
import re
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
        self.assertIn('-before\n+after', preview)
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
        self.bot.handle('api_key=fixture-secret\n![not an image](https://example.invalid/img)')
        ident = self.proposal_id(self.send_command('/export'))
        self.assertEqual(list(self.bot.workspace.iterdir()), [])
        self.bot.handle('after snapshot')
        self.send_command('/approve ' + ident)
        files = list(self.bot.workspace.glob('*.md'))
        self.assertEqual(len(files), 1)
        markdown = files[0].read_text()
        self.assertIn('    ![not an image]', markdown)
        self.assertNotIn('fixture-secret', markdown)
        self.assertNotIn('after snapshot', markdown)
        self.send_command('/approve ' + ident)
        self.assertEqual(list(self.bot.workspace.iterdir()), files)

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
