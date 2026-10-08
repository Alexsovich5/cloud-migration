"""
Dependency discovery on source hosts.

``parse_netstat`` and ``parse_ss`` turn ``netstat -tan`` / ``ss -tan`` output
into listening ports and outbound peers. ``SSHProbe`` runs those commands on
a host over SSH with paramiko, verifying the host key before it sends any
credentials and bounding each command's output size and run time.
``merge_dependencies`` combines what was observed with the dependencies
declared in config.
"""

import base64
import hashlib
import logging
import os
import socket
import threading
import time

import paramiko

logger = logging.getLogger('discovery')

IPV4_MAPPED_PREFIX = '::ffff:'


class DiscoveryError(Exception):
    """Raised when neither netstat nor ss produced usable output, or a command
    exceeded its output or time limit."""


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


DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
DEFAULT_COMMAND_TIMEOUT = 60
READ_CHUNK = 32768
POLL_INTERVAL = 0.02
# Extra time the watchdog allows before it closes the transport, so the read
# loop normally reports the deadline itself.
WATCHDOG_GRACE = 2


class HostKeyError(paramiko.SSHException):
    """The host key is unknown, changed, or does not match the pinned fingerprint."""


def fingerprint(key):
    """OpenSSH-style SHA256 fingerprint of a paramiko key: ``SHA256:<base64>``."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return 'SHA256:' + base64.b64encode(digest).decode('ascii').rstrip('=')


def _normalise_fingerprint(value):
    value = value.strip()
    if value[:7].upper() == 'SHA256:':
        value = value[7:]
    return 'SHA256:' + value.rstrip('=')


class HostKeyVerifier(paramiko.RejectPolicy):
    """Check a source host's key before any credentials are sent.

    Like ``RejectPolicy`` it refuses hosts it has no key for, unless a
    fingerprint is pinned or trust-on-first-use is switched on.

    ``SSHProbe`` loads no host keys into its ``SSHClient``, so paramiko calls
    ``missing_host_key`` for every connection, after key exchange and before
    authentication. The key is accepted when it matches the pinned
    fingerprint (if one is set) and the known_hosts entry for the host (if
    there is one). A host with no entry is accepted only with a pinned
    fingerprint, or with ``trust_on_first_use``, which appends the key to the
    configured known_hosts file and logs its fingerprint.
    """

    def __init__(self, known_hosts=None, fingerprint=None, trust_on_first_use=False,
                 system_host_keys=True):
        self.known_hosts_path = os.path.expanduser(known_hosts) if known_hosts else None
        self.pinned = _normalise_fingerprint(fingerprint) if fingerprint else None
        self.trust_on_first_use = bool(trust_on_first_use)
        if self.trust_on_first_use and not self.known_hosts_path:
            raise ValueError('trust_on_first_use needs a known_hosts file to record keys in')
        self.host_keys = paramiko.HostKeys()
        if system_host_keys:
            system = os.path.expanduser('~/.ssh/known_hosts')
            if os.path.exists(system):
                self.host_keys.load(system)
        if self.known_hosts_path and os.path.exists(self.known_hosts_path):
            self.host_keys.load(self.known_hosts_path)

    def missing_host_key(self, client, hostname, key):
        seen = fingerprint(key)
        if self.pinned is not None and seen != self.pinned:
            raise HostKeyError('{0}: host key {1} does not match the pinned fingerprint '
                               '{2}'.format(hostname, seen, self.pinned))
        known = self.host_keys.lookup(hostname) or {}
        if known:
            listed = known.get(key.get_name())
            if listed is None or listed.asbytes() != key.asbytes():
                raise HostKeyError('{0}: host key changed: server offered {1} {2}, '
                                   'known_hosts lists {3}'.format(
                                       hostname, key.get_name(), seen,
                                       ', '.join(sorted(known.keys()))))
            return
        if self.pinned is not None:
            return
        if self.trust_on_first_use:
            self._record(hostname, key)
            logger.warning("%s: trusting host key %s %s on first use; recorded in %s",
                           hostname, key.get_name(), seen, self.known_hosts_path)
            return
        raise HostKeyError('{0}: unknown host key {1} {2}; add it to known_hosts, set '
                           'host_key_fingerprint, or enable trust_on_first_use'.format(
                               hostname, key.get_name(), seen))

    def _record(self, hostname, key):
        directory = os.path.dirname(os.path.abspath(self.known_hosts_path))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(self.known_hosts_path, 'a') as handle:
            handle.write('{0} {1} {2}\n'.format(hostname, key.get_name(), key.get_base64()))
        self.host_keys.add(hostname, key.get_name(), key)


def read_bounded(channel, host, command, max_bytes, timeout):
    """Read stdout and stderr of an exec channel together, within limits.

    Both streams are drained as data arrives, so a command that fills its
    stderr window cannot stall while stdout is being read. The combined
    output may not exceed ``max_bytes`` and the command must finish within
    ``timeout`` seconds; otherwise the channel is closed and DiscoveryError
    names the host and the limit. Returns ``(status, stdout, stderr)``.
    """
    deadline = time.time() + timeout
    out, err = [], []
    total = 0
    try:
        while True:
            progressed = False
            if channel.recv_ready():
                data = channel.recv(READ_CHUNK)
                out.append(data)
                total += len(data)
                progressed = True
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(READ_CHUNK)
                err.append(data)
                total += len(data)
                progressed = True
            if total > max_bytes:
                raise DiscoveryError("{0}: '{1}' output exceeded {2} bytes".format(
                    host, command, max_bytes))
            if (not progressed and channel.exit_status_ready() and
                    not channel.recv_ready() and not channel.recv_stderr_ready()):
                break
            if time.time() >= deadline:
                raise DiscoveryError("{0}: '{1}' did not finish within {2}s".format(
                    host, command, timeout))
            if not progressed:
                time.sleep(POLL_INTERVAL)
        status = channel.recv_exit_status()
    finally:
        channel.close()
    return (status, b''.join(out).decode('utf-8', 'replace'),
            b''.join(err).decode('utf-8', 'replace'))


class SSHProbe(object):
    """Collect TCP listeners and outbound peers from a host over SSH."""

    COMMANDS = (('netstat -tan', parse_netstat), ('ss -tan', parse_ss))

    def __init__(self, host, port=22, username=None, key_file=None, password=None,
                 timeout=10, known_hosts=None, host_key_fingerprint=None,
                 trust_on_first_use=False, max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
                 command_timeout=DEFAULT_COMMAND_TIMEOUT, system_host_keys=True):
        self.host = host
        self.port = port
        self.username = username
        self.key_file = os.path.expanduser(key_file) if key_file else None
        self.password = password
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.command_timeout = command_timeout
        self.verifier = HostKeyVerifier(known_hosts, host_key_fingerprint,
                                        trust_on_first_use, system_host_keys)

    def _connect(self):
        client = paramiko.SSHClient()
        # No host keys are loaded into the client itself: the verifier holds
        # them, so it runs for every connection before authentication.
        client.set_missing_host_key_policy(self.verifier)
        try:
            client.connect(self.host, port=self.port, username=self.username,
                           password=self.password, key_filename=self.key_file,
                           timeout=self.timeout, look_for_keys=False,
                           allow_agent=False)
        except Exception:
            client.close()
            raise
        return client

    def _run(self, client, command):
        transport = client.get_transport()
        expired = []

        def expire():
            expired.append(True)
            transport.close()

        # Opening the channel and starting the command wait without a limit
        # in paramiko; the watchdog bounds them by closing the transport.
        watchdog = threading.Timer(self.command_timeout + WATCHDOG_GRACE, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            channel = transport.open_session()
            channel.exec_command(command)
            return read_bounded(channel, self.host, command, self.max_output_bytes,
                                self.command_timeout)
        except (paramiko.SSHException, EOFError, socket.error):
            if expired:
                raise DiscoveryError("{0}: '{1}' did not finish within {2}s".format(
                    self.host, command, self.command_timeout))
            raise
        finally:
            watchdog.cancel()

    def collect(self):
        """Run netstat, falling back to ss on a non-zero exit.

        paramiko errors (authentication, host key, connection) propagate to
        the caller; output and time limits raise DiscoveryError.
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
                    timeout=source.get('timeout', 10),
                    known_hosts=source.get('known_hosts'),
                    host_key_fingerprint=source.get('host_key_fingerprint'),
                    trust_on_first_use=source.get('trust_on_first_use', False),
                    max_output_bytes=source.get('max_output_bytes',
                                                DEFAULT_MAX_OUTPUT_BYTES),
                    command_timeout=source.get('command_timeout', DEFAULT_COMMAND_TIMEOUT))


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
