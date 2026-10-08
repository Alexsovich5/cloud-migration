import os

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
    assert result['ready'] is True


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
