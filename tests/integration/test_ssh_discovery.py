import json
import os
import socket
import time

import paramiko
import pytest

import assessment
import discovery
import sizing
from tests.support.fake_ssh import FakeSSHServer, host_key, known_hosts_line, read_fixture

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRICING_FILE = os.path.join(REPO_ROOT, 'config', 'pricing.yml')
SENTINEL = 'S3NTINEL-pw'

pytestmark = pytest.mark.integration


@pytest.yield_fixture
def server():
    with FakeSSHServer(username='migrate', password='pw') as srv:
        yield srv


@pytest.yield_fixture
def server_without_netstat():
    with FakeSSHServer(username='migrate', password='pw', netstat_exit=127) as srv:
        yield srv


def known_hosts(srv, tmpdir):
    return srv.write_known_hosts(str(tmpdir.join('known_hosts')))


def probe(srv, tmpdir, password='pw', **kwargs):
    if 'known_hosts' not in kwargs:
        kwargs['known_hosts'] = known_hosts(srv, tmpdir)
    kwargs.setdefault('timeout', 5)
    return discovery.SSHProbe('127.0.0.1', srv.port, 'migrate', password=password,
                              system_host_keys=False, **kwargs)


def test_collect_over_ssh_returns_parsed_netstat(server, tmpdir):
    result = probe(server, tmpdir).collect()
    assert result == discovery.parse_netstat(read_fixture('netstat_tan.txt'))
    assert result['listening'] == [22, 8080]
    assert ('10.0.0.30', 3306) in result['established']
    assert ('10.0.0.40', 11211) in result['established']
    assert server.commands == ['netstat -tan']


def test_collect_falls_back_to_ss_when_netstat_missing(server_without_netstat, tmpdir):
    result = probe(server_without_netstat, tmpdir).collect()
    assert server_without_netstat.commands == ['netstat -tan', 'ss -tan']
    assert result == discovery.parse_ss(read_fixture('ss_tan.txt'))
    assert result['listening'] == [22, 8080]


def test_wrong_password_raises_authentication_error(server, tmpdir):
    with pytest.raises(paramiko.AuthenticationException):
        probe(server, tmpdir, password='nope').collect()
    assert server.commands == []


# Host key verification

def test_unknown_host_key_is_rejected_before_any_password_is_sent(server, tmpdir):
    empty = str(tmpdir.join('empty_known_hosts'))
    open(empty, 'w').close()

    with pytest.raises(discovery.HostKeyError) as err:
        probe(server, tmpdir, known_hosts=empty).collect()

    assert 'unknown host key' in str(err.value)
    assert discovery.fingerprint(host_key()) in str(err.value)
    assert server.auth_attempts == []
    assert server.commands == []


def test_changed_host_key_is_rejected_before_any_password_is_sent(server, tmpdir):
    other = paramiko.RSAKey.generate(1024)
    path = str(tmpdir.join('changed_known_hosts'))
    with open(path, 'w') as handle:
        handle.write(known_hosts_line(server.port, other))

    with pytest.raises(discovery.HostKeyError) as err:
        probe(server, tmpdir, known_hosts=path).collect()

    assert 'changed' in str(err.value)
    assert server.auth_attempts == []


def test_pinned_fingerprint_is_accepted_without_known_hosts(server, tmpdir):
    pin = discovery.fingerprint(host_key())
    result = probe(server, tmpdir, known_hosts=None, host_key_fingerprint=pin).collect()
    assert result['listening'] == [22, 8080]


def test_wrong_pinned_fingerprint_is_rejected_even_if_key_is_known(server, tmpdir):
    other = discovery.fingerprint(paramiko.RSAKey.generate(1024))
    with pytest.raises(discovery.HostKeyError) as err:
        probe(server, tmpdir, host_key_fingerprint=other).collect()
    assert 'pinned' in str(err.value)
    assert server.auth_attempts == []


def test_trust_on_first_use_records_key_and_later_runs_verify_it(server, tmpdir, logs):
    path = str(tmpdir.join('tofu_known_hosts'))

    first = probe(server, tmpdir, known_hosts=path, trust_on_first_use=True).collect()

    assert first['listening'] == [22, 8080]
    with open(path) as handle:
        assert handle.read() == known_hosts_line(server.port, host_key())
    assert discovery.fingerprint(host_key()) in logs.text()
    second = probe(server, tmpdir, known_hosts=path).collect()
    assert second == first


def test_no_auto_add_policy_in_discovery_module():
    with open(os.path.join(REPO_ROOT, 'src', 'discovery.py')) as handle:
        assert 'AutoAddPolicy' not in handle.read()


# Bounded command output

def endless(channel):
    block = b'x' * 65536
    while True:
        channel.sendall(block)


def trickle(channel):
    while True:
        channel.sendall(b'.')
        time.sleep(0.1)


def big_stderr(channel):
    channel.sendall_stderr(b'e' * (3 * 1024 * 1024))
    channel.sendall(read_fixture('netstat_tan.txt').encode('utf-8'))
    channel.send_exit_status(0)


