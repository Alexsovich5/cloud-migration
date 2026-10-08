import json
import os
import re
import subprocess

import hcl
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
TF_DIR = os.path.join(ROOT, 'terraform')
TF_FILES = ['main.tf', 'variables.tf', 'outputs.tf']

EXPECTED_TYPES = set([
    'aws_vpc', 'aws_subnet', 'aws_internet_gateway', 'aws_route_table',
    'aws_route_table_association', 'aws_security_group',
    'aws_db_subnet_group', 'aws_s3_bucket',
])

EXPECTED_OUTPUTS = set([
    'vpc_id', 'public_subnet_ids', 'private_subnet_ids',
    'app_security_group_id', 'db_subnet_group_name', 'artifacts_bucket',
])


def _read(name):
    with open(os.path.join(TF_DIR, name)) as handle:
        return handle.read()


def _load(name):
    return hcl.loads(_read(name))


@pytest.fixture(scope='module')
def main():
    return _load('main.tf')


def _block_body(text, start):
    """Return the text between the brace at or after ``start`` and its match."""
    open_at = text.index('{', start)
    depth = 0
    for pos in range(open_at, len(text)):
        if text[pos] == '{':
            depth += 1
        elif text[pos] == '}':
            depth -= 1
            if depth == 0:
                return text[open_at + 1:pos]
    raise ValueError('unbalanced braces')


def _resource_body(text, rtype, name):
    """Return the raw body of ``resource "rtype" "name" { ... }``."""
    header = re.search(
        r'resource\s+"{0}"\s+"{1}"'.format(re.escape(rtype), re.escape(name)),
        text)
    assert header, '{0}.{1} not found'.format(rtype, name)
    return _block_body(text, header.end())


def _ingress_bodies(text):
    group = _resource_body(text, 'aws_security_group', 'app')
    return [_block_body(group, m.end())
            for m in re.finditer(r'\bingress\s*(?=\{)', group)]


def test_all_files_parse():
    for name in TF_FILES:
        assert isinstance(_load(name), dict)


def test_resource_types_are_exactly_the_landing_zone(main):
    assert set(main['resource'].keys()) == EXPECTED_TYPES


def test_bucket_policy_denies_unencrypted_puts(main):
    bucket = main['resource']['aws_s3_bucket']['artifacts']
    assert bucket['acl'] == 'private'
    policy = json.loads(bucket['policy'])
    denies = [s for s in policy['Statement'] if s['Effect'] == 'Deny']
    assert denies
    matching = [
        s for s in denies
        if s['Action'] in ('s3:PutObject', ['s3:PutObject'])
        and s['Condition']['StringNotEquals'][
            's3:x-amz-server-side-encryption'] == 'AES256'
    ]
    assert len(matching) == 1


def test_bucket_has_no_post_period_blocks(main):
    bucket = main['resource']['aws_s3_bucket']['artifacts']
    assert 'versioning' not in bucket
    assert 'server_side_encryption_configuration' not in bucket


def test_security_group_uses_name_not_prefix(main):
    group = main['resource']['aws_security_group']['app']
    assert group['name'] == 'migration-app'
    assert 'name_prefix' not in group


def test_db_subnet_group_has_description_and_no_tags(main):
    groups = main['resource']['aws_db_subnet_group']
    assert len(groups) == 1
    group = list(groups.values())[0]
    assert group['description'].strip()
    assert 'tags' not in group
    assert group['subnet_ids'] == ['${aws_subnet.private.*.id}']


def test_subnets_use_split_variables():
    # pyhcl 0.1.11 keeps only the last resource of a repeated type, so each
    # subnet body is extracted from the raw text and parsed on its own.
    text = _read('main.tf')
    for kind in ('public', 'private'):
        subnet = hcl.loads(_resource_body(text, 'aws_subnet', kind))
        assert subnet['count'] == 2
        assert 'var.{0}_subnet_cidrs'.format(kind) in subnet['cidr_block']
        assert 'var.availability_zones' in subnet['availability_zone']


def test_public_route_table_routes_to_internet_gateway(main):
    table = main['resource']['aws_route_table']['public']
    assert table['route']['cidr_block'] == '0.0.0.0/0'
    assert table['route']['gateway_id'] == '${aws_internet_gateway.main.id}'
    assoc = main['resource']['aws_route_table_association']['public']
    assert assoc['count'] == 2


def test_security_group_has_three_ingress_rules():
    bodies = _ingress_bodies(_read('main.tf'))
    assert len(bodies) == 3
    rules = {}
    for body in bodies:
        rule = hcl.loads(body)
        assert rule['from_port'] == rule['to_port']
        rules[rule['from_port']] = rule['cidr_blocks']
    assert rules == {
        80: ['10.0.0.0/8'],
        443: ['10.0.0.0/8'],
        22: ['${var.admin_cidr}'],
    }


def test_block_scanner_sees_repeated_blocks():
    text = ('resource "aws_security_group" "app" {\n'
            '  ingress { from_port = 1 }\n'
            '  egress { from_port = 9 }\n'
            '  ingress { from_port = 2 }\n'
            '}\n')
    bodies = _ingress_bodies(text)
    assert [hcl.loads(b)['from_port'] for b in bodies] == [1, 2]


@pytest.mark.parametrize('name', TF_FILES)
def test_no_post_period_syntax(name):
    text = _read(name)
    for token in ('[*]', 'cidrsubnet', 'nat_gateway', 'tags = {'):
        assert token not in text, '{0} contains {1}'.format(name, token)


def test_variables_include_list_strings():
    variables = _load('variables.tf')['variable']
    assert set(variables.keys()) == set([
        'aws_region', 'vpc_cidr', 'admin_cidr', 'project_name',
        'public_subnet_cidrs', 'private_subnet_cidrs', 'availability_zones',
    ])
    assert variables['public_subnet_cidrs']['default'] == \
        '10.100.0.0/24,10.100.1.0/24'
    assert variables['private_subnet_cidrs']['default'] == \
        '10.100.10.0/24,10.100.11.0/24'
    assert variables['availability_zones']['default'] == \
        'us-east-1a,us-east-1b'


def test_outputs_file_defines_all_outputs():
    outputs = _load('outputs.tf')['output']
    assert set(outputs.keys()) == EXPECTED_OUTPUTS
    assert outputs['private_subnet_ids']['value'] == \
        '${join(",", aws_subnet.private.*.id)}'


def test_main_does_not_define_outputs(main):
    assert 'output' not in main


def test_terraform_graph_parses_config():
    subprocess.check_call(['terraform', 'graph', '.'], cwd=TF_DIR,
                          stdout=subprocess.DEVNULL)
