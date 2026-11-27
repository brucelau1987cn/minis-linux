import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minis import read_history, save_history
from minis_sessions import SessionError, Sessions


class SessionsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.legacy = self.root / 'bot-42-owner-1.json'

    def write(self, path, text):
        save_history(path, [{'role': 'user', 'content': text}])

    def test_empty_initial_session_stays_switchable_without_materializing_history(self):
        sessions = Sessions(self.legacy)
        self.assertEqual(sessions.active_path, self.legacy)
        self.assertEqual([(s.id, s.active) for s in sessions.list()], [(1, True)])
        self.assertEqual(list(self.root.iterdir()), [])
        new = sessions.create()
        self.assertEqual(new.id, 2)
        self.assertTrue(new.title)
        self.assertFalse(sessions.active_path.exists())
        self.assertFalse(self.legacy.exists())
        self.assertEqual(Sessions(self.legacy).switch('1').id, 1)
        self.assertEqual(sessions.active_path, self.legacy)

    def test_legacy_history_is_never_moved_or_rewritten(self):
        self.write(self.legacy, 'original transcript')
        original = self.legacy.read_bytes()
        sessions = Sessions(self.legacy)
        new = sessions.create('新计划')
        new_path = sessions.active_path
        self.write(new_path, 'new transcript')
        reopened = Sessions(self.legacy)
        self.assertEqual(reopened.active_path, new_path)
        self.assertEqual(reopened.title('改名').id, new.id)
        reopened.title('原来的会话', 1)
        self.assertEqual([(s.id, s.title, s.active) for s in reopened.list()],
                         [(1, '原来的会话', False), (2, '改名', True)])
        reopened.switch(1)
        self.assertEqual(read_history(reopened.active_path)[0]['content'], 'original transcript')
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(new_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((new_path.parent / 'index.json').stat().st_mode), 0o600)

    def test_ids_are_not_paths_or_titles(self):
        sessions = Sessions(self.legacy)
        first = sessions.create('../bot-42-owner-9.json')
        self.assertEqual(sessions.create(first.title).id, first.id + 1)
        for invalid in ('../1', '2.json', '/2', '02', '２', True, 0, 999, '9' * 5000):
            with self.subTest(invalid=str(invalid)[:30]), self.assertRaises(SessionError):
                sessions.switch(invalid)
        for invalid in ('', 'a\nb', 'x' * 81):
            with self.subTest(title=invalid), self.assertRaises(SessionError):
                sessions.title(invalid)
        self.assertEqual(sessions.list()[-1].id, first.id + 1)

    def test_bad_index_is_preserved_and_does_not_silently_choose_a_history(self):
        sessions = Sessions(self.legacy)
        self.write(self.legacy, 'original')
        sessions.create('kept')
        named = sessions.active_path
        self.write(named, 'named history')
        index = named.parent / 'index.json'
        reopened = Sessions(self.legacy)
        for bad in (b'{broken', json.dumps({'version': 1, 'active': '../../elsewhere',
                                            'titles': ['Default']}).encode(), b'x' * 65537):
            index.write_bytes(bad)
            for use in (lambda: reopened.active_path, reopened.list, reopened.create):
                with self.assertRaises(SessionError):
                    use()
            self.assertEqual(index.read_bytes(), bad)
        self.assertEqual(read_history(self.legacy)[0]['content'], 'original')
        self.assertEqual(read_history(named)[0]['content'], 'named history')

    def test_missing_index_in_populated_store_fails_closed(self):
        sessions = Sessions(self.legacy)
        sessions.create('named')
        named = sessions.active_path
        self.write(named, 'keep this')
        index = named.parent / 'index.json'
        index.unlink()
        reopened = Sessions(self.legacy)
        for use in (lambda: reopened.active_path, reopened.list, reopened.create,
                    lambda: reopened.switch(1), lambda: reopened.title('new title'),
                    lambda: reopened.search('keep'), reopened.export):
            with self.subTest(use=use), self.assertRaises(SessionError):
                use()
        self.assertFalse(index.exists())
        self.assertEqual(read_history(named)[0]['content'], 'keep this')

    def test_create_syncs_store_entry_and_index(self):
        sessions = Sessions(self.legacy)
        synced = []
        sync = os.fsync

        def track(fd):
            synced.append(Path(os.readlink(f'/proc/self/fd/{fd}')))
            sync(fd)

        with patch('minis_sessions.os.fsync', side_effect=track):
            sessions.create()
        store = sessions.active_path.parent
        self.assertEqual(synced[0], self.root)
        self.assertEqual(synced[1].parent, store)
        self.assertTrue(synced[1].name.startswith('.index.json.'))
        self.assertEqual(synced[2:], [store])

    def test_failed_replace_preserves_index_and_cleans_temporary_file(self):
        sessions = Sessions(self.legacy)
        sessions.create('original')
        folder = sessions.active_path.parent
        with patch('minis_sessions.os.replace', side_effect=OSError('fixture replace failure')):
            with self.assertRaises(SessionError):
                sessions.title('unsaved')
        self.assertEqual(sorted(item.name for item in folder.iterdir()), ['index.json'])
        self.assertEqual(Sessions(self.legacy).list()[-1].title, 'original')

    def test_session_count_is_bounded_and_failure_does_not_change_active(self):
        sessions = Sessions(self.legacy)
        with patch('minis_sessions.MAX_SESSIONS', 2):
            sessions.create('second')
            sessions.switch(1)
            with self.assertRaises(SessionError):
                sessions.create('third')
            self.assertEqual(sessions.active_path, self.legacy)

    def test_next_id_does_not_adopt_an_unindexed_history(self):
        sessions = Sessions(self.legacy)
        sessions.create()
        orphan = sessions.active_path.with_name('3.json')
        self.write(orphan, 'unindexed')
        with self.assertRaises(SessionError):
            sessions.create()
        self.assertEqual([s.id for s in sessions.list()], [1, 2])
        self.assertEqual(read_history(orphan)[0]['content'], 'unindexed')

    def test_symlinked_store_and_index_are_not_used(self):
        other = Sessions(self.root / 'other.json')
        other.create()
        other_folder = other.active_path.parent
        store = self.root / (self.legacy.name + '.sessions')
        store.symlink_to(other_folder, target_is_directory=True)
        sessions = Sessions(self.legacy)
        with self.assertRaises(SessionError):
            sessions.list()
        store.unlink()
        store.mkdir(mode=0o700)
        (store / 'index.json').symlink_to(other_folder / 'index.json')
        with self.assertRaises(SessionError):
            sessions.create()
        self.assertTrue((store / 'index.json').is_symlink())

    def test_search_is_scoped_by_full_filename_and_ignores_unindexed_files(self):
        first = Sessions(self.legacy)
        self.write(first.active_path, 'needle legacy')
        first.create('project')
        self.write(first.active_path, 'needle project')
        self.write(first.active_path.with_name('3.json'), 'needle unindexed')
        for name in ('bot-42-owner-2.json', 'bot-41-owner-1.json', 'bot-terminal-owner-1.json'):
            other = Sessions(self.root / name)
            self.write(other.active_path, 'needle other legacy')
            other.create()
            self.write(other.active_path, 'needle other named')
        result = first.search('NEEDLE')
        self.assertEqual([(h.session_id, h.role, h.snippet) for h in result.hits],
                         [(2, 'user', 'needle project'), (1, 'user', 'needle legacy')])
        self.assertEqual(result.skipped_ids, ())
        self.assertEqual([h.session_id for h in first.search('needle', session_id='1').hits], [1])
        self.assertNotIn('needle legacy', first.export())

    def test_search_and_export_only_show_plain_user_and_assistant_content(self):
        save_history(self.legacy, [
            {'role': 'system', 'content': 'needle system'},
            {'role': 'user', 'content': 'needle user'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{'content': 'needle argument'}]},
            {'role': 'tool', 'content': 'needle tool'},
            {'role': 'user', 'content': [{'text': 'needle rich content'}]},
            {'role': 'assistant', 'content': 'needle answer', 'reasoning_content': 'needle hidden'},
            {'role': ['assistant'], 'content': 'needle invalid role'},
        ])
        sessions = Sessions(self.legacy)
        self.assertEqual([(h.role, h.message_index) for h in sessions.search('needle').hits],
                         [('assistant', 6), ('user', 2)])
        md = sessions.export()
        self.assertIn('## User\n\n    needle user\n', md)
        self.assertIn('## Assistant\n\n    needle answer\n', md)
        self.assertLess(md.index('needle user'), md.index('needle answer'))
        self.assertEqual(md.count('needle '), 2)

    def test_search_bounds_query_and_results_and_shows_context_near_hit(self):
        sessions = Sessions(self.legacy)
        self.write(self.legacy, 'z' * 800 + ' Straße\n计划\x1b ' + 'z' * 300)
        hit, = sessions.search('STRASSE').hits
        self.assertIn('Straße', hit.snippet)
        self.assertNotIn('\x1b', hit.snippet)
        self.assertNotIn('\n', hit.snippet)
        self.assertLessEqual(len(hit.snippet), 180)
        for query in ('', '   ', 'q' * 201):
            with self.assertRaises(SessionError):
                sessions.search(query)
        save_history(self.legacy, [{'role': 'user', 'content': 'result'}] * 25)
        self.assertEqual(len(sessions.search('result', limit=3).hits), 3)
        self.assertEqual(len(sessions.search('result').hits), 10)

    def test_search_skips_corrupt_history_and_does_not_open_unused_histories(self):
        sessions = Sessions(self.legacy)
        self.legacy.write_bytes(b'{bad json')
        sessions.create()
        self.write(sessions.active_path, 'needle good')
        found = sessions.search('needle')
        self.assertEqual(found.skipped_ids, (1,))
        self.assertEqual(len(found.hits), 1)
        self.assertEqual(sessions.search('needle', limit=1).skipped_ids, ())
        with self.assertRaises(SessionError):
            sessions.export(1)
        self.assertEqual(self.legacy.read_bytes(), b'{bad json')

    def test_symlink_hardlink_and_fifo_transcripts_are_not_read(self):
        outside = self.root / 'outside.json'
        self.write(outside, 'private outside')
        sessions = Sessions(self.legacy)
        for kind in ('symlink', 'hardlink', 'fifo'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    self.legacy.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, self.legacy)
                else:
                    os.mkfifo(self.legacy)
                try:
                    with self.assertRaises(SessionError):
                        _ = sessions.active_path
                    with self.assertRaises(SessionError):
                        sessions.switch(1)
                    with self.assertRaises(SessionError):
                        sessions.export()
                    self.assertEqual(sessions.search('private').skipped_ids, (1,))
                finally:
                    self.legacy.unlink()

    def test_export_redacts_common_secrets_and_quotes_untrusted_markdown_as_text(self):
        sessions = Sessions(self.legacy)
        sessions.title('token=title-fixture-value')
        self.write(self.legacy, '\n'.join((
            'MINIS_API_KEY="fake fixture value"', 'Authorization: Bearer abc.fixture.123',
            'password = pass-fixture', 'ghp_fixtureabcdefghijklmno',
            'https://user:fixture@private.invalid/path',
            '-----BEGIN OPENSSH PRIVATE KEY-----', 'fixture-private-pem', '-----END OPENSSH PRIVATE KEY-----',
            '```', '![render](https://example.invalid/external.png)', '<script>fixture</script>', 'bad\ud800\x1b',
        )))
        raw = self.legacy.read_bytes()
        md = sessions.export('1')
        for secret in ('title-fixture-value', 'fake fixture value', 'abc.fixture.123', 'pass-fixture',
                       'ghp_fixture', 'user:fixture@', 'fixture-private-pem'):
            self.assertNotIn(secret, md)
        self.assertIn('分享前请检查', md)
        self.assertIn('    ![render](', md)
        self.assertIn('    <script>', md)
        self.assertIn('    ```', md)
        self.assertNotIn('\n<script>', md)
        self.assertNotIn('\x1b', md)
        md.encode('utf-8')
        self.assertEqual(self.legacy.read_bytes(), raw)
        self.assertNotIn('fake fixture value', sessions.search('MINIS_API_KEY').hits[0].snippet)

    def test_export_is_bounded_utf8_and_marks_empty_or_truncated_transcripts(self):
        sessions = Sessions(self.legacy)
        self.assertIn('没有可导出的消息', sessions.export())
        self.write(self.legacy, 'some text: ' + '界' * 5000)
        with patch('minis_sessions.MAX_EXPORT_BYTES', 800):
            md = sessions.export()
        self.assertLessEqual(len(md.encode('utf-8')), 800)
        self.assertIn('some text', md)
        self.assertIn('截断', md)

    def test_search_budget_skips_big_histories_and_still_checks_small_ones(self):
        sessions = Sessions(self.legacy)
        self.write(self.legacy, 'needle small')
        sessions.create()
        self.write(sessions.active_path, 'needle big' + 'a' * 2000)
        with patch('minis_sessions.MAX_SEARCH_BYTES', 400):
            result = sessions.search('needle')
        self.assertEqual(result.skipped_ids, (2,))
        self.assertEqual([h.session_id for h in result.hits], [1])

    def test_export_rejects_history_beyond_per_file_read_limit(self):
        sessions = Sessions(self.legacy)
        self.write(self.legacy, 'needle ' * 200)
        with patch('minis_sessions.HISTORY_LIMIT', 400):
            with self.assertRaises(SessionError):
                sessions.export()
            self.assertEqual(sessions.search('needle').skipped_ids, (1,))

    def test_export_redacts_whole_authorization_and_cookie_headers(self):
        sessions = Sessions(self.legacy)
        self.write(self.legacy, 'Authorization: Digest username="fixture-user", response="fixture-response"\n'
                   'Cookie: foo=fixture-cookie; bar=fixture-other\nnot a secret')
        md = sessions.export()
        self.assertNotIn('fixture-', md)
        self.assertIn('not a secret', md)
