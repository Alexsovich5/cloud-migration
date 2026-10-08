import os
import subprocess
import sys

import pkg_resources

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_pins(filename):
    pins = []
    with open(os.path.join(REPO_ROOT, filename)) as handle:
        for line in handle:
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            name, version = line.split('==')
            pins.append((name.strip(), version.strip()))
    return pins


def test_python_is_3_4():
    assert sys.version_info[:2] == (3, 4)


def test_requirement_files_are_not_empty():
    assert read_pins('requirements.txt')
    assert read_pins('requirements-dev.txt')


def test_installed_versions_match_pins():
    mismatches = []
    for filename in ('requirements.txt', 'requirements-dev.txt'):
        for name, version in read_pins(filename):
            installed = pkg_resources.get_distribution(name).version
            if installed != version:
                mismatches.append('{0}: pinned {1}, installed {2}'.format(
                    name, version, installed))
    assert mismatches == []


def test_terraform_version():
    output = subprocess.check_output(['terraform', 'version']).decode('utf-8')
    assert 'Terraform v0.6.3' in output
