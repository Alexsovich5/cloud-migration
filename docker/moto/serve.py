"""Run moto_server with bucket ACL owners added to the S3 responses.

moto 0.4.14 answers GET /<bucket>?acl with the object listing, so a client
cannot read who owns a bucket. This wrapper adds two bucket requests:

- GET /<bucket>?acl returns an AccessControlPolicy whose Owner ID is the
  bucket's owner: by default the canonical ID that ListBuckets reports.
- PUT /<bucket>?acl stores the Owner ID from the request body, so a test can
  stage a bucket that belongs to another account.

Everything else is passed to moto unchanged.
"""

import re
import sys

from moto.s3 import responses
from moto.server import main

OWN_ID = re.search(r'<ID>([^<]+)</ID>', responses.S3_ALL_BUCKETS).group(1)

ACL_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<AccessControlPolicy xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Owner>
    <ID>{owner}</ID>
    <DisplayName>{owner}</DisplayName>
  </Owner>
  <AccessControlList>
    <Grant>
      <Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="CanonicalUser">
        <ID>{owner}</ID>
        <DisplayName>{owner}</DisplayName>
      </Grantee>
      <Permission>FULL_CONTROL</Permission>
    </Grant>
  </AccessControlList>
</AccessControlPolicy>"""

_bucket_get = responses.ResponseObject._bucket_response_get
_bucket_put = responses.ResponseObject._bucket_response_put


def _bucket_response_get(self, bucket_name, querystring, headers):
    if 'acl' in querystring:
        bucket = self.backend.get_bucket(bucket_name)
        owner = getattr(bucket, 'acl_owner_id', OWN_ID)
        return 200, headers, ACL_RESPONSE.format(owner=owner)
    return _bucket_get(self, bucket_name, querystring, headers)


def _bucket_response_put(self, body, region_name, bucket_name, querystring, headers):
    if 'acl' in querystring:
        bucket = self.backend.get_bucket(bucket_name)
        match = re.search(r'<Owner>.*?<ID>([^<]+)</ID>.*?</Owner>', body, re.DOTALL)
        if match:
            bucket.acl_owner_id = match.group(1)
        return 200, headers, ''
    return _bucket_put(self, body, region_name, bucket_name, querystring, headers)


responses.ResponseObject._bucket_response_get = _bucket_response_get
responses.ResponseObject._bucket_response_put = _bucket_response_put

if __name__ == '__main__':
    main(sys.argv[1:])
