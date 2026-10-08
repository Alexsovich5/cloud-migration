#!/usr/bin/env python3
"""
Stand-in for the docker CLI.

Each invocation appends its arguments as one JSON line to the file named by
$FAKE_DOCKER_LOG. ``inspect`` prints a canned image description. Any argument
equal to FAIL makes the command exit 1 with a message on stderr.
"""

import json
import os
import sys

INSPECT_OUTPUT = [{
    'Id': 'sha256:0f1e2d3c4b5a',
    'Size': 126353408,
    'Os': 'linux',
    'Architecture': 'amd64',
    'RootFS': {'Type': 'layers', 'Layers': ['l1', 'l2', 'l3', 'l4', 'l5']},
}]


def main(argv):
    log_path = os.environ.get('FAKE_DOCKER_LOG')
    if log_path:
        with open(log_path, 'a') as log:
            log.write(json.dumps(argv) + '\n')
    if 'FAIL' in argv:
        sys.stderr.write('fake docker: forced failure\n')
        return 1
    if argv and argv[0] == 'inspect':
        sys.stdout.write(json.dumps(INSPECT_OUTPUT))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
