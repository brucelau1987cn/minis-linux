import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

import minis_actions
from minis_actions import ACTION_TOOLS, ActionError, ActionQueue


class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(TemporaryDirectory())
        self.home = Path(self.tmp) / 'home'
        self.home.mkdir(mode=0o700)
        self.workspace = self.home / 'workspace'
        self.workspace.mkdir(mode=0o700)
        self.skills = self.home / 'skills'
        self.memory = []
        self.queue = self.open_queue()

    def open_queue(self, scope='cli:owner:main', **kwargs):
        return ActionQueue(self.home, self.workspace, self.skills,
                           self.memory.append, scope=scope, **kwargs)

    def test_staged_create_is_visible_after_restart_and_approval_is_one_use(self):
        p = self.queue.stage('propose_create_file', {'path': 'note.md', 'content': 'hello\n'})
        note = self.workspace / 'note.md'
        self.assertFalse(note.exists())
        reopened = self.open_queue()
        self.assertEqual(reopened.preview(p['id']), p)
        self.assertEqual([item['id'] for item in reopened.list()], [p['id']])
        self.assertEqual(reopened.approve(p['id'])['status'], 'applied')
        self.assertEqual(note.read_bytes(), b'hello\n')
        self.assertEqual(note.stat().st_mode & 0o777, 0o600)
        self.assertEqual(reopened.list(), [])
        with self.assertRaises(ActionError):
            reopened.approve(p['id'])

    def test_preview_quotes_reversible_text_and_keeps_written_bytes(self):
        content = 'a\r\n\t"b"\\r\u2028c\n'
        proposal = self.queue.stage('propose_create_file', {'path': 'note', 'content': content})
        detail = proposal['detail']
        self.assertEqual(json.loads(detail), content)
        self.assertTrue(all(ch.isprintable() for ch in detail))
        self.assertEqual(self.open_queue().preview(proposal['id']), proposal)
        self.queue.approve(proposal['id'])
        self.assertEqual((self.workspace / 'note').read_bytes(), content.encode())

    def test_create_does_not_replace_appearing_file(self):
        p = self.queue.stage('propose_create_file', {'path': 'note', 'content': 'new'})
        note = self.workspace / 'note'
        note.write_text('owner')
        with self.assertRaises(ActionError):
            self.queue.approve(p['id'])
        self.assertEqual(note.read_text(), 'owner')
        self.assertEqual(self.queue.list(), [])

    def test_edit_requires_approval_of_full_diff(self):
        note = self.workspace / 'note'
        note.write_bytes(b'old\nsecond')
        p = self.queue.stage('propose_edit_file', {'path': 'note', 'content': 'new\nsecond\n'})
        changes = [(line[0], json.loads(line[2:])) for line in p['detail'].splitlines()
                   if line.startswith(('- "', '+ "'))]
        self.assertEqual(changes, [('-', 'old\n'), ('-', 'second'), ('+', 'new\n'), ('+', 'second\n')])
        self.assertEqual(note.read_bytes(), b'old\nsecond')
        self.open_queue().approve(p['id'])
        self.assertEqual(note.read_bytes(), b'new\nsecond\n')
        self.assertEqual(self.queue.trash_list(), [])

    def test_changed_edit_or_delete_target_is_never_applied(self):
        for kind, args in (('propose_edit_file', {'content': 'new'}), ('propose_delete_file', {})):
            with self.subTest(kind=kind):
                note = self.workspace / 'note'
                note.write_text('old')
                p = self.queue.stage(kind, {'path': 'note', **args})
                replacement = self.workspace / 'replacement'
                replacement.write_text('old')
                replacement.replace(note)
                with self.assertRaises(ActionError):
                    self.queue.approve(p['id'])
                self.assertEqual(note.read_text(), 'old')
                self.assertEqual(self.queue.trash_list(), [])

    def test_delete_and_restore_never_overwrite(self):
        note = self.workspace / 'note.bin'
        note.write_bytes(b'\xff\x00\x01')
        deletion = self.queue.stage('propose_delete_file', {'path': 'note.bin'})
        self.assertIn('note.bin', deletion['summary'])
        self.assertTrue(note.exists())
        self.queue.approve(deletion['id'])
        self.assertFalse(note.exists())
        reopened = self.open_queue()
        trash_id, = [item['id'] for item in reopened.trash_list()]
        self.assertEqual(trash_id, deletion['id'])
        self.assertEqual(self.open_queue(scope='telegram:owner:main').trash_list(), [])
        p = reopened.stage_restore(trash_id)
        note.write_text('keep me')
        with self.assertRaises(ActionError):
            reopened.approve(p['id'])
        self.assertEqual(note.read_text(), 'keep me')
        note.unlink()
        reopened.approve(reopened.stage_restore(trash_id)['id'])
        self.assertEqual(note.read_bytes(), b'\xff\x00\x01')
        self.assertEqual(reopened.trash_list(), [])

    def test_memory_confirmation_and_metadata_only_audit(self):
        secret = 'token example-not-a-real-key'
        p = self.queue.stage('propose_memory', {'text': secret})
        self.assertIn(secret, p['detail'])
        self.assertNotIn(secret, repr(self.queue.list()))
        self.assertEqual(self.memory, [])
        self.queue.approve(p['id'])
        self.assertEqual(self.memory, [secret])
        self.assertEqual([entry['status'] for entry in self.queue.audit()],
                         ['applied', 'started', 'staged'])
        self.assertNotIn(secret, repr(self.queue.audit()))

    def test_skill_proposal_installs_only_markdown_and_never_overwrites(self):
        from minis_skills import SkillCatalog

        args = {'name': 'helper', 'description': 'One-line summary', 'content': '# Help\n'}
        p = self.queue.stage('propose_skill', args)
        self.assertFalse(self.skills.exists())
        self.assertIn('# Help', p['detail'])
        self.queue.approve(p['id'])
        self.assertEqual(SkillCatalog(self.skills).view('helper'), '# Help')
        self.assertEqual(list((self.skills / 'helper').iterdir()), [self.skills / 'helper' / 'SKILL.md'])
        with self.assertRaises(ActionError):
            self.queue.stage('propose_skill', args)

    def test_local_skill_import_validates_frontmatter_and_requires_approval(self):
        with self.assertRaises(ActionError):
            self.queue.stage_skill_import('---\nname: ../escape\ndescription: Bad\n---\n# Body\n')
        with self.assertRaises(ActionError):
            self.queue.stage_skill_import('---\nname: ok\ndescription: A\ndescription: B\n---\n# Body\n')
        p = self.queue.stage_skill_import('---\nname: local-note\ndescription: "Notes"\n---\n# How to write a note\n')
        self.assertFalse(self.skills.exists())
        self.queue.approve(p['id'])
        self.assertIn('# How to write a note', (self.skills / 'local-note' / 'SKILL.md').read_text())

    def test_replaced_skill_overlay_parent_invalidates_first_install(self):
        parent = self.home / 'overlays'
        parent.mkdir()
        queue = ActionQueue(self.home, self.workspace, parent / 'skills')
        p = queue.stage('propose_skill', {'name': 'note', 'description': 'Notes', 'content': '# Body'})
        parent.rename(self.home / 'old-overlays')
        parent.mkdir()
        with self.assertRaises(ActionError):
            queue.approve(p['id'])
        self.assertFalse((parent / 'skills').exists())

    def test_only_proposals_are_exposed_to_models(self):
        expected = {'propose_create_file': ['path', 'content'], 'propose_edit_file': ['path', 'content'],
                    'propose_delete_file': ['path'], 'propose_memory': ['text'],
                    'propose_skill': ['name', 'description', 'content']}
        for tool in ACTION_TOOLS:
            self.assertEqual(tool['type'], 'function')
            fn = tool['function']
            fields = expected.pop(fn['name'])
            self.assertEqual(fn['parameters']['required'], fields)
            self.assertIs(fn['parameters']['additionalProperties'], False)
        self.assertEqual(expected, {})

    def test_scope_gates_all_id_operations_and_audit(self):
        p = self.queue.stage('propose_create_file', {'path': 'note', 'content': 'hello'})
        for scope in ('telegram:owner:main', 'cli:someone:main', 'cli:owner:other'):
            wrong = self.open_queue(scope=scope)
            self.assertEqual(wrong.list(), [])
            self.assertEqual(wrong.audit(), [])
            for method in (wrong.preview, wrong.cancel, wrong.approve):
                with self.assertRaises(ActionError):
                    method(p['id'])
        self.assertEqual(self.queue.cancel(p['id'])['status'], 'cancelled')
        with self.assertRaises(ActionError):
            self.open_queue().approve(p['id'])

    def test_expired_id_stays_unavailable_after_restart(self):
        with patch('minis_actions.time.time', return_value=100):
            p = self.open_queue(ttl_seconds=10).stage('propose_memory', {'text': 'new item'})
        with patch('minis_actions.time.time', return_value=110):
            with self.assertRaises(ActionError):
                self.open_queue().approve(p['id'])
            self.assertEqual(self.queue.audit()[0]['status'], 'expired')
        self.assertEqual(self.memory, [])
        with patch('minis_actions.time.time', return_value=100):
            self.assertEqual(self.queue.list(), [])

    def test_simultaneous_approvals_execute_once(self):
        p = self.queue.stage('propose_memory', {'text': 'just once'})
        ready = Barrier(2, timeout=5)

        def attempt():
            queue = self.open_queue()
            ready.wait()
            try:
                return queue.approve(p['id'])['status']
            except ActionError:
                return 'rejected'

        with ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(pool.map(lambda _: attempt(), range(2)), ['applied', 'rejected'])
        self.assertEqual(self.memory, ['just once'])

    def test_interrupted_memory_callback_is_not_replayed(self):
        def interrupt(text):
            self.memory.append(text)
            raise SystemExit

        queue = ActionQueue(self.home, self.workspace, self.skills, interrupt, scope='cli:owner:main')
        p = queue.stage('propose_memory', {'text': 'one write, no replay'})
        with self.assertRaises(SystemExit):
            queue.approve(p['id'])
        with self.assertRaises(ActionError):
            self.open_queue().approve(p['id'])
        self.assertEqual(self.memory, ['one write, no replay'])
        self.assertEqual(self.queue.audit()[0]['status'], 'started')

    def test_ambiguous_callback_does_not_log_error_or_allow_retry(self):
        def fail(_):
            raise ValueError('sensitive callback error')

        queue = ActionQueue(self.home, self.workspace, self.skills, fail, scope='cli:owner:main')
        p = queue.stage('propose_memory', {'text': 'sensitive proposed item'})
        with self.assertRaises(ActionError) as failure:
            queue.approve(p['id'])
        self.assertNotIn('sensitive', str(failure.exception))
        self.assertNotIn('sensitive', (queue.state_dir / 'state.json').read_text())
        self.assertEqual(queue.audit()[0]['status'], 'unknown')
        self.assertEqual(queue.list(), [])

    def test_stale_in_place_edit_is_rejected_even_with_mtime_restored(self):
        note = self.workspace / 'note'
        note.write_text('abc')
        p = self.queue.stage('propose_edit_file', {'path': 'note', 'content': 'new'})
        before = note.stat()
        note.write_text('def')
        os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(ActionError):
            self.queue.approve(p['id'])
        self.assertEqual(note.read_text(), 'def')

    def test_rejects_links_directories_and_special_files(self):
        outside = self.home / 'outside'
        outside.write_text('original')
        (self.workspace / 'link').symlink_to(outside)
        (self.workspace / 'dangling').symlink_to(self.home / 'missing')
        (self.workspace / 'nested').mkdir()
        os.link(outside, self.workspace / 'hard')
        os.mkfifo(self.workspace / 'pipe')
        for name in ('link', 'dangling', 'hard', 'pipe', 'nested', '../outside', str(outside), '.hidden'):
            for kind, args in (('propose_create_file', {'content': 'new'}),
                               ('propose_edit_file', {'content': 'new'}), ('propose_delete_file', {})):
                with self.subTest(name=name, kind=kind), self.assertRaises(ActionError):
                    self.queue.stage(kind, {'path': name, **args})
        self.assertEqual(outside.read_text(), 'original')
        self.assertEqual(self.queue.list(), [])

    def test_symlink_in_configured_ancestor_is_not_followed(self):
        alias = self.home / 'alias'
        alias.symlink_to(self.home, target_is_directory=True)
        queue = ActionQueue(self.home, alias / 'workspace', self.skills)
        with self.assertRaises(ActionError):
            queue.stage('propose_create_file', {'path': 'note', 'content': 'new'})
        queue = ActionQueue(alias, self.workspace, self.skills)
        with self.assertRaises(ActionError):
            queue.list()

    def test_workspace_replacement_invalidates_approval(self):
        p = self.queue.stage('propose_create_file', {'path': 'note', 'content': 'new'})
        self.workspace.rename(self.home / 'old-workspace')
        self.workspace.mkdir()
        with self.assertRaises(ActionError):
            self.queue.approve(p['id'])
        self.assertFalse((self.workspace / 'note').exists())

    def test_state_files_are_private_and_symlink_state_is_rejected(self):
        self.queue.stage('propose_memory', {'text': 'private draft'})
        state = self.queue.state_dir / 'state.json'
        self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        state.rename(self.home / 'state-copy')
        state.symlink_to(self.home / 'state-copy')
        with self.assertRaises(ActionError):
            self.queue.list()

    def test_bounds_and_strict_tool_arguments(self):
        for args in ({'path': 'large', 'content': '汉' * 11000},
                     {'path': 'n', 'content': 'abc\0def'}, {'path': 'n', 'content': '\ud800'},
                     {'path': 'n', 'content': 'new', 'approve': True}, {'path': 123, 'content': 'new'}):
            with self.assertRaises(ActionError):
                self.queue.stage('propose_create_file', args)
        for kind in ('approve', 'restore_file', 'write_file'):
            with self.assertRaises(ActionError):
                self.queue.stage(kind, {})
        self.assertEqual(self.queue.list(), [])

    def test_edit_interrupted_after_stash_leaves_recoverable_original(self):
        note = self.workspace / 'note'
        note.write_text('original')
        p = self.queue.stage('propose_edit_file', {'path': 'note', 'content': 'changed'})
        move = minis_actions._move_new

        def interrupt(src_fd, src, dst_fd, dst):
            if dst == 'note':
                raise SystemExit
            return move(src_fd, src, dst_fd, dst)

        with patch('minis_actions._move_new', side_effect=interrupt), self.assertRaises(SystemExit):
            self.queue.approve(p['id'])
        reopened = self.open_queue()
        self.assertFalse(note.exists())
        with self.assertRaises(ActionError):
            reopened.approve(p['id'])
        self.assertEqual(reopened.trash_list()[0]['status'], 'recovery')
        reopened.approve(reopened.stage_restore(p['id'])['id'])
        self.assertEqual(note.read_text(), 'original')

    def test_swapped_target_at_rename_is_not_left_in_trash(self):
        note = self.workspace / 'note'
        note.write_text('approved target')
        outside = self.home / 'outside'
        outside.write_text('outside')
        p = self.queue.stage('propose_delete_file', {'path': 'note'})
        move = minis_actions._move_new

        def swap(src_fd, src, dst_fd, dst):
            if src == 'note':
                note.rename(self.workspace / 'old')
                note.symlink_to(outside)
            return move(src_fd, src, dst_fd, dst)

        with patch('minis_actions._move_new', side_effect=swap), self.assertRaises(ActionError):
            self.queue.approve(p['id'])
        self.assertTrue(note.is_symlink())
        self.assertEqual(outside.read_text(), 'outside')
        self.assertEqual(self.queue.trash_list(), [])

    def test_abandoned_state_temp_is_not_retained_after_restart(self):
        queue = self.open_queue()
        queue.list()  # Create private state directory.
        abandoned = queue.state_dir / '.state.tmp'
        abandoned.write_text('abandoned proposal payload')
        self.open_queue().list()
        self.assertFalse(abandoned.exists())
