"""Summarise the migration state file as a fixed-width table or as JSON."""

import json
from datetime import datetime

import state as state_mod

COLUMNS = (
    ('WORKLOAD', 'name', '<'),
    ('STATUS', 'status', '<'),
    ('STRATEGY', 'strategy', '<'),
    ('INSTANCE', 'instance', '<'),
    ('EST $/MO', 'monthly_cost', '>'),
    ('RESOURCES', 'resources', '>'),
    ('FILES', 'files', '>'),
    ('BYTES', 'bytes', '>'),
    ('ELAPSED', 'elapsed', '>'),
)


def _parse_time(value):
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError('unrecognised timestamp {0!r}'.format(value))


def elapsed_seconds(history):
    """Whole seconds between the first and last history timestamps."""
    if len(history) < 2:
        return 0
    first = _parse_time(history[0]['at'])
    last = _parse_time(history[-1]['at'])
    return max(0, int((last - first).total_seconds()))


def format_elapsed(seconds):
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return '{0}:{1:02d}:{2:02d}'.format(hours, minutes, secs)


def _assessment_index(assessment):
    return dict((item['name'], item) for item in (assessment or []))


def _rows(state, assessment):
    data = state.data if hasattr(state, 'data') else state
    assessed = _assessment_index(assessment)
    rows = []
    for name in sorted(data.get('workloads', {})):
        entry = data['workloads'][name]
        found = assessed.get(name)
        cost = None
        strategy = instance = None
        if found is not None:
            strategy = found.get('migration_strategy')
            instance = (found.get('resource_profile') or {}).get('recommended_instance')
            cost = (found.get('estimated_cost') or {}).get('total')
        moved = entry.get('data') or {}
        seconds = elapsed_seconds(entry.get('history') or [])
        rows.append({
            'name': name,
            'status': entry.get('status', state_mod.PENDING),
            'strategy': strategy,
            'instance': instance,
            'monthly_cost': cost,
            'resources': len(entry.get('ledger') or []),
            'files': moved.get('files', 0),
            'bytes': moved.get('bytes', 0),
            'elapsed': format_elapsed(seconds),
            'elapsed_seconds': seconds,
            'error': entry.get('error'),
        })
    return rows


def _totals(rows):
    by_status = {}
    for row in rows:
        by_status[row['status']] = by_status.get(row['status'], 0) + 1
    return {
        'workloads': len(rows),
        'by_status': by_status,
        'monthly_cost': round(sum(r['monthly_cost'] or 0 for r in rows), 2),
        'bytes': sum(r['bytes'] for r in rows),
    }


def _cell(row, key):
    value = row[key]
    if value is None:
        return '-'
    if key == 'monthly_cost':
        return '{0:.2f}'.format(value)
    return str(value)


def _status_order(status):
    if status in state_mod.STATUSES:
        return state_mod.STATUSES.index(status)
    return len(state_mod.STATUSES)


def _render_text(rows, totals):
    cells = [[_cell(row, key) for _, key, _ in COLUMNS] for row in rows]
    widths = [len(title) for title, _, _ in COLUMNS]
    for line in cells:
        widths = [max(w, len(c)) for w, c in zip(widths, line)]

    def fmt(values):
        parts = ['{0:{1}{2}}'.format(value, align, width)
                 for value, (_, _, align), width in zip(values, COLUMNS, widths)]
        return '  '.join(parts).rstrip()

    lines = [fmt([title for title, _, _ in COLUMNS])]
    lines.extend(fmt(line) for line in cells)
    lines.append('-' * len(lines[0]))
    counts = ', '.join('{0} {1}'.format(totals['by_status'][s], s)
                       for s in sorted(totals['by_status'], key=_status_order))
    lines.append('TOTAL {0} workloads ({1})  est ${2:.2f}/mo  {3} bytes'.format(
        totals['workloads'], counts, totals['monthly_cost'], totals['bytes']))
    return '\n'.join(lines)


def render(state, assessment=None, fmt='text'):
    """Render ``state`` (a MigrationState or its data dict) as text or JSON.

    ``assessment`` is the list written by ``--assess``; without it the
    strategy, instance and cost columns show ``-``.
    """
    rows = _rows(state, assessment)
    totals = _totals(rows)
    if fmt == 'json':
        return json.dumps({'workloads': rows, 'totals': totals}, indent=2, sort_keys=True)
    if fmt != 'text':
        raise ValueError('unknown report format {0!r}'.format(fmt))
    return _render_text(rows, totals)
