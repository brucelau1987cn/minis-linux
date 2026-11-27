"""Local action proposals. This module is not an authentication boundary.

Pass an existing home/state directory, the workspace and writable skill overlay
paths, and save_memory(text) (None is also accepted if memory is unavailable).
Use the same stable, parent-generated scope (surface + owner + session) on every
call for a session, or physically separate state dirs. The parent must buffer
model tool calls, stage them only after the turn commits, and invoke approve only
for an explicit owner `/approve ID` in the matching surface/session. Never send
pending previews or IDs back to the model. Preview text is untrusted and may
contain private data; escape it for display and show it fully before approval.
"""
import ctypes
from contextlib import contextmanager
from difflib import unified_diff
import errno
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import unicodedata

from minis import FILE_LIMIT, Workspace
from minis_skills import SLUG


_SPECS = {
    'propose_create_file': ('Propose creating a new workspace text file; never overwrite.', ('path', 'content')),
    'propose_edit_file': ('Propose replacing the text of an existing workspace file.', ('path', 'content')),
    'propose_delete_file': ('Propose moving one regular workspace file to private trash.', ('path',)),
    'propose_memory': ('Propose saving one item of long-term memory.', ('text',)),
    'propose_skill': ('Propose installing one new text-only Markdown skill.', ('name', 'description', 'content')),
}
ACTION_TOOLS = [{'type': 'function', 'function': {
    'name': name, 'description': description + ' Does not execute; requires explicit owner approval.',
    'parameters': {'type': 'object', 'properties': {field: {'type': 'string'} for field in fields},
                   'required': list(fields), 'additionalProperties': False}}}
                for name, (description, fields) in _SPECS.items()]

_ACTION_ID = re.compile(r'[0-9a-f]{32}\Z')
_STATE_TEMP = '.state.tmp'
_STATE_LIMIT = 8 * 1024 * 1024
_MAX_PENDING = 32
_MAX_AUDIT = 256
_MAX_TRASH = 128
_TRASH_FILE_LIMIT = 8 * 1024 * 1024
_MEMORY_LIMIT = 240
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class ActionError(ValueError):
    """User-safe rejection (never includes payloads, raw paths or callback errors)."""


def _path(value):
    path = Path(value).absolute()
    if path.anchor != '/' or '..' in path.parts:
        raise ActionError('Configured paths cannot contain parent traversal.')
    return path


