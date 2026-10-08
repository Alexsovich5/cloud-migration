import hashlib
import os
import uuid

import pytest

from data_migration import BucketAccessError, DataMigrator
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


FOREIGN_OWNER = '0123456789abcdef0123456789abcdef'


def unique_bucket(prefix):
    return '{0}-{1}'.format(prefix, uuid.uuid4().hex[:12])


def bucket_names(s3):
    return [b['Name'] for b in s3.list_buckets()['Buckets']]


def test_missing_bucket_is_created_owned_by_us_and_used(tmpdir):
    s3 = aws.client('s3')
    bucket = unique_bucket('fresh-artifacts')
    assert bucket not in bucket_names(s3)
    st = MigrationState(os.path.join(str(tmpdir), 'state.json'))

    DataMigrator(s3, bucket).migrate({'name': 'web-portal', 'data_path': FIXTURE_DIR}, st,
                                     manifest_dir=str(tmpdir))

    assert bucket in bucket_names(s3)
    assert (s3.get_bucket_acl(Bucket=bucket)['Owner']['ID'] ==
            s3.list_buckets()['Owner']['ID'])
    listed = s3.list_objects(Bucket=bucket, Prefix='web-portal/')
    assert len(listed['Contents']) == 2


def test_bucket_owned_by_another_account_receives_nothing(tmpdir):
    s3 = aws.client('s3')
    bucket = unique_bucket('squatted-artifacts')
    s3.create_bucket(Bucket=bucket)
    # The simulator lets a test hand the bucket to another canonical owner.
    s3.put_bucket_acl(Bucket=bucket, AccessControlPolicy={
        'Owner': {'ID': FOREIGN_OWNER, 'DisplayName': 'other'}, 'Grants': []})
    assert s3.get_bucket_acl(Bucket=bucket)['Owner']['ID'] == FOREIGN_OWNER
    st = MigrationState(os.path.join(str(tmpdir), 'state.json'))

    with pytest.raises(BucketAccessError) as err:
        DataMigrator(s3, bucket).migrate({'name': 'web-portal', 'data_path': FIXTURE_DIR},
                                         st, manifest_dir=str(tmpdir))

    assert FOREIGN_OWNER in str(err.value)
    assert 'Contents' not in s3.list_objects(Bucket=bucket)
    assert st.ledger('web-portal') == []
