"""
Dependency discovery on source hosts.

``parse_netstat`` and ``parse_ss`` turn ``netstat -tan`` / ``ss -tan`` output
into listening ports and outbound peers. ``SSHProbe`` runs those commands on
a host over SSH with paramiko. ``merge_dependencies`` combines what was
observed with the dependencies declared in config.
"""

import logging
import os

import paramiko

logger = logging.getLogger('discovery')

IPV4_MAPPED_PREFIX = '::ffff:'


class DiscoveryError(Exception):
    """Raised when neither netstat nor ss produced usable output."""


def _split_address(address):
    """Split 'ip:port' (IPv4, ``*:port``, ``:::port``, ``::ffff:a.b.c.d:port``).

    Returns ``(ip, port)`` with port as int, or ``(ip, None)`` for a wildcard
    port such as ``0.0.0.0:*``.
    """
    ip, _, port = address.rpartition(':')
    if ip.startswith('[') and ip.endswith(']'):
        ip = ip[1:-1]
    if ip.startswith(IPV4_MAPPED_PREFIX) and '.' in ip:
        ip = ip[len(IPV4_MAPPED_PREFIX):]
    if not port.isdigit():
        return ip, None
    return ip, int(port)


def _summarise(rows):
    """Build the result from ``(state, local, remote)`` rows.

    ``state`` is already normalised to 'LISTEN' or 'ESTABLISHED'. A peer is
    kept only when its local port is not a listening port, i.e. the host
    opened the connection.
    """
    listening = set()
    connections = []
    for state, local, remote in rows:
        _, local_port = _split_address(local)
        if local_port is None:
            continue
        if state == 'LISTEN':
            listening.add(local_port)
        else:
            connections.append((local_port, _split_address(remote)))
    established = set()
    for local_port, (peer_ip, peer_port) in connections:
        if local_port in listening or peer_port is None:
            continue
        established.add((peer_ip, peer_port))
    return {'listening': sorted(listening), 'established': sorted(established)}


def parse_netstat(text):
    """Parse ``netstat -tan`` output.

    Columns: Proto Recv-Q Send-Q Local Foreign State.
    """
    rows = []
    for line in (text or '').splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[0] not in ('tcp', 'tcp6'):
            continue
        state = fields[5]
        if state in ('LISTEN', 'ESTABLISHED'):
            rows.append((state, fields[3], fields[4]))
    return _summarise(rows)


def parse_ss(text):
    """Parse ``ss -tan`` output.

    Columns: State Recv-Q Send-Q Local:Port Peer:Port.
    """
    states = {'LISTEN': 'LISTEN', 'ESTAB': 'ESTABLISHED'}
    rows = []
    for line in (text or '').splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] not in states:
            continue
        rows.append((states[fields[0]], fields[3], fields[4]))
    return _summarise(rows)


class SSHProbe(object):
    """Collect TCP listeners and outbound peers from a host over SSH."""

    COMMANDS = (('netstat -tan', parse_netstat), ('ss -tan', parse_ss))

    def __init__(self, host, port=22, username=None, key_file=None, password=None,
                 timeout=10):
        self.host = host
        self.port = port
        self.username = username
        self.key_file = os.path.expanduser(key_file) if key_file else None
        self.password = password
        self.timeout = timeout

    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.host, port=self.port, username=self.username,
                       password=self.password, key_filename=self.key_file,
                       timeout=self.timeout, look_for_keys=False,
                       allow_agent=False)
        return client

    def _run(self, client, command):
        _, stdout, stderr = client.exec_command(command, timeout=self.timeout)
        out = stdout.read().decode('utf-8', 'replace')
        err = stderr.read().decode('utf-8', 'replace')
        status = stdout.channel.recv_exit_status()
        return status, out, err

    def collect(self):
        """Run netstat, falling back to ss on a non-zero exit.

        paramiko errors (authentication, connection) propagate to the caller.
        """
        client = self._connect()
        try:
            failures = []
            for command, parser in self.COMMANDS:
                status, out, err = self._run(client, command)
                if status == 0:
                    logger.info("%s: collected connections with '%s'", self.host, command)
                    return parser(out)
                failures.append("'{0}' exited {1}: {2}".format(
                    command, status, err.strip()))
            raise DiscoveryError("{0}: {1}".format(self.host, '; '.join(failures)))
        finally:
            client.close()


def probe_from_source(source):
    """Build an SSHProbe from a workload ``source`` block."""
    return SSHProbe(source['host'],
                    port=source.get('port', 22),
                    username=source.get('username'),
                    key_file=source.get('key_file'),
                    password=source.get('password'),
                    timeout=source.get('timeout', 10))


def _declared_endpoint(dep):
    if dep.get('type') == 'database':
        return '{0}:{1}'.format(dep.get('host', ''), dep.get('port', 0))
    return dep.get('endpoint', '')


def merge_dependencies(declared, observed, known_endpoints):
    """Merge observed peers into a copy of the declared dependency list.

    Each peer ``ip:port`` is translated through ``known_endpoints`` (for
    example ``{'10.0.0.30:3306': 'db.internal:3306'}``). If the result matches
    a declared dependency, that dependency gets ``observed: True``; otherwise
    an ``undeclared`` entry is appended.
    """
    known_endpoints = known_endpoints or {}
    merged = [dict(d) for d in declared]
    index = {}
    for dep in merged:
        index.setdefault(_declared_endpoint(dep), dep)
    for ip, port in observed.get('established', []):
        raw = '{0}:{1}'.format(ip, port)
        dep = index.get(known_endpoints.get(raw, raw))
        if dep is not None:
            dep['observed'] = True
        else:
            merged.append({'type': 'undeclared', 'endpoint': raw, 'source': 'ssh'})
    return merged
