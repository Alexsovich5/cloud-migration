import base64
import hashlib
import os

import paramiko
import pytest

import assessment
import discovery
import sizing

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(REPO_ROOT, 'tests', 'fixtures')
PRICING_FILE = os.path.join(REPO_ROOT, 'config', 'pricing.yml')


def read_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


@pytest.mark.parametrize('parser,fixture', [
    (discovery.parse_netstat, 'netstat_tan.txt'),
    (discovery.parse_ss, 'ss_tan.txt'),
])
def test_parsers_extract_listeners_and_outbound_peers(parser, fixture):
    result = parser(read_fixture(fixture))
    assert result['listening'] == [22, 8080]
    assert ('10.0.0.30', 3306) in result['established']
    assert ('10.0.0.40', 11211) in result['established']


@pytest.mark.parametrize('parser,fixture', [
    (discovery.parse_netstat, 'netstat_tan.txt'),
    (discovery.parse_ss, 'ss_tan.txt'),
])
def test_parsers_drop_inbound_and_non_established(parser, fixture):
    result = parser(read_fixture(fixture))
    # Inbound clients of 22 and 8080, and the TIME_WAIT row, are not peers.
    assert result['established'] == [('10.0.0.30', 3306), ('10.0.0.40', 11211)]


def test_netstat_and_ss_agree():
    assert (discovery.parse_netstat(read_fixture('netstat_tan.txt')) ==
            discovery.parse_ss(read_fixture('ss_tan.txt')))


def test_parse_netstat_ipv6_only_listener():
    text = (
        "Proto Recv-Q Send-Q Local Address           Foreign Address         State\n"
        "tcp6       0      0 :::5432                 :::*                    LISTEN\n"
        "tcp        0      0 10.0.0.12:5432          10.0.0.9:40000          ESTABLISHED\n"
        "tcp        0      0 10.0.0.12:40100         10.0.0.50:6379          ESTABLISHED\n"
    )
    result = discovery.parse_netstat(text)
    assert result == {'listening': [5432], 'established': [('10.0.0.50', 6379)]}


def test_parsers_handle_empty_output():
    assert discovery.parse_netstat('') == {'listening': [], 'established': []}
    assert discovery.parse_ss('') == {'listening': [], 'established': []}


def declared_portal_deps():
    return assessment.discover_declared_dependencies({
        'database': {'engine': 'mysql', 'host': 'db.internal', 'port': 3306},
        'services': [{'name': 'auth-service', 'endpoint': 'auth.internal:5000'}],
    })


def test_merge_marks_known_peer_and_adds_undeclared():
    observed = discovery.parse_netstat(read_fixture('netstat_tan.txt'))
    merged = discovery.merge_dependencies(
        declared_portal_deps(), observed,
        known_endpoints={'10.0.0.30:3306': 'db.internal:3306'})

    db = [d for d in merged if d['type'] == 'database'][0]
    assert db['observed'] is True
    svc = [d for d in merged if d['type'] == 'service'][0]
    assert not svc.get('observed')
    undeclared = [d for d in merged if d['type'] == 'undeclared']
    assert undeclared == [
        {'type': 'undeclared', 'endpoint': '10.0.0.40:11211', 'source': 'ssh'}]
    assert len(merged) == 3


def test_merge_without_known_endpoints_reports_every_peer():
    observed = {'listening': [8080], 'established': [('10.0.0.30', 3306)]}
    merged = discovery.merge_dependencies(declared_portal_deps(), observed, {})
    assert merged[-1] == {'type': 'undeclared', 'endpoint': '10.0.0.30:3306', 'source': 'ssh'}
    assert not any(d.get('observed') for d in merged[:-1])


def test_merge_does_not_mutate_declared():
    declared = declared_portal_deps()
    discovery.merge_dependencies(
        declared, {'listening': [], 'established': [('10.0.0.30', 3306)]},
        {'10.0.0.30:3306': 'db.internal:3306'})
    assert 'observed' not in declared[0]


class FakeProbe(object):
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def collect(self):
        if self.error is not None:
            raise self.error
        return self.result


