"""Offline checks at Bot's real Provider request boundary."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from minis import Provider, save_history
from minis_input import Prompt
from minis_telegram import Bot, TaskStopped
from minis_web import _WebContext
from test_streaming import DONE, UNSUPPORTED, Response, event, http_error, json_reply
from test_web import SEARCH_HTML


class WebTurnTest(unittest.TestCase):
    def setUp(self):
        home = TemporaryDirectory()
        self.addCleanup(home.cleanup)
        provider = Provider('https://model.invalid/v1', 'fixture-key', 'primary', read_only=True)
        self.bot = Bot(Path(home.name), 1, 2, None, provider, features=True)
        opener = patch.object(provider.opener, 'open', side_effect=AssertionError('unconfigured model fixture'))
        self.open = opener.start()
        self.addCleanup(opener.stop)

    def requests(self):
        return [json.loads(call.args[0].data) for call in self.open.call_args_list]

    def test_web_is_a_tool_free_request_with_user_role_data_and_no_history(self):
        save_history(self.bot.history, [{'role': 'user', 'content': 'prior fixture text'}])
        web = self.page()
        self.open.side_effect = [json_reply(content='fixture summary')]
        tools = self.bot.provider.tools.copy()
        with (patch('minis_telegram.prepare_web', return_value=web) as prepare,
              patch.object(self.bot, 'memories') as memories,
              patch.object(self.bot, 'skill_catalog', return_value=(None, False)) as skills,
              patch('minis_telegram.read_history', return_value=[]) as history_read,
              patch('minis_telegram.Workspace') as workspace):
            proposals = []
            reply, history = self.bot.generate('Read https://example.org/readme', proposals=proposals)
        prepare.assert_called_once_with('Read https://example.org/readme')
        request, = self.requests()
        self.assertEqual(request['tools'], [])
        system, user = request['messages']
        self.assertEqual((system['role'], user['role']), ('system', 'user'))
        self.assertNotIn('fixture page excerpt', system['content'])
        self.assertIn('fixture page excerpt', repr(json.loads(user['content'])))
        self.assertNotIn('prior fixture text', repr(request))
        self.assertNotIn('https://example.org/readme', repr(request))
        self.assertEqual(reply, 'fixture summary\n\n' + web.source_footer)
        self.assertIsNone(history)
        self.assertEqual(proposals, [])
        self.assertEqual(self.bot.provider.tools, tools)
        for mock in (memories, skills, history_read, workspace):
            mock.assert_not_called()

    def test_sensitive_url_is_not_sent_to_web_or_model_or_saved(self):
        with patch('minis_web._get') as get:
            reply, history = self.bot.generate('Read https://example.org/file?token=fixture-value')
        self.assertNotIn('fixture-value', reply)
        self.assertIsNone(history)
        get.assert_not_called()
        self.open.assert_not_called()

    def test_provider_calls_are_not_dispatched_or_forwarded_as_a_second_request(self):
        fixture = {'id': 'fixture', 'type': 'function',
                   'function': {'name': 'fixture_only', 'arguments': '{}'}}
        response = Response(event({'content': 'unchecked text', 'tool_calls': [{'index': 0, **fixture}]})
                            + event(finish='tool_calls') + DONE)
        self.open.side_effect = [response]
        deltas, proposals = [], []
        with (patch('minis_telegram.prepare_web', return_value=self.page()),
              patch('minis_telegram.complete_turn') as interactive):
            reply, history = self.bot.generate('Read https://example.org/readme', on_delta=deltas.append,
                                               proposals=proposals, fallback_model='backup')
        self.assertNotIn('unchecked text', reply)
        self.assertIsNone(history)
        self.assertEqual((deltas, proposals), ([], []))
        self.open.assert_called_once()
        interactive.assert_not_called()

    def test_sse_waits_for_complete_answer_and_keeps_only_local_links(self):
        self.open.side_effect = [Response(event({'content': 'Summary https://example.net/'})
                                          + event({'content': 'report'}) + event(finish='stop') + DONE)]
        deltas = []
        with patch('minis_telegram.prepare_web', return_value=self.page()):
            reply, history = self.bot.generate('Read https://example.org/readme', on_delta=deltas.append)
        self.assertTrue(self.requests()[0]['stream'])
        self.assertEqual(reply, 'Summary [链接省略]\n\n' + self.page().source_footer)
        self.assertEqual(deltas, [])
        self.assertIsNone(history)

    def test_search_sends_snippets_not_the_typed_query_to_the_model(self):
        class SearchResponse(Response):
            status = 200

        self.open.side_effect = [json_reply(content='Search summary [1]')]
        with patch('minis_web._get', return_value=SearchResponse(SEARCH_HTML, 'text/html')) as get:
            reply, history = self.bot.generate('搜索：public fixture topic')
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0].path, '/lite/?q=public+fixture+topic')
        request, = self.requests()
        data = json.loads(request['messages'][-1]['content'])
        self.assertEqual(data['operation'], 'search')
        self.assertIn('Current security notices', repr(data))
        self.assertNotIn('public fixture topic', repr(request))
        self.assertNotIn('https://example.org/result', repr(request))
        self.assertIn('1. https://example.org/result', reply)
        self.assertIsNone(history)

    def test_fallback_and_stream_downgrade_keep_the_same_isolation(self):
        self.open.side_effect = [http_error(429), http_error(400, UNSUPPORTED), json_reply(content='summary')]
        models, notices = [], []
        with patch('minis_telegram.prepare_web', return_value=self.page()) as lookup:
            reply, _ = self.bot.generate('Search for current fixture topic', on_delta=lambda _: None,
                                         fallback_model='backup', on_model=models.append, on_notice=notices.append)
        primary, backup, non_stream = self.requests()
        self.assertEqual(backup, {**primary, 'model': 'backup'})
        self.assertEqual(non_stream, {**backup, 'stream': False})
        self.assertTrue(primary['stream'])
        self.assertEqual(primary['tools'], [])
        self.assertNotIn('current fixture topic', repr(primary))
        self.assertEqual(models, ['backup'])
        self.assertEqual(len(notices), 1)
        self.assertIn('summary', reply)
        lookup.assert_called_once()
        self.assertEqual(self.bot.provider.model, 'primary')

    def test_no_model_fallback_after_partial_sse(self):
        class Partial(Response):
            def read1(self, size):
                chunk = super().read1(size)
                if not chunk:
                    raise TimeoutError
                return chunk

        self.open.side_effect = [Partial(event({'content': 'partial reply'}))]
        deltas, notices = [], []
        with patch('minis_telegram.prepare_web', return_value=self.page()):
            reply, history = self.bot.generate('Read https://example.org/readme', on_delta=deltas.append,
                                               fallback_model='backup', on_notice=notices.append)
        self.assertNotIn('partial reply', reply)
        self.assertIsNone(history)
        self.assertEqual((deltas, notices), ([], []))
        self.open.assert_called_once()

    def test_failed_lookup_never_falls_back_to_normal_chat(self):
        with patch('minis_web._get', side_effect=TimeoutError):
            reply, history = self.bot.generate('Open https://example.org/readme')
        self.assertIn('未读取网页', reply)
        self.assertIsNone(history)
        self.open.assert_not_called()

    def test_stop_after_lookup_prevents_inference(self):
        def checkpoint(phase):
            if phase == '准备模型请求':
                raise TaskStopped

        with patch('minis_telegram.prepare_web', return_value=self.page()):
            with self.assertRaises(TaskStopped):
                self.bot.generate('Read https://example.org/readme', checkpoint=checkpoint)
        self.open.assert_not_called()

    def test_disabled_and_bypassed_web_do_not_run_lookup(self):
        self.open.side_effect = [json_reply(content='chat') for _ in range(3)]
        with patch('minis_telegram.prepare_web') as lookup:
            for prompt, kwargs in (
                    ('Read https://example.org/readme', {'allow_web': False}),
                    (Prompt('/new https://example.org/readme', allow_commands=False), {}),
                    (Prompt('记住：https://example.org/readme', allow_commands=False), {})):
                _, history = self.bot.generate(prompt, **kwargs)
                self.assertIsNotNone(history)
        lookup.assert_not_called()

    @staticmethod
    def page():
        return _WebContext('fixture page excerpt', [(1, 'https://example.org/readme')])
