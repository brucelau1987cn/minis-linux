import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest


HERE = Path(__file__).resolve().parent
ON = b'\x1b[?2004h'
OFF = b'\x1b[?2004l'
BEGIN = b'\x1b[200~'
END = b'\x1b[201~'
CHILD = '''
import json
try:
    import readline  # Keep the parent CLI's usual line editing installed.
except ImportError:
    pass
from minis_input import read_prompt
try:
    value = read_prompt('Prompt> ', plain=PLAIN, commands=('/help', '/history', '/new'))
except (EOFError, KeyboardInterrupt) as exc:
    result = type(exc).__name__
else:
    result = [str(value), value.allow_commands]
print('RESULT=' + json.dumps(result), flush=True)
'''


class ComposerTest(unittest.TestCase):
    def start(self, *, term='xterm-256color', plain=False, columns=80, followup=''):
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        os.set_blocking(self.master, False)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, columns, 0, 0))
        self.original = termios.tcgetattr(self.slave)
        self.proc = subprocess.Popen(
            [sys.executable, '-EsB', '-c', f'PLAIN = {plain!r}\n' + CHILD + followup], cwd=HERE,
            env=dict(os.environ, TERM=term, LC_ALL='C.UTF-8', INPUTRC=os.devnull),
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
        )
        self.addCleanup(self.stop)
        self.output = bytearray()
        self.wait_for(b'Prompt> ')

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=3)

    def wait_for(self, marker, *, since=0):
        deadline = time.monotonic() + 3
        while self.output.find(marker, since) == -1:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, bytes(self.output))
            ready, _, _ = select.select([self.master], [], [], remaining)
            if ready:
                self.output.extend(os.read(self.master, 4096))
        return self.output.index(marker, since)

    def send(self, keys):
        deadline = time.monotonic() + 3
        while keys:
            remaining = max(0, deadline - time.monotonic())
            self.assertTrue(select.select([], [self.master], [], remaining)[1], 'PTY write stalled')
            keys = keys[os.write(self.master, keys):]

    def assert_draft_open(self):
        # Look beyond the preview marker for a premature return.
        while select.select([self.master], [], [], 0.05)[0]:
            self.output.extend(os.read(self.master, 4096))
        self.assertNotIn(b'RESULT=', self.output)
        self.assertIsNone(self.proc.poll())

    def result(self, expected, *, native=True):
        marker = b'RESULT=' + json.dumps(expected).encode('ascii') + b'\r\n'
        start = self.wait_for(marker)
        self.assertEqual(self.proc.wait(timeout=3), 0, self.output)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        if native:
            self.assertIn(ON, self.output)
            self.assertIn(OFF, self.output[:start])
        else:
            self.assertNotIn(ON, self.output)
            self.assertNotIn(OFF, self.output)

    def test_multiline_editing_and_explicit_submit(self):
        self.start()
        self.send('  first\nsecnd'.encode())
        self.send(b'\x1b[D\x1b[Do\x1b[F\x1b\r')
        self.send('第三行 '.encode())
        self.wait_for('第三行'.encode())
        self.assertNotIn(b'RESULT=', self.output)
        self.send(b'\r')
        self.result(['first\nsecond\n第三行', False])

    def test_scrolling_preview_keeps_the_entire_paste(self):
        self.start()
        text = '\n'.join(f'line {i}' for i in range(10)) + '\n' + '界' * 100
        self.send(BEGIN + text.encode() + END)
        self.wait_for(b'Paste preview')
        self.assertNotIn(b'line 0', self.output)
        self.send(b'\x1b[A' * 10)
        self.wait_for(b'Prompt> line 0')
        self.send(b'\r')
        self.result([text, False])

    def test_bracketed_paste_is_one_sanitized_draft(self):
        self.start()
        self.send(BEGIN[:3])
        self.send(BEGIN[3:] + b'  /new\r\n\x1b[31msecond\x1b[0m\tline\rthird'
                  b'\x00\x03\x04\x7f\x1b]0;untrusted-title\x07' + END[:4])
        self.assert_draft_open()
        self.send(END[4:])
        self.wait_for(b'Paste preview')
        self.assertIn(b'/new', self.output)
        self.assertIn(b'third', self.output)
        self.assertNotIn(b'untrusted-title', self.output)
        self.assertNotIn(b'\x1b[31m', self.output)
        self.assert_draft_open()
        self.send(b'\r')
        self.result(['/new\nsecond\tline\nthird', False])

    def test_paste_marker_survives_an_incomplete_escape_sequence(self):
        self.start()
        self.send(b'\x1b[1;' + BEGIN + b'/new\rsecond' + END)
        self.wait_for(b'Paste preview')
        self.assert_draft_open()
        self.send(b'\r')
        self.result(['/new\nsecond', False])

    def test_single_line_slash_paste_disables_commands_after_edits(self):
        self.start()
        self.send(BEGIN + b'/ne' + END)
        self.wait_for(b'Paste preview')
        self.assertNotIn(b'Suggestions:', self.output)
        self.send(b'\tw\r')
        self.result(['/new', False])

    def test_empty_sanitized_paste_does_not_enable_existing_slash_draft(self):
        self.start()
        self.send(b'/new' + BEGIN + b'\x00\x07' + END + b'\r')
        self.result(['/new', False])

    def test_oversized_paste_is_discarded_through_its_end_marker(self):
        self.start()
        self.send(b'before' + BEGIN + b'x\r\n' * 24000 + b'/new\r' + END)
        self.wait_for(b'Paste too large')
        self.assert_draft_open()
        self.send(b' after\r')
        self.result(['before after', False])

    def test_tab_cycles_only_supplied_matching_commands_without_submitting(self):
        self.start()
        self.send(b'/h')
        self.wait_for(b'Suggestions: /help /history')
        self.send(b'\t\t')
        self.wait_for(b'Prompt> /history')
        self.assertNotIn(b'RESULT=', self.output)
        self.send(b'\r')
        self.result(['/history', True])

    def test_ctrl_c_and_ctrl_d_restore_modes(self):
        for keys, expected in [(b'draft\x03', 'KeyboardInterrupt'), (b'\x04', 'EOFError')]:
            with self.subTest(keys=keys):
                self.start()
                self.send(keys)
                self.result(expected)
                self.stop()

    def test_ctrl_u_starts_a_fresh_command_draft(self):
        self.start()
        self.send(BEGIN + b'/new' + END + b'\x15/new\r')
        self.result(['/new', True])

    def test_sigint_during_unfinished_paste_restores_modes(self):
        self.start()
        self.send(BEGIN + b'unsubmitted\n')
        self.assert_draft_open()
        self.proc.send_signal(signal.SIGINT)
        self.result('KeyboardInterrupt')

    def test_plain_and_unsupported_term_use_existing_line_input(self):
        for plain, term, columns in ((True, 'xterm', 80), (False, 'dumb', 80),
                                     (False, 'made-up', 80), (False, 'xterm', 12)):
            with self.subTest(plain=plain, term=term, columns=columns):
                self.start(plain=plain, term=term, columns=columns)
                self.send(b'/nex\x7fw\r')
                self.result(['/new', True], native=False)
                self.stop()

    def test_readline_works_after_a_native_prompt(self):
        self.start(followup="print('LINE=' + input('Next> '), flush=True)\n")
        self.send(b'hello\r')
        start = self.wait_for(b'Next> ')
        self.assertIn(b'RESULT=["hello", true]', self.output[:start])
        self.assertIn(OFF, self.output[:start])
        self.send(b'typou\x7f\x7f\r')
        self.wait_for(b'LINE=typ\r\n')
        self.assertEqual(self.proc.wait(timeout=3), 0, self.output)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)


class RedirectedInputTest(unittest.TestCase):
    def test_pipe_uses_line_input_without_terminal_modes(self):
        proc = subprocess.run(
            [sys.executable, '-EsB', '-c', 'PLAIN = False\n' + CHILD], cwd=HERE,
            input=b'  hello  \n', capture_output=True, timeout=3, check=True,
        )
        self.assertEqual(proc.stdout, b'Prompt> RESULT=["hello", true]\n')
