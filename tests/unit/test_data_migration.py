import base64
import hashlib
import json
import os
from unittest import mock

import pytest
from botocore.exceptions import ClientError

import data_migration
from state import MigrationState

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_DIR = os.path.join(ROOT, 'tests', 'fixtures', 'data', 'web-portal')


class FakeS3(object):
    """In-memory S3 stand-in; ``etag_override`` maps keys to a forced ETag."""

    def __init__(self, etag_override=None, buckets=()):
        self.objects = {}
        self.buckets = set(buckets)
        self.etag_override = etag_override or {}
        self.put_calls = []

    def head_bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise ClientError({'Error': {'Code': '404', 'Message': 'Not Found'}}, 'HeadBucket')
        return {}

    def create_bucket(self, Bucket):
        self.buckets.add(Bucket)
        return {}

    def put_object(self, **kwargs):
        body = kwargs['Body'].read()
        self.put_calls.append(dict(kwargs, Body=body))
        etag = self.etag_override.get(kwargs['Key'], hashlib.md5(body).hexdigest())
        self.objects[(kwargs['Bucket'], kwargs['Key'])] = (body, etag)
        return {'ETag': '"{0}"'.format(etag)}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({'Error': {'Code': '404', 'Message': 'Not Found'}}, 'HeadObject')
        body, etag = self.objects[(Bucket, Key)]
        return {'ContentLength': len(body), 'ETag': '"{0}"'.format(etag)}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        return {}


def workload(path=FIXTURE_DIR):
    return {'name': 'web-portal', 'data_path': path}


@pytest.fixture
def st(tmpdir):
    return MigrationState(os.path.join(str(tmpdir), 'state.json'))


def fixture_files():
    return ['db/portal_db.sql', 'uploads/logo.txt']


def test_md5_file_matches_hashlib(tmpdir):
    path = tmpdir.join('sample.bin')
    payload = b'cloud migration payload\n' * 1000
    path.write(payload, mode='wb')

    assert data_migration.md5_file(str(path), chunk=7) == hashlib.md5(payload).hexdigest()


def test_keys_are_prefixed_and_sorted(st, tmpdir):
    s3 = FakeS3()
    migrator = data_migration.DataMigrator(s3, 'artifacts')

    migrator.migrate(workload(), st, manifest_dir=str(tmpdir))

    keys = [call['Key'] for call in s3.put_calls]
    assert keys == ['web-portal/db/portal_db.sql', 'web-portal/uploads/logo.txt']


def test_put_object_sends_content_md5_and_sse(st, tmpdir):
    s3 = FakeS3()
    migrator = data_migration.DataMigrator(s3, 'artifacts')

    migrator.migrate(workload(), st, manifest_dir=str(tmpdir))

    call = s3.put_calls[0]
    local = os.path.join(FIXTURE_DIR, 'db', 'portal_db.sql')
    with open(local, 'rb') as handle:
        digest = hashlib.md5(handle.read()).digest()
    assert call['Bucket'] == 'artifacts'
    assert call['ContentMD5'] == base64.b64encode(digest).decode('ascii')
    assert call['ServerSideEncryption'] == 'AES256'


def test_migrate_writes_manifest_ledger_and_state(st, tmpdir):
    s3 = FakeS3()
    migrator = data_migration.DataMigrator(s3, 'artifacts')

    manifest_path = migrator.migrate(workload(), st, manifest_dir=str(tmpdir))

    assert manifest_path == os.path.join(str(tmpdir), 'web-portal.manifest.json')
    with open(manifest_path) as handle:
        manifest = json.load(handle)
    assert manifest['workload'] == 'web-portal'
    assert manifest['bucket'] == 'artifacts'
    sizes = [os.path.getsize(os.path.join(FIXTURE_DIR, rel)) for rel in fixture_files()]
    assert [f['key'] for f in manifest['files']] == [
        'web-portal/' + rel for rel in fixture_files()]
    assert [f['size'] for f in manifest['files']] == sizes
    assert all(f['status'] == 'verified' for f in manifest['files'])
    assert manifest['files'][1]['md5'] == data_migration.md5_file(
        os.path.join(FIXTURE_DIR, 'uploads', 'logo.txt'))

    ledger = st.ledger('web-portal')
    assert ledger == [{'kind': 's3_object', 'bucket': 'artifacts', 'key': 'web-portal/' + rel}
                      for rel in fixture_files()]
    assert st.workload('web-portal')['data'] == {
        'files': 2, 'bytes': sum(sizes), 'manifest': manifest_path}


