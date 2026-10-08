import os
import re

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
README = os.path.join(ROOT, 'README.md')

FORBIDDEN = ['ACORIA', 'Status-Complete', '99.9', '60%', '35%', 'Developed during']


def _readme():
    with open(README) as handle:
        return handle.read()


def _layout_paths(text):
    """Turn the indented tree in the Layout section into relative paths."""
    match = re.search(r'^## Layout\s*\n+```\n(.*?)\n```', text, re.M | re.S)
    assert match, 'README has no fenced Layout block'
    stack = []
    paths = []
    for line in match.group(1).split('\n'):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(' '))
        assert indent % 2 == 0, 'odd indentation in layout line: {0!r}'.format(line)
        depth = indent // 2
        assert depth <= len(stack), 'layout line skips a level: {0!r}'.format(line)
        stack = stack[:depth]
        name = line.strip()
        if name.endswith('/'):
            stack.append(name.rstrip('/'))
            paths.append(('dir', '/'.join(stack)))
        else:
            paths.append(('file', '/'.join(stack + [name])))
    return paths


def test_layout_parser_reads_nested_tree():
    text = '## Layout\n\n```\nsrc/\n  a.py\ntests/\n  unit/\n    t.py\nMakefile\n```\n'
    assert _layout_paths(text) == [
        ('dir', 'src'), ('file', 'src/a.py'), ('dir', 'tests'),
        ('dir', 'tests/unit'), ('file', 'tests/unit/t.py'), ('file', 'Makefile'),
    ]


def test_every_layout_path_exists():
    paths = _layout_paths(_readme())
    files = [p for kind, p in paths if kind == 'file']
    assert len(files) > 20
    missing = []
    for kind, rel in paths:
        full = os.path.join(ROOT, rel)
        if kind == 'dir' and not os.path.isdir(full):
            missing.append(rel)
        if kind == 'file' and not os.path.isfile(full):
            missing.append(rel)
    assert missing == []


def test_mentions_simulators():
    text = _readme()
    assert 'moto' in text
    assert 'simulated' in text


@pytest.mark.parametrize('needle', FORBIDDEN)
def test_has_no_fabricated_claims(needle):
    assert needle not in _readme()


def test_has_no_template_placeholders():
    text = _readme()
    assert '{{' not in text
    assert 'delete this comment' not in text
