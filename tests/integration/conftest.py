import os

import pytest

from tests.support import aws


@pytest.fixture(scope='session')
def simulators():
    """Skip unless the moto simulators are reachable (run via ``make test``)."""
    if not os.environ.get('MOTO_EC2'):
        pytest.skip('MOTO_EC2 is not set: moto simulators only run under `make test`')
    aws.wait_for_simulators()