def portal_workload():
    return {
        'name': 'web-portal', 'type': 'stateless', 'runtime': 'python',
        'cpu': 2, 'memory': 4, 'storage': 50, 'containerizable': True,
        'services': [{'name': 'auth-service', 'endpoint': 'auth.internal:5000'}],
        'database': {'engine': 'mysql', 'host': 'db.internal', 'port': 3306,
                     'storage': 100},
        'source': {'host': '10.0.0.12', 'port': 22, 'username': 'migrate',
                   'known_endpoints': {'10.0.0.30:3306': 'db.internal:3306'}},
    }


def test_assess_merges_probe_results():
    catalog = sizing.Catalog.from_file(PRICING_FILE)
    observed = discovery.parse_netstat(read_fixture('netstat_tan.txt'))
    seen = []

    def factory(source):
        seen.append(source)
        return FakeProbe(result=observed)

    result = assessment.assess([portal_workload()], catalog, probe_factory=factory)[0]
    assert seen[0]['host'] == '10.0.0.12'
    assert result['dependencies'][0]['observed'] is True
    assert result['dependencies'][-1] == {
        'type': 'undeclared', 'endpoint': '10.0.0.40:11211', 'source': 'ssh'}
    # The SSH port the probe connected through is not reported as an app port.
    assert result['listening_ports'] == [8080]
    assert 'discovery_error' not in result


def test_assess_records_discovery_error_and_continues():
    catalog = sizing.Catalog.from_file(PRICING_FILE)

    def factory(source):
        return FakeProbe(error=discovery.DiscoveryError('netstat and ss both failed'))

    result = assessment.assess([portal_workload()], catalog, probe_factory=factory)[0]
    assert 'netstat and ss both failed' in result['discovery_error']
    assert result['listening_ports'] == []
    assert [d['type'] for d in result['dependencies']] == ['database', 'service']
    # Unknown dependencies are not "no dependencies": the workload is not ready.
    assert result['ready'] is False
    assert result['blockers'] == ['Dependency discovery failed; dependencies are unverified']


def test_assess_skips_probe_without_source_or_factory():
    catalog = sizing.Catalog.from_file(PRICING_FILE)
    calls = []
    w = portal_workload()
    del w['source']
    result = assessment.assess([w], catalog, probe_factory=calls.append)[0]
    assert calls == []
    assert result['listening_ports'] == []

    result = assessment.assess([portal_workload()], catalog)[0]
    assert result['listening_ports'] == []
    assert 'discovery_error' not in result


# Host key verification

KEY = paramiko.RSAKey.generate(1024)
OTHER = paramiko.RSAKey.generate(1024)
HOST = '[10.0.0.12]:2222'


def write_known_hosts(tmpdir, key, host=HOST):
    path = str(tmpdir.join('known_hosts'))
    with open(path, 'w') as handle:
        handle.write('{0} {1} {2}\n'.format(host, key.get_name(), key.get_base64()))
    return path


def test_fingerprint_is_openssh_sha256_format():
    digest = hashlib.sha256(KEY.asbytes()).digest()
    expected = 'SHA256:' + base64.b64encode(digest).decode('ascii').rstrip('=')
    assert discovery.fingerprint(KEY) == expected


def test_verifier_rejects_unknown_key_by_default(tmpdir):
    verifier = discovery.HostKeyVerifier(system_host_keys=False)
    with pytest.raises(discovery.HostKeyError) as err:
        verifier.missing_host_key(None, HOST, KEY)
    assert 'unknown host key' in str(err.value)
    assert isinstance(err.value, paramiko.SSHException)
    assert isinstance(verifier, paramiko.RejectPolicy)


def test_verifier_accepts_key_listed_in_known_hosts(tmpdir):
    verifier = discovery.HostKeyVerifier(write_known_hosts(tmpdir, KEY),
                                         system_host_keys=False)
    verifier.missing_host_key(None, HOST, KEY)


def test_verifier_rejects_changed_key(tmpdir):
    verifier = discovery.HostKeyVerifier(write_known_hosts(tmpdir, OTHER),
                                         system_host_keys=False)
    with pytest.raises(discovery.HostKeyError) as err:
        verifier.missing_host_key(None, HOST, KEY)
    assert 'changed' in str(err.value)


@pytest.mark.parametrize('form', ['full', 'bare', 'padded'])
def test_verifier_accepts_pinned_fingerprint(form):
    pin = discovery.fingerprint(KEY)
    if form == 'bare':
        pin = pin[len('SHA256:'):]
    elif form == 'padded':
        pin += '='
    verifier = discovery.HostKeyVerifier(fingerprint=pin, system_host_keys=False)
    verifier.missing_host_key(None, HOST, KEY)


