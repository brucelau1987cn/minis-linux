import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRIPT = Path(__file__).with_name('minis.py')


class MinisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def cli(self, *args, stdin='', **env):
        return subprocess.run([sys.executable, str(SCRIPT), '--home', str(self.base / 'state'), *args], input=stdin, text=True, capture_output=True, timeout=10, env={**os.environ, **env})

    def test_demo_persists_workspace_and_session(self):
        r = self.cli('--demo', 'demo', stdin='y\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('已写入 note.md', r.stdout)
        self.assertEqual((self.base / 'state/workspace/note.md').read_text(), '你好，Linux！\n')
        history = self.cli('--history')
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertEqual(json.loads(history.stdout)[-1]['content'], '已写入 note.md')

    def test_write_without_consent_does_not_happen(self):
        r = self.cli('--demo', 'demo')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.base / 'state/workspace/note.md').exists())
        self.assertIn('未授权', r.stdout)

    def test_client_sends_tool_result_to_openai_compatible_endpoint(self):
        requests = []
        call = {'id': 'abc', 'type': 'function', 'function': {'name': 'list_files', 'arguments': '{}'}}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                requests.append((self.path, self.headers.get('Authorization'), json.loads(body)))
                msg = {'tool_calls': [call], 'content': None} if len(requests) == 1 else {'content': '检查完成'}
                payload = json.dumps({'choices': [{'message': msg}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                r = self.cli('--base-url', f'http://127.0.0.1:{server.server_port}/v1', '--model', 'fixture', '列文件', MINIS_API_KEY='fixture-key')
            finally:
                server.shutdown()
                t.join()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('检查完成', r.stdout)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][:2], ('/v1/chat/completions', 'Bearer fixture-key'))
        self.assertEqual(requests[1][2]['messages'][-1], {'role': 'tool', 'tool_call_id': 'abc', 'content': '[]'})

    def test_workspace_blocks_parent_paths_and_symlinks(self):
        from minis import Workspace
        ws = self.base / 'workspace'
        ws.mkdir()
        target = self.base / 'secret.txt'
        target.write_text('private')
        (ws / 'link.txt').symlink_to(target)
        with Workspace(ws) as workspace:
            for name in ('../secret.txt', 'link.txt'):
                with self.assertRaises((OSError, ValueError)):
                    workspace.read(name)
            with self.assertRaises((OSError, ValueError)):
                workspace.write('link.txt', 'no')
        self.assertEqual(target.read_text(), 'private')

    def test_redirect_is_not_followed(self):
        from minis import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid'))

    def test_session_names_are_isolated(self):
        self.assertEqual(self.cli('--demo', '--session', 'one', 'demo').returncode, 0)
        r = self.cli('--history', '--session', 'two')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [])


if __name__ == '__main__':
    unittest.main()