def test_endless_output_stops_at_the_byte_cap(tmpdir):
    with FakeSSHServer(handlers={'netstat -tan': endless}) as srv:
        started = time.time()
        with pytest.raises(discovery.DiscoveryError) as err:
            probe(srv, tmpdir, max_output_bytes=256 * 1024, command_timeout=30).collect()
    assert time.time() - started < 20
    assert '127.0.0.1' in str(err.value)
    assert '262144 bytes' in str(err.value)


def test_default_cap_is_one_mib():
    assert discovery.DEFAULT_MAX_OUTPUT_BYTES == 1024 * 1024
    assert discovery.SSHProbe('h').max_output_bytes == 1024 * 1024


def test_trickling_output_stops_at_the_deadline(tmpdir):
    with FakeSSHServer(handlers={'netstat -tan': trickle}) as srv:
        started = time.time()
        with pytest.raises(discovery.DiscoveryError) as err:
            probe(srv, tmpdir, command_timeout=1).collect()
    elapsed = time.time() - started
    assert 1 <= elapsed < 10
    assert 'did not finish within 1s' in str(err.value)


def test_large_stderr_with_small_stdout_does_not_deadlock(tmpdir):
    with FakeSSHServer(handlers={'netstat -tan': big_stderr}) as srv:
        result = probe(srv, tmpdir, max_output_bytes=8 * 1024 * 1024,
                       command_timeout=30).collect()
    assert result == discovery.parse_netstat(read_fixture('netstat_tan.txt'))


# Assessment through the probe

def workload(name, port, password, known_hosts_path, **source):
    src = {'host': '127.0.0.1', 'port': port, 'username': 'migrate',
           'password': password, 'known_hosts': known_hosts_path, 'timeout': 5,
           'known_endpoints': {'10.0.0.30:3306': 'db.internal:3306'}}
    src.update(source)
    return {
        'name': name, 'type': 'stateless', 'runtime': 'python',
        'cpu': 2, 'memory': 4, 'storage': 50, 'containerizable': True,
        'database': {'engine': 'mysql', 'host': 'db.internal', 'port': 3306,
                     'storage': 100},
        'source': src,
    }


def catalog():
    return sizing.Catalog.from_file(PRICING_FILE)


def test_assess_with_ssh_probe_merges_observed_dependencies(server, tmpdir):
    w = workload('web-portal', server.port, 'pw', known_hosts(server, tmpdir))
    result = assessment.assess([w], catalog(), probe_factory=discovery.probe_from_source)[0]
    assert result['dependencies'][0]['observed'] is True
    assert result['dependencies'][-1] == {
        'type': 'undeclared', 'endpoint': '10.0.0.40:11211', 'source': 'ssh'}
    assert 'discovery_error' not in result
    assert result['ready'] is True


def test_assess_records_discovery_error_on_bad_password(server, tmpdir):
    w = workload('web-portal', server.port, 'nope', known_hosts(server, tmpdir))
    result = assessment.assess([w], catalog(), probe_factory=discovery.probe_from_source)[0]
    assert 'Authentication failed' in result['discovery_error']
    assert result['listening_ports'] == []
    assert result['estimated_cost']['total'] == 182.21
    assert result['ready'] is False


def test_one_misbehaving_host_does_not_stop_discovery_of_the_others(server, tmpdir):
    with FakeSSHServer(handlers={'netstat -tan': endless}) as bad:
        bad_hosts = bad.write_known_hosts(str(tmpdir.join('bad_known_hosts')))
        workloads = [
            workload('bad-host', bad.port, 'pw', bad_hosts, max_output_bytes=65536),
            workload('good-host', server.port, 'pw', known_hosts(server, tmpdir)),
        ]
        results = assessment.assess(workloads, catalog(),
                                    probe_factory=discovery.probe_from_source)
    assert '65536 bytes' in results[0]['discovery_error']
    assert 'discovery_error' not in results[1]
    assert results[1]['dependencies'][0]['observed'] is True


def closed_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_ssh_password_never_reaches_errors_logs_output_or_results(
        server, tmpdir, logs, capsys):
    empty = str(tmpdir.join('empty_known_hosts'))
    open(empty, 'w').close()
    good_hosts = known_hosts(server, tmpdir)
    with FakeSSHServer(password=SENTINEL, handlers={'netstat -tan': trickle}) as slow:
        slow_hosts = slow.write_known_hosts(str(tmpdir.join('slow_known_hosts')))
        workloads = [
            workload('auth-failure', server.port, SENTINEL, good_hosts),
            workload('unknown-key', server.port, SENTINEL, empty),
            workload('refused', closed_port(), SENTINEL, good_hosts),
            workload('timeout', slow.port, SENTINEL, slow_hosts, command_timeout=1),
        ]
        results = assessment.assess(workloads, catalog(),
                                    probe_factory=discovery.probe_from_source)
    path = assessment.write_results(results, str(tmpdir.join('results.json')))

    assert all(r.get('discovery_error') for r in results)
    for r in results:
        assert SENTINEL not in r['discovery_error']
    with open(path) as handle:
        assert SENTINEL not in handle.read()
    assert SENTINEL not in json.dumps(results)
    assert SENTINEL not in logs.text()
    out, err = capsys.readouterr()
    assert SENTINEL not in out + err
