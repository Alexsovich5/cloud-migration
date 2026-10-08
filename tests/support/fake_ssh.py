"""
A small in-process SSH server for discovery tests.

It listens on 127.0.0.1 on a free port, accepts one username/password pair
and answers ``exec`` requests for ``netstat -tan`` and ``ss -tan`` with
fixture text. ``netstat_exit=127`` makes netstat behave as if it were not
installed, so the ``ss`` fallback can be exercised. ``handlers`` maps a
command to a function that writes the reply to the channel itself, for
misbehaving hosts (endless or trickling output, large stderr).

Every password attempt is recorded in ``auth_attempts``, so tests can show
that a rejected host key stops the client before it sends credentials.
``write_known_hosts`` exports the server's host key in OpenSSH format.
"""

import os
import socket
import threading
import time

import paramiko

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'fixtures')

_HOST_KEY = None
_HOST_KEY_LOCK = threading.Lock()


def host_key():
    """Generate the RSA host key once per test process."""
    global _HOST_KEY
    with _HOST_KEY_LOCK:
        if _HOST_KEY is None:
            _HOST_KEY = paramiko.RSAKey.generate(1024)
    return _HOST_KEY


def read_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


class _Server(paramiko.ServerInterface):

    def __init__(self, fake):
        self.fake = fake

    def get_allowed_auths(self, username):
        return 'password'

    def check_auth_password(self, username, password):
        self.fake.auth_attempts.append(username)
        if username == self.fake.username and password == self.fake.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        if isinstance(command, bytes):
            command = command.decode('utf-8')
        self.fake.commands.append(command)
        handler = self.fake.handlers.get(command.strip())
        if handler is not None:
            target, args = _run_handler, (channel, handler)
        else:
            target, args = _reply, (channel,) + self.fake.respond(command)
        thread = threading.Thread(target=target, args=args)
        thread.daemon = True
        thread.start()
        return True


# paramiko sends the exec-request acknowledgement only after
# check_channel_exec_request returns. Replying (and closing the channel)
# before that ack reaches the client makes the client see "Channel closed",
# so the reply thread waits briefly first.
REPLY_DELAY = 0.2


def _reply(channel, stdout, stderr, status):
    time.sleep(REPLY_DELAY)
    try:
        if stdout:
            channel.sendall(stdout.encode('utf-8'))
        if stderr:
            channel.sendall_stderr(stderr.encode('utf-8'))
        channel.send_exit_status(status)
    finally:
        channel.close()


def _run_handler(channel, handler):
    time.sleep(REPLY_DELAY)
    try:
        handler(channel)
    except (OSError, socket.error, EOFError):
        # The client closed the channel while the handler was still writing.
        pass
    finally:
        channel.close()


def known_hosts_line(port, key, host='127.0.0.1'):
    """One OpenSSH known_hosts line for ``key`` served on host:port."""
    name = host if port == 22 else '[{0}]:{1}'.format(host, port)
    return '{0} {1} {2}\n'.format(name, key.get_name(), key.get_base64())


class FakeSSHServer(object):
    """Threaded SSH server; use as a context manager or call start()/stop()."""

    def __init__(self, username='migrate', password='pw', netstat_exit=0, handlers=None):
        self.username = username
        self.password = password
        self.netstat_exit = netstat_exit
        self.handlers = dict(handlers or {})
        self.commands = []
        self.auth_attempts = []
        self.port = None
        self._sock = None
        self._thread = None
        self._transports = []
        self._stopping = False

    def respond(self, command):
        """Return (stdout, stderr, exit_status) for an exec request."""
        name = command.split()[0] if command.strip() else ''
        if command.strip() == 'netstat -tan':
            if self.netstat_exit != 0:
                return '', 'bash: netstat: command not found\n', self.netstat_exit
            return read_fixture('netstat_tan.txt'), '', 0
        if command.strip() == 'ss -tan':
            return read_fixture('ss_tan.txt'), '', 0
        return '', 'bash: {0}: command not found\n'.format(name), 127

    def write_known_hosts(self, path):
        """Write this server's host key to ``path`` and return the path."""
        with open(path, 'w') as handle:
            handle.write(known_hosts_line(self.port, host_key()))
        return path

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()
        return self

    def _serve(self):
        while not self._stopping:
            try:
                conn, _ = self._sock.accept()
            except (OSError, socket.error):
                return
            transport = paramiko.Transport(conn)
            transport.add_server_key(host_key())
            self._transports.append(transport)
            try:
                transport.start_server(server=_Server(self))
            except (paramiko.SSHException, EOFError, OSError, socket.error):
                transport.close()

    def stop(self):
        self._stopping = True
        for transport in self._transports:
            transport.close()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
