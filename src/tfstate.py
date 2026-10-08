"""
Terraform state outputs import.

Reads the root module outputs from a Terraform 0.6 state file (version 1)
and fills empty network settings in the migration config with them.
"""

import json

SUPPORTED_VERSION = 1
ROOT_PATH = ['root']


def read_outputs(path):
    """Return the root module outputs of a version 1 state file as {name: value}."""
    with open(path) as handle:
        data = json.load(handle)
    version = data.get('version')
    if version != SUPPORTED_VERSION:
        raise ValueError('{0}: unsupported state version {1}, expected {2}'.format(
            path, version, SUPPORTED_VERSION))
    for module in data.get('modules') or []:
        if module.get('path') == ROOT_PATH:
            outputs = module.get('outputs') or {}
            return dict((name, str(value)) for name, value in outputs.items())
    raise ValueError('{0}: no root module in state'.format(path))


def _split_ids(value):
    return [item.strip() for item in value.split(',') if item.strip()]


# config key under aws -> (output name, conversion)
MAPPING = (
    ('vpc_id', 'vpc_id', str),
    ('subnet_ids', 'private_subnet_ids', _split_ids),
    ('security_group_id', 'app_security_group_id', str),
    ('artifacts_bucket', 'artifacts_bucket', str),
)


def merge_outputs(cfg, outputs):
    """Fill empty aws settings in cfg from Terraform outputs and return cfg."""
    aws = cfg.setdefault('aws', {})
    for key, output, convert in MAPPING:
        if aws.get(key) or not outputs.get(output):
            continue
        aws[key] = convert(outputs[output])
    return cfg
