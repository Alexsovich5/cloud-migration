"""Post-migration checks: instance state, TCP reachability and data manifests."""

import socket
import time

RUNNING = 'running'
# States from which an instance can still reach ``running`` on its own.
TRANSITIONAL = ('pending',)


def tcp_check(host, port, timeout):
    """Return True if a TCP connection to host:port opens within ``timeout`` seconds."""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except (OSError, socket.error):
        return False
    sock.close()
    return True


class Validator(object):
    """Check a migrated workload against the resources recorded in its ledger."""

    def __init__(self, aws, data_migrator, timeout=60, interval=2):
        self.aws = aws
        self.data_migrator = data_migrator
        self.timeout = timeout
        self.interval = interval

    def _wait_running(self, instance_id):
        deadline = time.time() + self.timeout
        while True:
            current = self.aws.instance_state(instance_id)
            if current == RUNNING:
                return current
            if current not in TRANSITIONAL or time.time() >= deadline:
                return current
            time.sleep(self.interval)

    def validate(self, workload, state):
        """Return ``(ok, failures)`` where failures is a list of messages."""
        name = workload['name']
        failures = []

        for item in state.ledger(name):
            if item['kind'] != 'instance':
                continue
            current = self._wait_running(item['id'])
            if current != RUNNING:
                failures.append('instance {0} state {1}'.format(item['id'], current))

        check = workload.get('validation')
        if check:
            host, port = check['host'], check['port']
            if not tcp_check(host, port, check.get('timeout', 5)):
                failures.append('tcp {0}:{1} unreachable'.format(host, port))

        data = state.workload(name).get('data') or {}
        if data.get('manifest'):
            ok, problems = self.data_migrator.verify(data['manifest'])
            if not ok:
                failures.extend('data ' + problem for problem in problems)

        return not failures, failures
