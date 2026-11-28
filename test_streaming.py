from email.message import Message
import io
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from minis import Provider, Workspace, complete_turn, read_history


def event(delta=None, *, finish=None):
    payload = {'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}
    return ('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n').encode()


DONE = b'data: [DONE]\n\n'
UNSUPPORTED = {'error': {'code': 'unsupported_value', 'param': 'stream', 'message': 'private details'}}


def json_reply(**message):
    return Response(json.dumps({'choices': [{'message': message}]}).encode(), 'application/json')


def http_error(status, payload=None):
    return HTTPError('https://example.invalid/v1/chat/completions', status, 'private details', Message(),
                     io.BytesIO(json.dumps(payload).encode()))


def tool_delta(index, *, call_id=None, name=None, args=None):
    return {'index': index, 'id': call_id, 'type': 'function' if name else None,
            'function': {'name': name, 'arguments': args}}


class Response(io.BytesIO):
    def __init__(self, data, media='text/event-stream', *, fragment=4096):
        super().__init__(data)
        self.headers = {'Content-Type': media}
        self.fragment = fragment

    def read1(self, size):
        return super().read1(min(size, self.fragment))


class StreamingTest(unittest.TestCase):
    def setUp(self):
        self.provider = Provider('https://example.invalid/v1', 'fixture-key', 'primary', read_only=True)
        opener = patch.object(self.provider.opener, 'open')
        self.opener = opener.start()
        self.addCleanup(opener.stop)

    def requests(self):
        return [json.loads(call.args[0].data) for call in self.opener.call_args_list]

    def test_utf8_fragments_are_progressive_and_reasoning_is_not_exposed(self):
        response = Response(b'\xef\xbb\xbf: heartbeat\r\n\r\n' +
                            event({'reasoning_content': 'hidden', 'reasoning': 'also hidden'}) +
                            event({'content': '你'}).replace(b'\n', b'\r\n') +
                            'data: {"choices": [\rdata: {"delta":{"content":"好"}}]}\r\r'.encode() +
                            b'data: {"choices":[], "usage":{"total_tokens":3}}\n\n' + DONE,
                            'text/event-stream; charset=utf-8', fragment=1)
        self.opener.return_value = response
        deltas = []

        def emit(delta):
            self.assertFalse(response.closed)
            self.assertLess(response.tell(), len(response.getbuffer()))
            deltas.append(delta)

        reply = self.provider.reply([], on_delta=emit)
        self.assertEqual(deltas, ['你', '好'])
        self.assertEqual(reply, {'content': '你好'})
        self.assertTrue(self.requests()[0]['stream'])
        self.assertTrue(response.closed)

    def test_without_callback_requests_normal_json(self):
        self.opener.return_value = json_reply(content='ok', reasoning_content='hidden')
        self.assertEqual(self.provider.reply([]), {'content': 'ok'})
        self.assertFalse(self.requests()[0]['stream'])

    def test_fallback_changes_only_request_model_and_not_the_default(self):
        self.opener.side_effect = [URLError(TimeoutError()), json_reply(content='backup'),
                                   json_reply(content='default')]
        models, notices = [], []
        self.assertEqual(complete_turn(self.provider, None, [], None, fallback_model='backup',
                                       on_model=models.append, on_notice=notices.append, on_delta=lambda _: None),
                         'backup')
        self.assertEqual(models, ['backup'])
        self.assertEqual(len(notices), 1)
        first, second = [call.args[0] for call in self.opener.call_args_list]
        self.assertEqual((first.full_url, first.headers), (second.full_url, second.headers))
        primary, backup = self.requests()
        self.assertEqual(primary['model'], 'primary')
        self.assertEqual(backup, {**primary, 'model': 'backup'})
        self.assertEqual(self.provider.model, 'primary')
        self.assertEqual(self.provider.reply([]), {'content': 'default'})
        self.assertEqual(self.requests()[-1]['model'], 'primary')

    def test_old_signature_is_not_retried_or_given_new_kwargs(self):
        class Legacy:
            model = 'legacy'

            def reply(self, messages):
                return {'content': 'whole'}

        deltas, models = [], []
        self.assertEqual(complete_turn(Legacy(), None, [], None, on_delta=deltas.append, on_model=models.append), 'whole')
        self.assertEqual(deltas, [])
        self.assertEqual(models, ['legacy'])

    def test_stream_checks_cancellation_before_exposing_each_fragment(self):
        class Stopped(Exception):
            pass

        count = 0

        def checkpoint(phase):
            if phase == '接收模型回复':
                nonlocal count
                count += 1
                if count == 2:
                    raise Stopped

        response = Response(event({'content': 'one'}) + event({'content': 'two'}) + DONE)
        self.opener.return_value = response
        deltas, messages = [], []
        with self.assertRaises(Stopped):
            complete_turn(self.provider, None, messages, None, on_delta=deltas.append,
                          checkpoint=checkpoint, fallback_model='backup')
        self.assertEqual(deltas, ['one'])
        self.assertEqual(messages, [])
        self.assertEqual(self.opener.call_count, 1)
        self.assertTrue(response.closed)

    def test_json_response_to_stream_request_is_used_without_replay(self):
        self.opener.return_value = json_reply(content='whole', reasoning_content='private')
        deltas = []
        self.assertEqual(self.provider.reply([], on_delta=deltas.append), {'content': 'whole'})
        self.assertEqual(deltas, [])
        self.assertEqual(self.opener.call_count, 1)

    def test_explicit_unsupported_stream_retries_once_without_stream(self):
        body = json.dumps(UNSUPPORTED)
        cases = (http_error(400, UNSUPPORTED), Response(body.encode(), 'application/json'),
                 Response(f'data: {body}\n\n'.encode()))
        for i, unsupported in enumerate(cases):
            with self.subTest(i=i):
                provider = self.provider.for_model(f'fixture-{i}')
                self.opener.reset_mock()
                self.opener.side_effect = [unsupported, json_reply(content='whole'), json_reply(content='again')]
                self.assertEqual(provider.reply([], on_delta=self.fail), {'content': 'whole'})
                self.assertEqual(provider.reply([], on_delta=self.fail), {'content': 'again'})
                self.assertEqual([r['stream'] for r in self.requests()], [True, False, False])
                self.assertTrue(unsupported.closed)

    def test_unsupported_after_text_or_tool_fragments_does_not_replay(self):
        for delta in ({'content': 'visible'}, {'tool_calls': [tool_delta(0, call_id='partial')]}):
            with self.subTest(delta=delta), patch.object(self.provider.opener, 'open') as opener:
                opener.return_value = Response(event(delta) + f'data: {json.dumps(UNSUPPORTED)}\n\n'.encode())
                with self.assertRaisesRegex(ValueError, '模型接口返回错误'):
                    self.provider.reply([], on_delta=lambda _: None)
                self.assertEqual(opener.call_count, 1)

    def test_bad_or_ambiguous_stream_is_not_replayed(self):
        for payload in (b'', DONE, b'data: \xff\n\n', b'data: nonsense\n\n',
                        event({'content': 'partial'}), event({'tool_calls': [tool_delta(4)]}) + DONE,
                        event({'tool_calls': [tool_delta(0, call_id='incomplete')]}) + DONE,
                        event({'tool_calls': [tool_delta(0, call_id='x', name='list_files', args='{}')]},
                              finish='length') + DONE):
            with self.subTest(payload=payload), patch.object(self.provider.opener, 'open') as opener:
                opener.return_value = Response(payload)
                with self.assertRaises(ValueError):
                    complete_turn(self.provider, None, [], None, on_delta=lambda _: None, fallback_model='backup')
                self.assertEqual(opener.call_count, 1)

    def test_sse_event_and_body_limits_include_ignored_data(self):
        with patch('minis.SSE_EVENT_LIMIT', 80):
            self.opener.return_value = Response((b'data: xxxxxxx\n' * 8) + b'\n')
            with self.assertRaisesRegex(ValueError, '事件过大'):
                self.provider.reply([], on_delta=self.fail)
        with patch('minis.HTTP_LIMIT', 4096):
            self.opener.return_value = Response(b': ignored\n\n' * 700)
            with self.assertRaisesRegex(ValueError, '响应过大'):
                self.provider.reply([], on_delta=self.fail)

    def test_transient_statuses_retry_once_and_permanent_statuses_do_not(self):
        transient = (408, 429, 500, 502, 503, 504, 529)
        permanent = (401, 403, 404, 422, 501, 505, 511)
        for status in transient + permanent:
            with self.subTest(status=status), patch.object(self.provider.opener, 'open') as opener:
                opener.side_effect = [http_error(status), http_error(503)]
                with self.assertRaisesRegex(ValueError, f'HTTP {503 if status in transient else status}'):
                    complete_turn(self.provider, None, [], None, fallback_model='backup')
                self.assertEqual(opener.call_count, 2 if status in transient else 1)

    def test_error_event_can_trigger_fallback_before_any_text_but_not_after_partial_reply(self):
        error = b'data: {"error":{"code":429,"message":"private"}}\n\n'
        for first in ({'reasoning_content': 'ignored'}, {'content': 'partial'},
                      {'tool_calls': [tool_delta(0, call_id='partial')]}):
            with self.subTest(first=first), patch.object(self.provider.opener, 'open') as opener:
                opener.side_effect = [Response(event(first) + error), json_reply(content='backup')]
                deltas, messages = [], []
                if 'reasoning_content' in first:
                    self.assertEqual(complete_turn(self.provider, None, messages, None, on_delta=deltas.append,
                                                   fallback_model='backup'), 'backup')
                    self.assertEqual(deltas, [])
                    self.assertEqual(opener.call_count, 2)
                else:
                    with self.assertRaisesRegex(ValueError, 'HTTP 429'):
                        complete_turn(self.provider, None, messages, None, on_delta=deltas.append,
                                      fallback_model='backup')
                    self.assertEqual(deltas, [first['content']] if 'content' in first else [])
                    self.assertEqual(messages, [])
                    self.assertEqual(opener.call_count, 1)

    def test_read_timeout_after_tool_fragments_does_not_retry_or_dispatch(self):
        class Stalled(Response):
            def read1(self, size):
                chunk = super().read1(size)
                if not chunk:
                    raise TimeoutError
                return chunk

        self.opener.side_effect = [Stalled(event({'tool_calls': [tool_delta(0, call_id='part')]})),
                                   json_reply(content='unexpected')]
        messages = []
        with self.assertRaisesRegex(ValueError, '流式响应超时'):
            complete_turn(self.provider, None, messages, None, on_delta=self.fail, fallback_model='backup')
        self.assertEqual(messages, [])
        self.assertEqual(self.opener.call_count, 1)

    def test_non_timeout_url_error_and_provider_type_error_do_not_retry(self):
        for error in (URLError(ConnectionRefusedError()), TypeError('provider bug')):
            with self.subTest(error=error):
                self.opener.reset_mock()
                self.opener.side_effect = [error, json_reply(content='unexpected')]
                with self.assertRaises(type(error)):
                    complete_turn(self.provider, None, [], None, on_delta=self.fail, fallback_model='backup')
                self.assertEqual(self.opener.call_count, 1)

    def test_cancellation_prevents_retry_and_checkpoints_unsupported_downgrade(self):
        class Stopped(Exception):
            pass

        def checkpoint(phase):
            if phase != '等待模型':
                raise Stopped

        for error in (http_error(429), http_error(400, UNSUPPORTED)):
            with self.subTest(status=error.code):
                self.opener.reset_mock()
                self.opener.side_effect = [error, json_reply(content='unexpected')]
                with self.assertRaises(Stopped):
                    complete_turn(self.provider, None, [], None, checkpoint=checkpoint,
                                  on_delta=self.fail, fallback_model='backup', on_notice=self.fail)
                self.assertEqual(self.opener.call_count, 1)

    def test_proposal_allowlist_runs_before_read_only_deny_and_blocks_later_fallback(self):
        functions = [{'name': name, 'arguments': '{}'} for name in ('propose_write', 'write_file')]
        calls = [{'id': str(i), 'type': 'function', 'function': fn} for i, fn in enumerate(functions)]
        self.opener.side_effect = [json_reply(tool_calls=calls), http_error(503)]
        proposals, messages = [], []

        def propose(function):
            proposals.append(function)
            return 'staged only'

        with self.assertRaisesRegex(ValueError, 'HTTP 503'):
            complete_turn(self.provider, None, messages, None, read_only=True, fallback_model='backup',
                          propose_action=propose, proposal_tools=('propose_write',))
        self.assertEqual(proposals, functions[:1])
        self.assertEqual(messages[-2]['content'], 'staged only')
        self.assertIn('未授权', messages[-1]['content'])
        self.assertEqual([r['model'] for r in self.requests()], ['primary', 'primary'])


class LoopbackStreamingTest(unittest.TestCase):
    def test_fragmented_parallel_tools_round_trip_then_save_final_text(self):
        requests = []
        chunks = [event({'tool_calls': [tool_delta(1, call_id='tim', name='get_current_', args='{'),
                                       tool_delta(0, call_id='fi', name='list_', args='{')]}),
                  event({'tool_calls': [tool_delta(0, call_id='les', name='files', args='}'),
                                       tool_delta(1, call_id='e', name='time', args='}')]}),
                  event(finish='tool_calls'), DONE]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                # Second response uses a finish reason with no [DONE].
                body = chunks if len(requests) == 1 else [event({'content': '完成'}, finish='stop')]
                for chunk in body:
                    self.wfile.write(chunk)

            def log_message(self, format, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server, tempfile.TemporaryDirectory() as root:
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            try:
                provider = Provider(f'http://127.0.0.1:{server.server_port}/v1', 'fixture-key', 'primary', read_only=True)
                path = Path(root) / 'history.json'
                deltas = []
                with Workspace(root) as workspace:
                    text = complete_turn(provider, workspace, [], path, read_only=True, on_delta=deltas.append)
                self.assertEqual(text, '完成')
                self.assertEqual(deltas, [text])
                self.assertEqual(read_history(path)[-1]['content'], text)
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual([r['stream'] for r in requests], [True, True])
        tools = requests[1]['messages'][-2:]
        self.assertEqual(tools[0], {'role': 'tool', 'tool_call_id': 'files', 'content': '[]'})
        self.assertEqual(tools[1]['tool_call_id'], 'time')
        self.assertIn('本机时间', tools[1]['content'])
