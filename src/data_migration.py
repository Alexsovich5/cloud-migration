"""Copy workload data files to S3 and prove each one arrived intact."""

import base64
import hashlib
import json
import os

from botocore.exceptions import ClientError

DEFAULT_BUCKET = 'migration-artifacts'
# A single PUT is limited to 5 GiB; multipart uploads are not supported.
MAX_OBJECT_SIZE = 5 * 1024 ** 3

VERIFIED = 'verified'
MISMATCH = 'mismatch'


class IntegrityError(Exception):
    """Raised when an uploaded object does not match its local file."""


def artifacts_bucket(aws_cfg):
    """Return the configured artifacts bucket, or the default name."""
    return aws_cfg.get('artifacts_bucket') or DEFAULT_BUCKET


def md5_file(path, chunk=1 << 20):
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _strip_etag(etag):
    return (etag or '').strip('"')


def _list_files(root):
    """Return paths relative to ``root`` for every file below it, sorted."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            found.append(os.path.relpath(full, root).replace(os.sep, '/'))
    return sorted(found)


class DataMigrator(object):
    """Upload a workload's ``data_path`` to ``bucket`` with MD5 checks."""

    def __init__(self, s3_client, bucket):
        self.s3 = s3_client
        self.bucket = bucket

    def ensure_bucket(self):
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.s3.create_bucket(Bucket=self.bucket)

    def migrate(self, workload, state, manifest_dir='state'):
        name = workload['name']
        data_path = workload.get('data_path')
        if data_path and not os.path.isdir(data_path):
            raise ValueError('{0}: data_path {1} is not a directory'.format(name, data_path))

        files = []
        for rel in (_list_files(data_path) if data_path else []):
            full = os.path.join(data_path, rel)
            size = os.path.getsize(full)
            if size > MAX_OBJECT_SIZE:
                raise ValueError('{0}: {1} is {2} bytes, above the 5 GiB single upload '
                                 'limit'.format(name, rel, size))
            files.append((rel, full, size))

        if files:
            self.ensure_bucket()

        entries = []
        mismatched = []
        for rel, full, size in files:
            key = '{0}/{1}'.format(name, rel)
            local_md5 = md5_file(full)
            content_md5 = base64.b64encode(bytes.fromhex(local_md5)).decode('ascii')
            with open(full, 'rb') as body:
                response = self.s3.put_object(Bucket=self.bucket, Key=key, Body=body,
                                              ContentMD5=content_md5,
                                              ServerSideEncryption='AES256')
            state.record(name, 's3_object', bucket=self.bucket, key=key)
            status = VERIFIED
            if _strip_etag(response.get('ETag')) != local_md5:
                status = MISMATCH
                mismatched.append(key)
            entries.append({'key': key, 'size': size, 'md5': local_md5, 'status': status})

        manifest_path = self._write_manifest(manifest_dir, name, entries)
        state.set(name, 'data', {'files': len(entries),
                                 'bytes': sum(e['size'] for e in entries),
                                 'manifest': manifest_path})
        if mismatched:
            raise IntegrityError('{0}: ETag does not match local MD5 for {1}'.format(
                name, ', '.join(mismatched)))
        return manifest_path

    def _write_manifest(self, manifest_dir, name, entries):
        if not os.path.isdir(manifest_dir):
            os.makedirs(manifest_dir)
        path = os.path.join(manifest_dir, '{0}.manifest.json'.format(name))
        manifest = {'workload': name, 'bucket': self.bucket, 'files': entries}
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w') as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
        return path

    def verify(self, manifest_path):
        """Re-check every manifest entry against S3 with ``head_object``."""
        with open(manifest_path) as handle:
            manifest = json.load(handle)
        bucket = manifest.get('bucket') or self.bucket
        problems = []
        for entry in manifest['files']:
            key = entry['key']
            try:
                head = self.s3.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                problems.append('{0}: missing ({1})'.format(
                    key, exc.response.get('Error', {}).get('Code')))
                continue
            reasons = []
            if head.get('ContentLength') != entry['size']:
                reasons.append('size {0} != {1}'.format(head.get('ContentLength'), entry['size']))
            etag = _strip_etag(head.get('ETag'))
            if etag != entry['md5']:
                reasons.append('etag {0} != md5 {1}'.format(etag, entry['md5']))
            if reasons:
                problems.append('{0}: {1}'.format(key, ', '.join(reasons)))
        return not problems, problems

    def delete_objects(self, keys):
        deleted = []
        for key in keys:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
            deleted.append(key)
        return deleted