def test_verifier_rejects_pin_mismatch_even_for_known_key(tmpdir):
    verifier = discovery.HostKeyVerifier(write_known_hosts(tmpdir, KEY),
                                         fingerprint=discovery.fingerprint(OTHER),
                                         system_host_keys=False)
    with pytest.raises(discovery.HostKeyError) as err:
        verifier.missing_host_key(None, HOST, KEY)
    assert 'pinned' in str(err.value)


def test_trust_on_first_use_appends_to_known_hosts(tmpdir, logs):
    path = str(tmpdir.join('ssh', 'known_hosts'))
    verifier = discovery.HostKeyVerifier(path, trust_on_first_use=True,
                                         system_host_keys=False)
    verifier.missing_host_key(None, HOST, KEY)

    with open(path) as handle:
        assert handle.read() == '{0} {1} {2}\n'.format(HOST, KEY.get_name(), KEY.get_base64())
    assert discovery.fingerprint(KEY) in logs.text()
    assert 'first use' in logs.text()
    # Once recorded, a different key for the same host is a changed key.
    with pytest.raises(discovery.HostKeyError):
        verifier.missing_host_key(None, HOST, OTHER)


def test_trust_on_first_use_requires_a_known_hosts_file():
    with pytest.raises(ValueError):
        discovery.HostKeyVerifier(trust_on_first_use=True, system_host_keys=False)


def test_probe_from_source_passes_host_key_and_limit_settings():
    probe = discovery.probe_from_source({
        'host': '10.0.0.12', 'port': 2222, 'known_hosts': '/etc/migration/known_hosts',
        'host_key_fingerprint': discovery.fingerprint(KEY), 'trust_on_first_use': False,
        'max_output_bytes': 4096, 'command_timeout': 7})
    assert probe.verifier.known_hosts_path == '/etc/migration/known_hosts'
    assert probe.verifier.pinned == discovery.fingerprint(KEY)
    assert probe.verifier.trust_on_first_use is False
    assert probe.max_output_bytes == 4096
    assert probe.command_timeout == 7


# Bounded command reader

class ScriptedChannel(object):
    """Channel stand-in that hands out queued stdout/stderr chunks."""

    def __init__(self, stdout=(), stderr=(), status=0, endless=None):
        self.stdout = list(stdout)
        self.stderr = list(stderr)
        self.status = status
        self.endless = endless
        self.closed = False

    def recv_ready(self):
        return bool(self.stdout) or self.endless is not None

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv(self, size):
        if self.endless is not None:
            return self.endless[:size]
        return self.stdout.pop(0)

    def recv_stderr(self, size):
        return self.stderr.pop(0)

    def exit_status_ready(self):
        return self.endless is None and not self.stdout and not self.stderr

    def recv_exit_status(self):
        return self.status

    def close(self):
        self.closed = True


def test_read_bounded_returns_both_streams_and_status():
    channel = ScriptedChannel([b'out1 ', b'out2'], [b'err'], status=3)
    status, out, err = discovery.read_bounded(channel, 'h1', 'netstat -tan', 1024, 5)
    assert (status, out, err) == (3, 'out1 out2', 'err')


def test_read_bounded_counts_stderr_towards_the_cap():
    channel = ScriptedChannel([b'a' * 10], [b'b' * 20])
    with pytest.raises(discovery.DiscoveryError) as err:
        discovery.read_bounded(channel, 'h1', 'netstat -tan', 25, 5)
    assert "h1: 'netstat -tan' output exceeded 25 bytes" in str(err.value)
    assert channel.closed


def test_read_bounded_stops_endless_output_at_the_cap():
    channel = ScriptedChannel(endless=b'x' * 4096)
    with pytest.raises(discovery.DiscoveryError):
        discovery.read_bounded(channel, 'h1', 'netstat -tan', 10000, 5)
    assert channel.closed


def test_read_bounded_enforces_overall_deadline():
    channel = ScriptedChannel()
    channel.exit_status_ready = lambda: False
    with pytest.raises(discovery.DiscoveryError) as err:
        discovery.read_bounded(channel, 'h1', 'ss -tan', 1024, 0.2)
    assert "h1: 'ss -tan' did not finish within 0.2s" in str(err.value)
    assert channel.closed
