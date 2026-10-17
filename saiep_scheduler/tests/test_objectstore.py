import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from saiep_scheduler import objectstore


def test_sigv4_matches_aws_reference_vector():
    # "GET Object" example from the AWS SigV4 documentation (header auth).
    store = objectstore.ObjectStore('https://examplebucket.s3.amazonaws.com',
                                    'AKIAIOSFODNN7EXAMPLE',
                                    'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY')
    headers = store.sign('GET', '/test.txt', now=datetime(2013, 5, 24, tzinfo=timezone.utc),
                         extra_headers={'Range': 'bytes=0-9'})
    assert headers['authorization'].endswith(
        'Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41')


@pytest.fixture
def fake_s3():
    objects = {}

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            assert self.headers['Authorization'].startswith('AWS4-HMAC-SHA256 Credential=ak/')
            objects[self.path] = self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            if self.path not in objects:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(objects[self.path])

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield objectstore.ObjectStore(f'http://127.0.0.1:{server.server_port}', 'ak', 'sk'), objects
    server.shutdown()


def test_put_then_get_round_trip(fake_s3):
    store, objects = fake_s3
    store.put('saiep-records', 'decisions/2026/a b.json', b'{"x": 1}', 'application/json')
    assert '/saiep-records/decisions/2026/a%20b.json' in objects
    assert store.get('saiep-records', 'decisions/2026/a b.json') == b'{"x": 1}'


def test_missing_object_raises_not_found(fake_s3):
    store, _ = fake_s3
    with pytest.raises(objectstore.NotFound):
        store.get('saiep-artifacts', 'nope')


def test_unreachable_endpoint_raises_store_error():
    store = objectstore.ObjectStore('http://127.0.0.1:1', 'ak', 'sk', timeout=1)
    with pytest.raises(objectstore.ObjectStoreError):
        store.get('b', 'k')
