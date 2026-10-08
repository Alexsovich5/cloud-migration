import os

import paramiko
import pytest

import assessment
import discovery
import sizing
from tests.support.fake_ssh import FakeSSHServer, read_fixture

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRICING_FILE = os.path.join(REPO_ROOT, 'config', 'pricing.yml')

pytestmark = pytest.mark.integration


@pytest.yield_fixture
def server():
    with FakeSSHServer(username='migrate', password='pw') as srv:
        yield srv


@pytest.yield_fixture
def server_without_netstat():
    with FakeSSHServer(username='migrate', password='pw', netstat_exit=127) as srv:
        yield srv


def test_collect_over_ssh_returns_parsed_netstat(server):
    probe = discovery.SSHProbe('127.0.0.1', server.port, 'migrate', password='pw')
    result = probe.collect()
    assert result == discovery.parse_netstat(read_fixture('netstat_tan.txt'))
    assert result['listening'] == [22, 8080]
    assert ('10.0.0.30', 3306) in result['established']
    assert ('10.0.0.40', 11211) in result['established']
    assert server.commands == ['netstat -tan']


def test_collect_falls_back_to_ss_when_netstat_missing(server_without_netstat):
    probe = discovery.SSHProbe('127.0.0.1', server_without_netstat.port, 'migrate',
                               password='pw')
    result = probe.collect()
    assert server_without_netstat.commands == ['netstat -tan', 'ss -tan']
    assert result == discovery.parse_ss(read_fixture('ss_tan.txt'))
    assert result['listening'] == [22, 8080]


def test_wrong_password_raises_authentication_error(server):
    probe = discovery.SSHProbe('127.0.0.1', server.port, 'migrate', password='nope',
                               timeout=5)
    with pytest.raises(paramiko.AuthenticationException):
        probe.collect()
    assert server.commands == []


def workload(port, password):
    return {
        'name': 'web-portal', 'type': 'stateless', 'runtime': 'python',
        'cpu': 2, 'memory': 4, 'storage': 50, 'containerizable': True,
        'database': {'engine': 'mysql', 'host': 'db.internal', 'port': 3306,
                     'storage': 100},
        'source': {'host': '127.0.0.1', 'port': port, 'username': 'migrate',
                   'password': password,
                   'known_endpoints': {'10.0.0.30:3306': 'db.internal:3306'}},
    }


def test_assess_with_ssh_probe_merges_observed_dependencies(server):
    catalog = sizing.Catalog.from_file(PRICING_FILE)
    result = assessment.assess([workload(server.port, 'pw')], catalog,
                               probe_factory=discovery.probe_from_source)[0]
    assert result['dependencies'][0]['observed'] is True
    assert result['dependencies'][-1] == {
        'type': 'undeclared', 'endpoint': '10.0.0.40:11211', 'source': 'ssh'}
    assert 'discovery_error' not in result


def test_assess_records_discovery_error_on_bad_password(server):
    catalog = sizing.Catalog.from_file(PRICING_FILE)
    result = assessment.assess([workload(server.port, 'nope')], catalog,
                               probe_factory=discovery.probe_from_source)[0]
    assert 'Authentication failed' in result['discovery_error']
    assert result['listening_ports'] == []
    assert result['estimated_cost']['total'] == 182.21
