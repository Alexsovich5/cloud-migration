import hashlib
import os
import uuid

import pytest

from data_migration import DataMigrator
from state import MigrationState
from tests.support import aws

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures('simulators')]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_DIR = os.path.join(ROOT, 'tests', 'fixtures', 'data', 'web-portal')


@pytest.fixture
def migrated(request, tmpdir):
    s3 = aws.client('s3')
    bucket = 'migration-artifacts-{0}'.format(uuid.uuid4().hex[:12])
    migrator = DataMigrator(s3, bucket)
    st = MigrationState(os.path.join(str(tmpdir), 'state.json'))

    def cleanup():
        migrator.delete_objects([item['key'] for item in st.ledger('web-portal')
                                 if item['kind'] == 's3_object'])
    request.addfinalizer(cleanup)

    manifest_path = migrator.migrate({'name': 'web-portal', 'data_path': FIXTURE_DIR}, st,
                                     manifest_dir=str(tmpdir))
    return s3, bucket, migrator, st, manifest_path


def test_fixture_directory_lands_in_s3_and_verifies(migrated):
    s3, bucket, migrator, st, manifest_path = migrated

    expected = 0
    for rel in ('db/portal_db.sql', 'uploads/logo.txt'):
        expected += os.path.getsize(os.path.join(FIXTURE_DIR, rel))
    data = st.workload('web-portal')['data']
    assert data['files'] == 2
    assert data['bytes'] == expected
    assert data['manifest'] == manifest_path

    listed = s3.list_objects(Bucket=bucket, Prefix='web-portal/')
    assert sorted(o['Key'] for o in listed['Contents']) == [
        'web-portal/db/portal_db.sql', 'web-portal/uploads/logo.txt']
    head = s3.head_object(Bucket=bucket, Key='web-portal/uploads/logo.txt')
    with open(os.path.join(FIXTURE_DIR, 'uploads', 'logo.txt'), 'rb') as handle:
        assert head['ETag'].strip('"') == hashlib.md5(handle.read()).hexdigest()

    assert migrator.verify(manifest_path) == (True, [])


def test_overwritten_object_fails_verify(migrated):
    s3, bucket, migrator, st, manifest_path = migrated

    s3.put_object(Bucket=bucket, Key='web-portal/db/portal_db.sql',
                  Body=b'-- tampered after migration\n')

    ok, problems = migrator.verify(manifest_path)

    assert ok is False
    assert len(problems) == 1
    assert 'web-portal/db/portal_db.sql' in problems[0]
