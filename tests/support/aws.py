"""Helpers for integration tests that talk to the moto simulators."""

import os
import socket
import time

import boto3
from botocore.client import Config

from urllib.parse import urlparse

SERVICES = {'ec2': 'MOTO_EC2', 's3': 'MOTO_S3', 'rds': 'MOTO_RDS'}


def wait_for_port(host, port, timeout=30):
    """Block until a TCP connect to host:port succeeds or raise after timeout."""
    deadline = time.time() + timeout
    while True:
        try:
            sock = socket.create_connection((host, int(port)), timeout=2)
            sock.close()
            return
        except (OSError, socket.error):
            if time.time() >= deadline:
                raise RuntimeError('{0}:{1} did not accept connections within {2}s'.format(
                    host, port, timeout))
            time.sleep(0.5)


def endpoint(svc):
    return os.environ.get(SERVICES[svc])


def wait_for_simulators(timeout=30):
    for svc in SERVICES:
        parsed = urlparse(endpoint(svc))
        wait_for_port(parsed.hostname, parsed.port, timeout)


def sim_config():
    """Build the ``aws`` config section pointing at the simulators."""
    return {
        'region': os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'),
        'vpc_id': '',
        'subnet_ids': [],
        'endpoints': dict((svc, endpoint(svc)) for svc in SERVICES if endpoint(svc)),
    }


def client(svc):
    """Create a boto3 client for ``svc`` aimed at its simulator."""
    kwargs = {'region_name': sim_config()['region'], 'endpoint_url': endpoint(svc)}
    if svc == 's3':
        kwargs['config'] = Config(signature_version='s3v4')
    return boto3.session.Session().client(svc, **kwargs)