def _open_dir(parent, name, *, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        # Persist even if a second process opened the directory right after mkdir.
        os.fsync(parent)
    return os.open(name, _DIR_FLAGS, dir_fd=parent)


@contextmanager
def _directory(path, *, create_leaf=False):
    """Open each directory component without following links; pin via dirfd."""
    fd = os.open('/', _DIR_FLAGS)
    try:
        for index, name in enumerate(path.parts[1:], 2):
            child = _open_dir(fd, name, create=create_leaf and index == len(path.parts))
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _text(value, limit=FILE_LIMIT, *, multiline=True):
    if not isinstance(value, str) or len(value) > limit:
        raise ActionError('Invalid or oversized text.')
    if any((not multiline or char not in '\n\r\t') and unicodedata.category(char).startswith('C')
           for char in value):
        raise ActionError('Text contains unsupported characters.')
    if len(value.encode('utf-8')) > limit:
        raise ActionError('Text exceeds the byte limit.')
    return value


def _name(name):
    _text(name, 240, multiline=False)
    try:
        Workspace.validate_name(name)
        if len(name.splitlines()) != 1:
            raise ValueError
    except ValueError:
        raise ActionError('Use a non-hidden filename in the workspace root.') from None
    return name


def _identity(fd):
    meta = os.fstat(fd)
    return [meta.st_dev, meta.st_ino]


def _no_entry(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ActionError('The destination already exists.')


def _regular(meta, limit):
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
        raise ActionError('Only single-link regular files are allowed.')
    if meta.st_size > limit:
        raise ActionError('The file exceeds the size limit.')
    # Atime may change on reads; every other relevant change invalidates approval.
    return [meta.st_dev, meta.st_ino, meta.st_mode, meta.st_nlink, meta.st_uid,
            meta.st_gid, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns]


def _snapshot(fd, name, limit):
    before = _regular(os.stat(name, dir_fd=fd, follow_symlinks=False), limit)
    with os.fdopen(os.open(name, _READ_FLAGS, dir_fd=fd), 'rb') as stream:
        if _regular(os.fstat(stream.fileno()), limit) != before:
            raise ActionError('Target changed; prepare a new proposal.')
        data = stream.read(limit + 1)
        if (len(data) != before[6] or _regular(os.fstat(stream.fileno()), limit) != before
                or _regular(os.stat(name, dir_fd=fd, follow_symlinks=False), limit) != before):
            raise ActionError('Target changed; prepare a new proposal.')
    return [sha256(data).hexdigest(), *before], data


def _diff(before, after):
    def lines_with_lf(text):
        *lines, tail = text.split('\n')
        return [line + '\n' for line in lines] + ([tail] if tail else [])

    old, new = lines_with_lf(before), lines_with_lf(after)
    if len(old) + len(new) <= 2048:
        lines = unified_diff(old, new, 'before', 'after')
    else:
        # Avoid quadratic matching on adversarial inputs. Show a full replacement hunk.
        lines = ['--- before\n', '+++ after\n', f'@@ -1,{len(old)} +1,{len(new)} @@\n',
                 *('-' + line for line in old), *('+' + line for line in new)]
    return ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                   for line in lines)


def _skill(args):
    name = _text(args['name'], 48, multiline=False)
    description = _text(args['description'], 640, multiline=False).strip()
    body = _text(args['content'])
    if (not SLUG.fullmatch(name) or not 0 < len(description) <= 160 or not body.strip()
            or len(description.splitlines()) != 1):
        raise ActionError('Invalid skill name, description or Markdown body.')
    quoted = json.dumps(description, ensure_ascii=False)
    if len(quoted) > 162:  # Existing catalog strips quotes but does not decode escapes.
        raise ActionError('The formatted skill description is too long.')
    return name, _text(f'---\nname: {name}\ndescription: {quoted}\n---\n{body}')


def _move_new(src_fd, src, dst_fd, dst):
    """Linux atomic rename without replacing even an empty directory or link."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise OSError(errno.ENOSYS, 'renameat2 is unavailable')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), 1) != 0:  # RENAME_NOREPLACE
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


@contextmanager
def _temp_file(fd, name, data):
    with os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=fd), 'wb') as stream:
        try:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
            yield name
        finally:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass


class ActionQueue:
    """Scoped durable proposals, serialized across processes sharing a state directory.

    stage(tool_name, args) -> {id, kind, expires_at, summary, detail}. ID is 32
    lowercase hex digits; expires_at is Unix time in seconds. preview(id) returns
    the same dict; list() returns active summaries without detail. cancel(id) and
    approve(id) return {id, kind, at, status}, as do the newest-first entries of
    audit(limit=50). Rejections raise ActionError. stage_skill_import(text) and
    stage_restore(trash_id) return previews and are operator-only entry points.

    `home` must exist, and `skills_dir` is the writable overlay, not the bundled
    catalog. If the overlay is absent, its parent must exist. On systemd, pass
    the trusted canonical state paths if StateDirectory is exposed via a link.
    All queues that can write the same workspace or skill overlay should share
    the same state directory (home/.minis-actions) so their writes serialize.
    The enclosing paths must be trusted; this is not a sandbox against other
    local processes with write access to those paths. File edits publish new
    mode-0600 files, not in-place changes. Edit/delete need private state on the
    workspace filesystem; unsupported atomic renames fail without a copy fallback.

    save_memory(text) runs under the lock; do not re-enter the queue. Return False
    or raise to report a failure; any other return means success. The parent is
    responsible for serializing direct memory commands with this callback.

    Approval claims are persisted before application. A lone 'started' means
    interrupted/unknown, never retried. On interrupted or failed application,
    inspect the workspace, action audit and trash before proposing another action.
    A process killed mid-edit can leave a recoverable original in trash; an
    interrupted file/skill publish may leave a hidden temporary file/directory
    named for the action ID for local inspection/removal. There is no auto-purge
    of trash; only a local operator may archive or remove it outside the queue.
    """

    def __init__(self, home, workspace, skills_dir, save_memory=None, *, scope='', ttl_seconds=900):
        self.state_dir = _path(home) / '.minis-actions'
        self.workspace, self.skills_dir = _path(workspace), _path(skills_dir)
        if any(a == b or a in b.parents or b in a.parents for a, b in
               ((self.state_dir, self.workspace), (self.state_dir, self.skills_dir),
                (self.workspace, self.skills_dir))):
            raise ActionError('Workspace, skill overlay and private action state must be separate.')
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ActionError('Proposal lifetime must be between 1 and 86400 seconds.')
        self.ttl_seconds = ttl_seconds
        self.save_memory = save_memory
        # Scope and configured paths are not copied into the audit log.
        binding = json.dumps([_text(scope, 512, multiline=False), str(self.workspace), str(self.skills_dir)])
        self._binding = sha256(binding.encode()).hexdigest()

    @contextmanager
    def _locked(self):
        try:
            with _directory(self.state_dir, create_leaf=True) as fd:
                meta = os.fstat(fd)
                if meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
                    raise ActionError('Action state directory must be private.')
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    os.unlink(_STATE_TEMP, dir_fd=fd)
                    os.fsync(fd)
                except FileNotFoundError:
                    pass
                state = self._load(fd)
                if self._expire(state):
                    self._save(fd, state)
                yield fd, state
        except (OSError, UnicodeError, KeyError, TypeError, RecursionError, IndexError):
            raise ActionError('Action storage or filesystem operation failed.') from None

    @staticmethod
    def _load(fd):
        try:
            state_file = os.open('state.json', _READ_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            return {'pending': {}, 'audit': [], 'trash': {}}
        with os.fdopen(state_file, 'rb') as stream:
            meta = os.fstat(stream.fileno())
            if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != os.geteuid()
                    or meta.st_mode & 0o077 or meta.st_size > _STATE_LIMIT):
                raise ActionError('Unsafe or oversized action state.')
            data = stream.read(_STATE_LIMIT + 1)
        try:
            if len(data) > _STATE_LIMIT:
                raise ValueError
            state = json.loads(data)
            if (not isinstance(state, dict) or set(state) != {'pending', 'audit', 'trash'}
                    or not isinstance(state['pending'], dict) or len(state['pending']) > _MAX_PENDING
                    or not isinstance(state['trash'], dict) or len(state['trash']) > _MAX_TRASH
                    or not isinstance(state['audit'], list) or len(state['audit']) > _MAX_AUDIT):
                raise ValueError
            if (any(not _ACTION_ID.fullmatch(key) or p['id'] != key
                    or (p['kind'] == 'restore_file' and not _ACTION_ID.fullmatch(p['trash_id']))
                    for key, p in state['pending'].items())
                    or any(not _ACTION_ID.fullmatch(key) for key in state['trash'])
                    or any(set(event) != {'id', 'kind', 'at', 'status'} for _, event in state['audit'])):
                raise ValueError
        except ValueError:
            raise ActionError('Unreadable action state.') from None
        return state

    @staticmethod
    def _save(fd, state):
        data = json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode()
        if len(data) > _STATE_LIMIT:
            raise ActionError('Action state exceeds its size limit.')
        with _temp_file(fd, _STATE_TEMP, data) as temp:
            os.replace(temp, 'state.json', src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)

    @staticmethod
    def _event(state, proposal, status):
        entry = {key: proposal[key] for key in ('id', 'kind')}
        entry.update(status=status, at=int(time.time()))
        state['audit'].append([proposal['binding'], entry])
        del state['audit'][:-_MAX_AUDIT]
        return entry

    def _expire(self, state):
        now = time.time()
        expired = [p for p in state['pending'].values() if p['expires_at'] <= now]
        for p in expired:
            del state['pending'][p['id']]
            self._event(state, p, 'expired')
        return bool(expired)

    def _find(self, state, action_id):
        if not isinstance(action_id, str) or not _ACTION_ID.fullmatch(action_id):
            raise ActionError('Invalid action ID.')
        p = state['pending'].get(action_id)
        if p is None or p['binding'] != self._binding:
            raise ActionError('No pending action with that ID in this session.')
        return p

    @staticmethod
    def _view(p, *, detail=True):
        view = {key: p[key] for key in ('id', 'kind', 'expires_at', 'summary')}
        if detail:
            view['detail'] = p['detail']
        return view

    def stage(self, tool_name, args):
        if (not isinstance(tool_name, str) or tool_name not in _SPECS or not isinstance(args, dict)
                or args.keys() != set(_SPECS[tool_name][1])):
            raise ActionError('Unknown action or unexpected arguments.')
        with self._locked() as (fd, state):
            if tool_name == 'propose_memory':
                text = _text(args['text'], 4 * _MEMORY_LIMIT, multiline=False).strip()
                if (self.save_memory is None or not 0 < len(text) <= _MEMORY_LIMIT
                        or len(text.splitlines()) != 1):
                    raise ActionError('Memory is unavailable or the item is not one bounded line.')
                data = {'summary': 'Save one long-term memory item', 'detail': text}
            elif tool_name == 'propose_skill':
                name, document = _skill(args)
                with _directory(self.skills_dir.parent) as parent:
                    try:
                        skills = _open_dir(parent, self.skills_dir.name)
                    except FileNotFoundError:
                        root = None
                    else:
                        try:
                            _no_entry(skills, name)
                            root = _identity(skills)
                        finally:
                            os.close(skills)
                    data = {'summary': f'Install skill {name!r}', 'detail': document,
                            'name': name, 'root': root, 'parent': _identity(parent)}
            else:
                data = self._prepare_file(tool_name, args)
            return self._stage(fd, state, tool_name, data)

    def _prepare_file(self, kind, args):
        name = _name(args['path'])
        with _directory(self.workspace) as workspace:
            data = {'root': _identity(workspace), 'name': name}
            if kind == 'propose_create_file':
                _no_entry(workspace, name)
                return dict(data, summary=f'Create {name!r}', detail=_text(args['content']))
            limit = FILE_LIMIT if kind == 'propose_edit_file' else _TRASH_FILE_LIMIT
            target, old = _snapshot(workspace, name, limit)
            data['target'] = target
            if kind == 'propose_delete_file':
                return dict(data, summary=f'Move {name!r} to private trash', detail=f'{len(old)} bytes')
            before, content = _text(old.decode('utf-8')), _text(args['content'])
            if before == content:
                raise ActionError('The proposed text is unchanged.')
            return dict(data, summary=f'Edit {name!r}', detail=_diff(before, content), content=content)

    def _stage(self, fd, state, kind, data):
        if len(state['pending']) >= _MAX_PENDING:
            raise ActionError('The pending action queue is full.')
        action_id = secrets.token_hex(16)
        p = dict(data, id=action_id, binding=self._binding, kind=kind,
                 expires_at=time.time() + self.ttl_seconds)
        state['pending'][action_id] = p
        self._event(state, p, 'staged')
        self._save(fd, state)
        return self._view(p)

    def stage_skill_import(self, text):
        """Stage trusted local SKILL.md text. Caller reads at most FILE_LIMIT bytes.

        Accept simple frontmatter (exactly name and description) and a Markdown
        body. No YAML evaluation, additional files or remote source fetching.
        """
        text = _text(text).replace('\r\n', '\n')
        opening, _, rest = text.partition('\n')
        header, closing, content = rest.partition('\n---\n')
        if opening != '---' or not closing or len(header) > 2048:
            raise ActionError('Import requires name and description frontmatter.')
        args = {'content': content}
        for line in header.split('\n'):
            key, colon, value = line.partition(':')
            if not colon or key not in ('name', 'description') or key in args:
                raise ActionError('Import frontmatter must contain only name and description.')
            value = value.strip()
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            args[key] = value
        return self.stage('propose_skill', args)

    def trash_list(self):
        """List recoverable files for this scope (id, path, size, status)."""
        with self._locked() as (fd, state):
            return [{'id': key, 'path': item['name'], 'size': item['target'][7],
                     'status': 'recovery' if item['recovery'] else 'trashed'}
                    for key, item in state['trash'].items() if item['binding'] == self._binding
                    and self._trash_exists(fd, key, item)]

    def stage_restore(self, trash_id):
        """Stage an operator-only restore, to the original name; approve as usual."""
        if not isinstance(trash_id, str) or not _ACTION_ID.fullmatch(trash_id):
            raise ActionError('Invalid trash ID.')
        with self._locked() as (fd, state), _directory(self.workspace) as workspace:
            item = state['trash'].get(trash_id)
            if (item is None or item['binding'] != self._binding or item['root'] != _identity(workspace)
                    or not self._trash_exists(fd, trash_id, item)):
                raise ActionError('No recoverable file with that ID in this session.')
            name = _name(item['name'])
            _no_entry(workspace, name)
            return self._stage(fd, state, 'restore_file', dict(item, trash_id=trash_id,
                               summary=f'Restore {name!r} from private trash', detail=f'{item["target"][7]} bytes'))

    @staticmethod
    def _trash_exists(fd, key, item):
        try:
            meta = os.stat('trash-' + key, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        # Rename changes ctime. Never offer a raced replacement or special entry for restore.
        return (stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1
                and _regular(meta, _TRASH_FILE_LIMIT)[:-1] == item['target'][1:-1])

    def preview(self, action_id):
        with self._locked() as (_, state):
            return self._view(self._find(state, action_id))

    def list(self):
        with self._locked() as (_, state):
            return [self._view(p, detail=False) for p in state['pending'].values()
                    if p['binding'] == self._binding]

    def cancel(self, action_id):
        with self._locked() as (fd, state):
            p = self._find(state, action_id)
            del state['pending'][action_id]
            entry = self._event(state, p, 'cancelled')
            self._save(fd, state)
            return entry

    def approve(self, action_id):
        with self._locked() as (fd, state):
            p = self._find(state, action_id)
            del state['pending'][action_id]
            # Claim durably before external effects. No retry after a crash.
            if p['kind'] in ('propose_edit_file', 'propose_delete_file'):
                # Save recovery metadata *before* touching the workspace. A killed
                # process may leave the original file in private trash.
                for key in list(state['trash']):
                    try:
                        os.stat('trash-' + key, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        del state['trash'][key]
                if len(state['trash']) >= _MAX_TRASH:
                    self._event(state, p, 'failed')
                    self._save(fd, state)
                    raise ActionError('Private trash is full; restore or archive files locally first.')
                state['trash'][action_id] = {key: p[key] for key in ('binding', 'name', 'root', 'target')}
                state['trash'][action_id]['recovery'] = p['kind'] == 'propose_edit_file'
            self._event(state, p, 'started')
            self._save(fd, state)
            try:
                if p['expires_at'] <= time.time():
                    raise ActionError('Approval expired before application.')
                self._apply(fd, p)
            except (OSError, ActionError) as error:
                self._event(state, p, 'failed')
                self._save(fd, state)
                if isinstance(error, ActionError):
                    raise
                raise ActionError('Action did not finish; inspect workspace and trash before retrying.') from None
            if p['kind'] == 'propose_edit_file':
                del state['trash'][action_id]
            elif p['kind'] == 'restore_file':
                del state['trash'][p['trash_id']]
            entry = self._event(state, p, 'applied')
            self._save(fd, state)
            return entry

    def _apply(self, fd, p):
        kind = p['kind']
        if kind == 'propose_memory':
            if self.save_memory is None:
                raise ActionError('Memory is unavailable.')
            try:
                result = self.save_memory(p['detail'])
            except Exception:
                raise ActionError('Memory save did not finish; check memory before retrying.') from None
            if result is False:
                raise ActionError('The memory store declined the item.')
            return
        if kind == 'propose_skill':
            self._install_skill(p)
            return
        with _directory(self.workspace) as workspace:
            if _identity(workspace) != p['root']:
                raise ActionError('Workspace changed; prepare a new proposal.')
            name = _name(p['name'])
            if kind == 'restore_file':
                source = 'trash-' + p['trash_id']
                target, _ = _snapshot(fd, source, _TRASH_FILE_LIMIT)
                if target[:-1] != p['target'][:-1]:
                    raise ActionError('Trash file changed; restore was refused.')
                _move_new(fd, source, workspace, name)
                os.fsync(workspace)
                os.fsync(fd)
            elif kind == 'propose_create_file':
                with _temp_file(workspace, '.minis-file-' + p['id'], p['detail'].encode('utf-8')) as temp:
                    _move_new(workspace, temp, workspace, name)
                    os.fsync(workspace)
            elif kind == 'propose_delete_file':
                self._stash(fd, workspace, name, p, _TRASH_FILE_LIMIT)
            elif kind == 'propose_edit_file':
                with _temp_file(workspace, '.minis-file-' + p['id'], p['content'].encode('utf-8')) as temp:
                    source = self._stash(fd, workspace, name, p, FILE_LIMIT)
                    try:
                        _move_new(workspace, temp, workspace, name)
                    except OSError:
                        self._put_back(fd, source, workspace, name)
                        raise
                    os.fsync(workspace)
                    os.unlink(source, dir_fd=fd)
                    os.fsync(fd)
            else:
                raise ActionError('Unrecognized pending action.')

    @staticmethod
    def _put_back(fd, source, workspace, name):
        try:
            _move_new(fd, source, workspace, name)
        except FileExistsError:
            # Preserve both entries. Never replace a concurrent writer's file.
            return
        os.fsync(workspace)
        os.fsync(fd)

    def _stash(self, fd, workspace, name, p, limit):
        target, _ = _snapshot(workspace, name, limit)
        if target != p['target']:
            raise ActionError('Target changed; prepare a new proposal.')
        source = 'trash-' + p['id']
        _move_new(workspace, name, fd, source)
        try:
            moved, _ = _snapshot(fd, source, limit)
            if moved[:-1] != target[:-1]:
                raise ActionError('Target changed; prepare a new proposal.')
        except (ActionError, OSError):
            self._put_back(fd, source, workspace, name)
            raise
        os.fsync(fd)
        os.fsync(workspace)
        return source

    def _install_skill(self, p):
        name = p['name']
        if not SLUG.fullmatch(name):
            raise ActionError('Invalid skill name.')
        with _directory(self.skills_dir.parent) as parent:
            if _identity(parent) != p['parent']:
                raise ActionError('Skill directory changed; prepare a new proposal.')
            skills = _open_dir(parent, self.skills_dir.name, create=p['root'] is None)
        try:
            if p['root'] is not None and _identity(skills) != p['root']:
                raise ActionError('Skill directory changed; prepare a new proposal.')
            temp = '.minis-skill-' + p['id']
            os.mkdir(temp, 0o700, dir_fd=skills)
            child = os.open(temp, _DIR_FLAGS, dir_fd=skills)
            try:
                with _temp_file(child, '.text.tmp', p['detail'].encode('utf-8')) as text:
                    _move_new(child, text, child, 'SKILL.md')
                os.fsync(child)
                _move_new(skills, temp, skills, name)
            except (OSError, ActionError):
                try:
                    os.unlink('SKILL.md', dir_fd=child)
                except FileNotFoundError:
                    pass
                os.rmdir(temp, dir_fd=skills)
                raise
            finally:
                os.close(child)
            os.fsync(skills)
        finally:
            os.close(skills)

    @contextmanager
    def record_attempt(self, ident, kind):
        """Record only metadata for a caller's already-authorized export or upload."""
        if not _ACTION_ID.fullmatch(ident) or kind not in ('attachment_send', 'history_export'):
            raise ActionError('Invalid audit metadata.')
        item = {'binding': self._binding, 'id': ident, 'kind': kind}

        def mark(status):
            with self._locked() as (fd, state):
                self._event(state, item, status)
                self._save(fd, state)

        mark('started')
        try:
            yield
        except Exception:
            mark('failed')
            raise
        else:
            mark('applied')

    def audit(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= _MAX_AUDIT:
            raise ActionError('Invalid audit limit.')
        with self._locked() as (_, state):
            return [entry for binding, entry in reversed(state['audit']) if binding == self._binding][:limit]
