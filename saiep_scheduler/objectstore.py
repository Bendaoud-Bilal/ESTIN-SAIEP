"""Minimal S3 client (stdlib, AWS Signature V4) for the SAIEP MinIO store.

Only what the hub needs: GET an archived job log, PUT a decision record.
Blocking — call from an executor.

Configured by env:
  SAIEP_MINIO_ENDPOINT     e.g. http://10.96.12.34:9000 (no path)
  SAIEP_MINIO_ACCESS_KEY   saiep-hub MinIO user (read logs, write records)
  SAIEP_MINIO_SECRET_KEY
"""
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LOGS_BUCKET = 'saiep-artifacts'
RECORDS_BUCKET = 'saiep-records'
REGION = 'us-east-1'  # MinIO default


class ObjectStoreError(Exception):
    pass


class NotFound(ObjectStoreError):
    pass


def enabled():
    return all(os.environ.get(k) for k in
               ('SAIEP_MINIO_ENDPOINT', 'SAIEP_MINIO_ACCESS_KEY', 'SAIEP_MINIO_SECRET_KEY'))


def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


class ObjectStore:
    def __init__(self, endpoint=None, access_key=None, secret_key=None, region=REGION, timeout=5):
        self.endpoint = (endpoint or os.environ['SAIEP_MINIO_ENDPOINT']).rstrip('/')
        self.access_key = access_key or os.environ['SAIEP_MINIO_ACCESS_KEY']
        self.secret_key = secret_key or os.environ['SAIEP_MINIO_SECRET_KEY']
        self.region, self.timeout = region, timeout
        self.host = urllib.parse.urlsplit(self.endpoint).netloc

    def sign(self, method, path, payload=b'', now=None, extra_headers=None):
        """Return the headers for a SigV4-signed path-style request."""
        now = now or datetime.now(timezone.utc)
        amz_date, day = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
        payload_hash = hashlib.sha256(payload).hexdigest()
        headers = {'host': self.host, 'x-amz-content-sha256': payload_hash, 'x-amz-date': amz_date,
                   **{k.lower(): v for k, v in (extra_headers or {}).items()}}
        signed = ';'.join(sorted(headers))
        canonical = '\n'.join([
            method, path, '',
            ''.join(f'{k}:{headers[k]}\n' for k in sorted(headers)),
            signed, payload_hash,
        ])
        scope = f'{day}/{self.region}/s3/aws4_request'
        to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope,
                             hashlib.sha256(canonical.encode()).hexdigest()])
        key = _hmac(_hmac(_hmac(_hmac(f'AWS4{self.secret_key}'.encode(), day), self.region), 's3'),
                    'aws4_request')
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers['authorization'] = (f'AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, '
                                    f'SignedHeaders={signed}, Signature={signature}')
        return headers

    def _request(self, method, bucket, key, payload=b'', content_type=None):
        path = '/' + urllib.parse.quote(f'{bucket}/{key}', safe='/-_.~')
        headers = self.sign(method, path, payload)
        del headers['host']  # urllib sets it from the URL
        if content_type:
            headers['content-type'] = content_type
        req = urllib.request.Request(self.endpoint + path, data=payload if method == 'PUT' else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(f'{bucket}/{key}') from e
            raise ObjectStoreError(f'{method} {bucket}/{key}: HTTP {e.code}') from e
        except (urllib.error.URLError, OSError) as e:
            raise ObjectStoreError(f'{method} {bucket}/{key}: {e}') from e

    def get(self, bucket, key):
        return self._request('GET', bucket, key)

    def put(self, bucket, key, data, content_type='application/octet-stream'):
        self._request('PUT', bucket, key, data, content_type)
