"""Bounded, read-only catalog of operator-installed Markdown skills."""
import json
from itertools import islice
import os
import re

from minis import Workspace

SLUG = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')
SKILL_TOOL = {'type': 'function', 'function': {
    'name': 'skill_view', 'description': 'Read the instructions for an installed skill listed in the catalog.',
    'parameters': {'type': 'object', 'properties': {'name': {'type': 'string'}},
                   'required': ['name'], 'additionalProperties': False}}}


class SkillCatalog:
    def __init__(self, root):
        self.entries = {}
        self._bodies = {}
        try:
            with Workspace(root) as directory:
                # Scan at most the first 128 entries, without following directory links.
                with os.scandir(directory.fd) as scan:
                    names = sorted(entry.name for entry in islice(scan, 128))
                for name in names:
                    if len(self.entries) >= 24:
                        break
                    if not SLUG.fullmatch(name):
                        continue
                    try:
                        with Workspace(name, dir_fd=directory.fd) as skill:
                            text = skill.read('SKILL.md')
                        self._add(name, text)
                    except (OSError, ValueError):
                        continue
        except FileNotFoundError:
            pass

    def _add(self, name, text):
        marker, _, rest = text.partition('\n')
        if marker != '---':
            return
        header, separator, body = rest.partition('\n---\n')
        if not separator or len(header) > 2048 or not body.strip():
            return
        lines = [line.partition(':')[2].strip() for line in header.splitlines()
                 if line.startswith('description:')]
        if len(lines) != 1:
            return
        description = lines[0]
        if len(description) >= 2 and description[0] in ("'", '"') and description[-1] == description[0]:
            description = description[1:-1].strip()
        if not 0 < len(description) <= 160:
            return
        self.entries[name] = description
        self._bodies[name] = body.strip()

    def summary(self):
        return json.dumps(self.entries, ensure_ascii=False)

    def view(self, name):
        try:
            return self._bodies[name]
        except KeyError:
            raise ValueError('未找到此技能；只允许加载已安装目录中的技能。') from None
