import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


def message(seq, text='你好', owner=123, kind='private', **extra):
    return {'update_id': seq, 'message': {
        'date': int(time.time()), 'from': {'id': owner, 'is_bot': False},
        'chat': {'id': owner, 'type': kind}, 'text': text, **extra}}


class FakeTelegram:
    def __init__(self, updates):
        self.updates = updates
        self.offsets = []
        self.sent = []

    def updates_since(self, offset):
        self.offsets.append(offset)
        return self.updates

    def send(self, owner, text):
        self.sent.append((owner, text))


class FakeProvider:
    def __init__(self, *replies):
        self.messages = []
        self.replies = iter(replies)

    def reply(self, messages):
        self.messages.append(list(messages))
        return next(self.replies)


class TelegramTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def bot(self, telegram, provider, owner=123):
        from minis_telegram import Bot
        return Bot(self.home, owner, '1000', telegram, provider)

    def test_only_allowlisted_unforwarded_private_text_is_processed(self):
        telegram = FakeTelegram([
            message(1, owner=124), message(2, kind='group'),
            message(3, forward_origin={'type': 'user'}),
            message(4, chat={'id': 124, 'type': 'private'}),
            message(5, sender_chat={'id': 123}),
            message(6, date=int(time.time()) - 3601),
            message(7, '/start'), message(8, '处理这条'),
        ])
        model = FakeProvider({'content': '收到'})
        self.bot(telegram, model).poll_once()
        self.assertEqual(len(model.messages), 1)
        self.assertEqual(model.messages[0][-1]['content'], '处理这条')
        self.assertEqual([x[0] for x in telegram.sent], [123, 123])
        self.assertEqual(telegram.sent[-1][1], '收到')

    def test_persisted_offset_and_conversation_continue_then_reset(self):
        first = FakeTelegram([message(12, '第一轮')])
        self.bot(first, FakeProvider({'content': '答一'})).poll_once()
        second = FakeTelegram([message(12), message(13, '第二轮'), message(14, '/new'), message(15, '第三轮')])
        model = FakeProvider({'content': '答二'}, {'content': '答三'})
        self.bot(second, model).poll_once()
        self.assertEqual(second.offsets, [13])
        self.assertEqual([x['content'] for x in model.messages[0][1:]], ['第一轮', '答一', '第二轮'])
        self.assertEqual([x['content'] for x in model.messages[1][1:]], ['第三轮'])

    def test_time_and_disk_commands_only_answer_the_owner_without_a_model(self):
        client = FakeTelegram([message(1, '/time', owner=456), message(2, '/disk', kind='group'),
                               message(3, '/time'), message(4, '/disk')])
        model = FakeProvider()
        bot = self.bot(client, model)
        bot.poll_once()
        self.assertEqual(model.messages, [])
        self.assertEqual(len(client.sent), 2)
        self.assertIn('本机时间：', client.sent[0][1])
        self.assertIn('工作区所在文件系统：可用', client.sent[1][1])
        self.assertFalse(bot.history.exists())

    def test_owner_change_does_not_inherit_conversation(self):
        self.bot(FakeTelegram([message(20, '私密历史')]), FakeProvider({'content': '私密回答'})).poll_once()
        other = FakeProvider({'content': '答'})
        self.bot(FakeTelegram([message(21, owner=456)]), other, owner=456).poll_once()
        self.assertEqual(len(other.messages[0]), 2)

    def test_explicit_memory_survives_reset_and_restart_then_can_be_forgotten(self):
        first = FakeTelegram([message(100, '记住：周报用中文'), message(101, '/new')])
        self.bot(first, FakeProvider()).poll_once()
        model = FakeProvider({'content': '收到'}, {'content': '好的'})
        second = FakeTelegram([message(102, '查看记忆'), message(103, '写周报'),
                               message(104, '忘记第 1 条'), message(105, '继续')])
        bot = self.bot(second, model)
        bot.poll_once()
        self.assertIn('1. 周报用中文', second.sent[0][1])
        self.assertIn('周报用中文', model.messages[0][0]['content'])
        self.assertNotIn('周报用中文', model.messages[1][0]['content'])
        self.assertNotIn('周报用中文', bot.history.read_text())

    def test_memory_is_writable_only_by_allowed_owner_and_not_by_the_model(self):
        client = FakeTelegram([message(110, '记住：未授权数据', owner=456), message(111, '你好'),
                               message(112, '查看记忆')])
        model = FakeProvider({'tool_calls': [{'id': 'memory', 'type': 'function', 'function': {
            'name': 'remember', 'arguments': '{"text":"模型想写入"}'}}]}, {'content': '你好'})
        self.bot(client, model).poll_once()
        self.assertIn('未授权', model.messages[1][-1]['content'])
        self.assertIn('没有长期记忆', client.sent[-1][1])

    def test_retrying_a_forget_id_cannot_delete_another_entry(self):
        first = FakeTelegram([message(113, '记住：甲'), message(114, '记住：乙'),
                              message(115, '忘记第 1 条')])
        self.bot(first, FakeProvider()).poll_once()
        second = FakeTelegram([message(116, '忘记第 1 条'), message(117, '记住：丙'),
                               message(118, '查看记忆')])
        self.bot(second, FakeProvider()).poll_once()
        self.assertIn('找不到', second.sent[0][1])
        self.assertEqual(second.sent[-1][1], '2. 乙\n3. 丙')

    def test_normal_chat_still_works_if_optional_data_is_unavailable(self):
        telegram = FakeTelegram([message(119, '继续聊天')])
        model = FakeProvider({'content': '对话可用'})
        bot = self.bot(telegram, model)
        bot.memory.write_text('invalid json', encoding='utf-8')
        bot.skills_dir.write_text('not a directory', encoding='utf-8')
        bot.poll_once()
        self.assertEqual(len(model.messages), 1)
        self.assertIn('对话可用', telegram.sent[-1][1])
        self.assertIn('长期记忆', telegram.sent[-1][1])
        self.assertIn('技能目录', telegram.sent[-1][1])

    def test_skill_can_be_discovered_and_loaded_during_an_ordinary_chat(self):
        from minis_telegram import Bot
        root = self.home / 'local-skills'
        skill = root / 'report'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: report\ndescription: 生成周报\n---\n先列事实再列行动项。', encoding='utf-8')
        telegram = FakeTelegram([message(120, '帮我整理周报')])
        model = FakeProvider({'tool_calls': [{'id': 'skill', 'type': 'function', 'function': {
            'name': 'skill_view', 'arguments': '{"name":"report"}'}}]}, {'content': '周报已整理'})
        bot = Bot(self.home, 123, '1000', telegram, model, skills_dir=root)
        bot.poll_once()
        self.assertIn('生成周报', model.messages[0][0]['content'])
        self.assertNotIn('先列事实再列行动项', model.messages[0][0]['content'])
        self.assertIn('先列事实再列行动项', model.messages[1][-1]['content'])
        self.assertEqual(telegram.sent[-1][1], '周报已整理')

    def test_skill_catalog_never_loads_symlinks_or_traversal(self):
        from minis_skills import SkillCatalog
        root = self.home / 'local-skills'
        root.mkdir()
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'SKILL.md').write_text('---\ndescription: Private\n---\nDo not load this file.', encoding='utf-8')
        (root / 'symlink-dir').symlink_to(outside, target_is_directory=True)
        (root / 'symlink-file').mkdir()
        (root / 'symlink-file/SKILL.md').symlink_to(outside / 'SKILL.md')
        catalog = SkillCatalog(root)
        self.assertEqual(catalog.entries, {})
        for name in ('../outside', 'symlink-dir', 'symlink-file'):
            with self.assertRaises(ValueError):
                catalog.view(name)

    def test_malformed_model_unicode_cannot_poison_following_turn(self):
        telegram = FakeTelegram([message(22), message(23)])

        class Utf8Provider(FakeProvider):
            def reply(self, messages):
                json.dumps(messages, ensure_ascii=False).encode('utf-8')
                return super().reply(messages)

        model = Utf8Provider({'content': '坏字符\ud800'}, {'content': '下一轮成功'})
        self.bot(telegram, model).poll_once()
        self.assertEqual(telegram.sent[-1][1], '下一轮成功')
        history = json.loads(self.bot(telegram, model).history.read_text())
        self.assertEqual(history[1]['content'], '坏字符\ufffd')

    def test_existing_malformed_history_can_be_read_as_utf8(self):
        from minis import read_history
        path = self.home / 'existing.json'
        path.write_text(json.dumps([{'role': 'assistant', 'content': '历史\ud800'}]))
        self.assertEqual(read_history(path)[0]['content'], '历史\ufffd')

    def test_new_removes_only_this_conversations_abandoned_save(self):
        bot = self.bot(FakeTelegram([message(24, '/new')]), FakeProvider())
        leftover = bot.state / f'.{bot.history.name}.abandoned.tmp'
        unrelated = bot.state / '.unrelated.json.abandoned.tmp'
        leftover.write_text('old conversation')
        unrelated.write_text('another conversation')
        bot.poll_once()
        self.assertFalse(leftover.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(json.loads(bot.history.read_text()), [])

    def test_write_tool_is_rejected_even_if_model_requests_it(self):
        client = FakeTelegram([message(30, '写文件')])
        model = FakeProvider({'tool_calls': [{'id': 'a', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps({'path': 'x.txt', 'content': 'no'})}}]},
            {'content': '结束'})
        self.bot(client, model).poll_once()
        self.assertFalse(list(self.home.rglob('x.txt')))
        self.assertIn('只读', model.messages[1][-1]['content'])
        self.assertEqual(client.sent[-1][1], '结束')

    def test_reply_is_split_for_telegram_utf16_limit(self):
        client = FakeTelegram([message(40)])
        text = '你好😀' * 3000
        self.bot(client, FakeProvider({'content': text})).poll_once()
        self.assertEqual(''.join(t for _, t in client.sent), text)
        self.assertTrue(all(len(t.encode('utf-16-le')) // 2 <= 4096 for _, t in client.sent))

    def test_model_error_is_generic_and_does_not_stop_polling(self):
        client = FakeTelegram([message(50), message(51)])
        class Provider:
            called = 0
            def reply(self, messages):
                self.called += 1
                if self.called == 1:
                    raise ValueError('sensitive details')
                return {'content': '下一条正常'}
        self.bot(client, Provider()).poll_once()
        self.assertNotIn('sensitive details', client.sent[0][1])
        self.assertEqual(client.sent[-1][1], '下一条正常')

    def test_delivery_failure_is_not_replayed(self):
        class FailingTelegram(FakeTelegram):
            def send(self, owner, text):
                from minis_telegram import TelegramError
                raise TelegramError('sendMessage', 503)
        from minis_telegram import TelegramError
        item = message(60)
        with self.assertRaises(TelegramError):
            self.bot(FailingTelegram([item]), FakeProvider({'content': '模型已调用'})).poll_once()
        retry = FakeTelegram([item, message(61, '/status')])
        self.bot(retry, FakeProvider()).poll_once()
        self.assertEqual(retry.offsets, [61])
        self.assertEqual(len(retry.sent), 1)

    def test_read_tool_can_read_dedicated_workspace(self):
        client = FakeTelegram([message(70, '读文件')])
        model = FakeProvider({'tool_calls': [{'id': 'a', 'type': 'function', 'function': {
            'name': 'read_file', 'arguments': json.dumps({'path': 'x.txt'})}}]}, {'content': '已读'})
        bot = self.bot(client, model)
        (self.home / 'workspace' / 'x.txt').write_text('授权样本文本')
        bot.poll_once()
        self.assertEqual(model.messages[1][-1]['content'], '授权样本文本')

    def test_bot_api_http_round_trip_with_model_http(self):
        from minis_telegram import Bot, config
        requests = []
        fixture_token = '1000:' + 'X' * 32

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, body))
                if self.path.endswith('/getUpdates'):
                    result = {'ok': True, 'result': [message(3, '请回复测试')]}
                elif self.path == '/v1/chat/completions':
                    if body['messages'][-1]['role'] == 'tool':
                        msg = {'content': '本地 HTTP 联调成功'}
                    else:
                        msg = {'tool_calls': [{'id': 'use-report', 'type': 'function', 'function': {
                            'name': 'skill_view', 'arguments': '{"name":"report"}'}}]}
                    result = {'choices': [{'message': msg}]}
                elif self.path.endswith('/sendMessage'):
                    result = {'ok': True, 'result': {}}
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

            def log_message(self, format, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                skill = self.home / 'skills' / 'report'
                skill.mkdir(parents=True)
                (skill / 'SKILL.md').write_text('---\ndescription: Report\n---\nUse short headings.', encoding='utf-8')
                with patch('minis_telegram.TELEGRAM_HOST', base), patch.dict('os.environ', {
                    'MINIS_TELEGRAM_USER_ID': '123', 'MINIS_TELEGRAM_BOT_TOKEN': fixture_token,
                    'MINIS_API_KEY': 'model-fixture', 'MINIS_BASE_URL': base + '/v1', 'MINIS_MODEL': 'model',
                }, clear=True):
                    owner, telegram, model = config()
                Bot(self.home, owner, telegram.bot_id, telegram, model).poll_once()
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual([path for path, _ in requests], [f'/bot{fixture_token}/getUpdates',
                         '/v1/chat/completions', '/v1/chat/completions', f'/bot{fixture_token}/sendMessage'])
        self.assertEqual(requests[0][1]['allowed_updates'], ['message'])
        self.assertEqual(requests[1][1]['messages'][-1]['content'], '请回复测试')
        self.assertEqual([t['function']['name'] for t in requests[1][1]['tools']],
                         ['list_files', 'read_file', 'get_current_time', 'get_workspace_disk', 'skill_view'])
        self.assertEqual(requests[2][1]['messages'][-1]['content'], 'Use short headings.')
        self.assertEqual(requests[3][1]['chat_id'], 123)
        self.assertEqual(requests[3][1]['text'], '本地 HTTP 联调成功')


if __name__ == '__main__':
    unittest.main()
