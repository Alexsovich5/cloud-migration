"""Capture log records in tests (pytest 2.8 has no caplog fixture)."""

import logging

import pytest


class _ListHandler(logging.Handler):

    def __init__(self):
        logging.Handler.__init__(self, level=logging.DEBUG)
        self.setFormatter(logging.Formatter('%(name)s %(levelname)s %(message)s'))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))

    def text(self):
        return '\n'.join(self.lines)


@pytest.yield_fixture
def logs():
    """Collect every record logged anywhere while the test runs."""
    handler = _ListHandler()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
