import json
import os
import socket
import time
from unittest import mock

import pytest

import validation
from state import MigrationState


@pytest.fixture
def listener(request):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    request.addfinalizer(server.close)
    return server.getsockname()


@pytest.fixture
def closed_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def st(tmpdir):
    return MigrationState(os.path.join(str(tmpdir), 'state.json'))


def test_tcp_check_true_against_listening_socket(listener):
    host, port = listener
    assert validation.tcp_check(host, port, 1) is True


def test_tcp_check_false_on_closed_port_within_one_second(closed_port):
    started = time.time()
    assert validation.tcp_check('127.0.0.1', closed_port, 1) is False
    assert time.time() - started < 1.0


def fake_aws(states):
    aws = mock.MagicMock()
    aws.instance_state.side_effect = lambda instance_id: states[instance_id]
    return aws


def test_validate_reports_stopped_instance(st):
    st.record('app', 'instance', id='i-x')
    validator = validation.Validator(fake_aws({'i-x': 'stopped'}), mock.MagicMock(),
                                     timeout=0, interval=0)

    ok, failures = validator.validate({'name': 'app'}, st)

    assert ok is False
    assert failures == ['instance i-x state stopped']


def test_validate_waits_for_pending_instance_to_run(st):
    st.record('app', 'instance', id='i-x')
    aws = mock.MagicMock()
    aws.instance_state.side_effect = ['pending', 'pending', 'running']
    validator = validation.Validator(aws, mock.MagicMock(), timeout=5, interval=0)

    assert validator.validate({'name': 'app'}, st) == (True, [])
    assert aws.instance_state.call_count == 3


def test_validate_tcp_and_data_checks(st, listener, closed_port, tmpdir):
    st.record('app', 'instance', id='i-x')
    manifest = os.path.join(str(tmpdir), 'app.manifest.json')
    with open(manifest, 'w') as handle:
        json.dump({'files': []}, handle)
    st.set('app', 'data', {'files': 1, 'bytes': 3, 'manifest': manifest})
    migrator = mock.MagicMock()
    migrator.verify.return_value = (False, ['app/a.txt: missing (404)'])
    validator = validation.Validator(fake_aws({'i-x': 'running'}), migrator,
                                     timeout=0, interval=0)
    workload = {'name': 'app',
                'validation': {'host': '127.0.0.1', 'port': closed_port, 'timeout': 1}}

    ok, failures = validator.validate(workload, st)

    migrator.verify.assert_called_once_with(manifest)
    assert ok is False
    assert failures == ['tcp 127.0.0.1:{0} unreachable'.format(closed_port),
                        'data app/a.txt: missing (404)']

    host, port = listener
    migrator.verify.return_value = (True, [])
    workload['validation'] = {'host': host, 'port': port, 'timeout': 1}
    assert validator.validate(workload, st) == (True, [])


def test_validate_skips_data_check_without_data(st):
    st.record('app', 'instance', id='i-x')
    migrator = mock.MagicMock()
    validator = validation.Validator(fake_aws({'i-x': 'running'}), migrator,
                                     timeout=0, interval=0)

    assert validator.validate({'name': 'app'}, st) == (True, [])
    assert not migrator.verify.called