def test_wrong_etag_raises_and_marks_mismatch(st, tmpdir):
    s3 = FakeS3(etag_override={'web-portal/uploads/logo.txt': '0' * 32})
    migrator = data_migration.DataMigrator(s3, 'artifacts')

    with pytest.raises(data_migration.IntegrityError) as excinfo:
        migrator.migrate(workload(), st, manifest_dir=str(tmpdir))

    assert 'web-portal/uploads/logo.txt' in str(excinfo.value)
    with open(os.path.join(str(tmpdir), 'web-portal.manifest.json')) as handle:
        manifest = json.load(handle)
    statuses = dict((f['key'], f['status']) for f in manifest['files'])
    assert statuses == {'web-portal/db/portal_db.sql': 'verified',
                        'web-portal/uploads/logo.txt': 'mismatch'}
    # The uploaded objects stay in the ledger so rollback can remove them.
    assert len(st.ledger('web-portal')) == 2


def test_files_over_5_gib_are_rejected_before_upload(st, tmpdir):
    s3 = FakeS3()
    migrator = data_migration.DataMigrator(s3, 'artifacts')
    real_getsize = os.path.getsize

    def fake_getsize(path):
        if path.endswith('logo.txt'):
            return 5 * 1024 ** 3 + 1
        return real_getsize(path)

    with mock.patch('os.path.getsize', side_effect=fake_getsize):
        with pytest.raises(ValueError) as excinfo:
            migrator.migrate(workload(), st, manifest_dir=str(tmpdir))

    assert 'logo.txt' in str(excinfo.value)
    assert s3.put_calls == []


def test_missing_data_path_directory_raises(st, tmpdir):
    migrator = data_migration.DataMigrator(FakeS3(), 'artifacts')

    with pytest.raises(ValueError):
        migrator.migrate(workload(os.path.join(str(tmpdir), 'absent')), st,
                         manifest_dir=str(tmpdir))


def test_verify_reports_size_and_etag_problems(st, tmpdir):
    s3 = FakeS3()
    migrator = data_migration.DataMigrator(s3, 'artifacts')
    manifest_path = migrator.migrate(workload(), st, manifest_dir=str(tmpdir))
    assert migrator.verify(manifest_path) == (True, [])

    s3.objects[('artifacts', 'web-portal/uploads/logo.txt')] = (b'changed', 'f' * 32)
    del s3.objects[('artifacts', 'web-portal/db/portal_db.sql')]

    ok, problems = migrator.verify(manifest_path)

    assert ok is False
    assert len(problems) == 2
    assert any('web-portal/db/portal_db.sql' in p and 'missing' in p for p in problems)
    assert any('web-portal/uploads/logo.txt' in p for p in problems)


def test_ensure_bucket_creates_only_when_absent():
    s3 = FakeS3(buckets=['existing'])
    s3.create_bucket = mock.MagicMock()

    data_migration.DataMigrator(s3, 'existing').ensure_bucket()
    assert not s3.create_bucket.called

    data_migration.DataMigrator(s3, 'fresh').ensure_bucket()
    s3.create_bucket.assert_called_once_with(Bucket='fresh')


def test_artifacts_bucket_defaults():
    assert data_migration.artifacts_bucket({}) == 'migration-artifacts'
    assert data_migration.artifacts_bucket({'artifacts_bucket': ''}) == 'migration-artifacts'
    assert data_migration.artifacts_bucket({'artifacts_bucket': 'mine'}) == 'mine'


def test_delete_objects_removes_each_key():
    s3 = FakeS3()
    s3.objects[('artifacts', 'a')] = (b'1', 'x')
    s3.objects[('artifacts', 'b')] = (b'2', 'y')
    migrator = data_migration.DataMigrator(s3, 'artifacts')

    assert migrator.delete_objects(['a', 'b']) == ['a', 'b']
    assert s3.objects == {}
