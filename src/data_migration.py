"""Copy workload data files to S3 and prove each one arrived intact.

Bucket names are global, so before anything is uploaded the artifacts bucket
must be one this account owns: its ACL owner is compared with the canonical
ID that ListBuckets reports for the caller. Only a "not found" answer from
HeadBucket leads to CreateBucket; any other error stops the upload.
"""

import base64
import errno
import hashlib
import json
import os
import stat

from botocore.exceptions import ClientError

from config import ConfigError

# HeadBucket carries no body, so a missing bucket shows up as the HTTP status.
NOT_FOUND_CODES = ('404', 'NoSuchBucket', 'NotFound')
# A single PUT is limited to 5 GiB; multipart uploads are not supported.
MAX_OBJECT_SIZE = 5 * 1024 ** 3

VERIFIED = 'verified'
MISMATCH = 'mismatch'


class IntegrityError(Exception):
    """Raised when an uploaded object does not match its local file."""


class BucketAccessError(Exception):
    """Raised when the artifacts bucket cannot be confirmed as our own."""


def artifacts_bucket(aws_cfg):
    """Return the configured artifacts bucket; there is no default name."""
    bucket = aws_cfg.get('artifacts_bucket')
    if not bucket:
        raise ConfigError('aws.artifacts_bucket: required (an S3 bucket this account owns; '
                          '--tfstate fills it from the Terraform output)')
    return bucket


def _error_code(exc):
    return str(exc.response.get('Error', {}).get('Code', ''))


def _open_regular(path):
    """Open ``path`` read-only without following a final symlink.

    Returns ``(file object, size)`` and raises ValueError when the path is a
    symlink or is not a regular file, so a file swapped for a link after it
    was listed is never read.
    """
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError('{0}: is a symbolic link'.format(path))
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('{0}: not a regular file'.format(path))
        return os.fdopen(fd, 'rb'), info.st_size
    except BaseException:
        os.close(fd)
        raise


def _md5_handle(handle, chunk=1 << 20):
    digest = hashlib.md5()
    while True:
        block = handle.read(chunk)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def md5_file(path, chunk=1 << 20):
    handle, _ = _open_regular(path)
    with handle:
        return _md5_handle(handle, chunk)


def _strip_etag(etag):
    return (etag or '').strip('"')


def _list_files(root):
    """Return paths relative to ``root`` for every regular file below it, sorted.

    Symlinks are never followed. One that resolves outside ``root`` raises
    ValueError, so only data that lives under ``data_path`` is uploaded; one
    that stays inside is skipped, since its target is listed on its own.
    """
    real_root = os.path.realpath(root)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in filenames + [d for d in dirnames
                                     if os.path.islink(os.path.join(dirpath, d))]:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root).replace(os.sep, '/')
            if os.path.islink(full):
                target = os.path.realpath(full)
                if target != real_root and not target.startswith(real_root + os.sep):
                    raise ValueError('{0}: {1} resolves outside data_path'.format(root, rel))
                continue
            found.append(rel)
    return sorted(found)


class DataMigrator(object):
    """Upload a workload's ``data_path`` to ``bucket`` with MD5 checks."""

    def __init__(self, s3_client, bucket):
        self.s3 = s3_client
        self.bucket = bucket

    def ensure_bucket(self):
        """Create the bucket only if it does not exist, then verify we own it."""
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            code = _error_code(exc)
            if code not in NOT_FOUND_CODES:
                raise BucketAccessError('{0}: head_bucket failed ({1}); not creating or '
                                        'uploading'.format(self.bucket, code))
            self.s3.create_bucket(Bucket=self.bucket)
        self.verify_owner()

    def verify_owner(self):
        """Raise BucketAccessError unless the bucket's owner is this account."""
        try:
            own = self.s3.list_buckets().get('Owner', {}).get('ID')
            owner = self.s3.get_bucket_acl(Bucket=self.bucket).get('Owner', {}).get('ID')
        except ClientError as exc:
            raise BucketAccessError('{0}: cannot read the bucket owner ({1})'.format(
                self.bucket, _error_code(exc)))
        if not own or not owner:
            raise BucketAccessError('{0}: bucket owner could not be determined'.format(
                self.bucket))
        if owner != own:
            raise BucketAccessError('{0}: owned by {1}, not by this account ({2})'.format(
                self.bucket, owner, own))

    def migrate(self, workload, state, manifest_dir='state'):
        name = workload['name']
        data_path = workload.get('data_path')
        if data_path and not os.path.isdir(data_path):
            raise ValueError('{0}: data_path {1} is not a directory'.format(name, data_path))

        rels = _list_files(data_path) if data_path else []
        for rel in rels:
            size = os.path.getsize(os.path.join(data_path, rel))
            if size > MAX_OBJECT_SIZE:
                raise ValueError('{0}: {1} is {2} bytes, above the 5 GiB single upload '
                                 'limit'.format(name, rel, size))
        if rels:
            self.ensure_bucket()

        entries = []
        mismatched = []
        for rel in rels:
            key = '{0}/{1}'.format(name, rel)
            body, size = _open_regular(os.path.join(data_path, rel))
            with body:
                if size > MAX_OBJECT_SIZE:
                    raise ValueError('{0}: {1} is {2} bytes, above the 5 GiB single upload '
                                     'limit'.format(name, rel, size))
                local_md5 = _md5_handle(body)
                body.seek(0)
                content_md5 = base64.b64encode(bytes.fromhex(local_md5)).decode('ascii')
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
        bucket = manifest.get('bucket')
        if bucket != self.bucket:
            raise BucketAccessError('{0}: manifest names bucket {1!r}, not the configured '
                                    'bucket {2!r}'.format(manifest_path, bucket, self.bucket))
        self.verify_owner()
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
