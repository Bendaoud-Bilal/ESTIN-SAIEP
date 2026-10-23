from datetime import datetime, timezone
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from saiep_scheduler import k8s, objectstore

NS, WF = 'saiep-u-alice', 'train-abc'


def _node(pod, key, started, bucket='saiep-artifacts', name='main-logs'):
    return {'type': 'Pod', 'startedAt': started,
            'outputs': {'artifacts': [{'name': name, 's3': {'bucket': bucket, 'key': key}}]}}


def _workflow(*nodes):
    return {'status': {'nodes': {f'n{i}': n for i, n in enumerate(nodes)} | {
        'root': {'type': 'Retry'}}}}


def test_archived_logs_only_trusts_this_workflows_prefix():
    wf = _workflow(
        _node('p1', f'{NS}/{WF}/train-abc-main-1/main.log', '2026-01-01T00:00:01Z'),
        _node('x', f'saiep-u-bob/{WF}/train-abc-main-9/main.log', 't'),     # other tenant
        _node('x', f'{NS}/other-wf/p/main.log', 't'),                       # other workflow
        _node('x', f'{NS}/{WF}/../../saiep-u-bob/x/main.log', 't'),          # traversal
        _node('x', f'{NS}/{WF}/p/main.log', 't', bucket='saiep-records'),    # other bucket
        _node('x', f'{NS}/{WF}/p/out.tgz', 't', name='out'),                 # not a log
    )
    assert k8s.archived_logs(wf, NS, WF) == [
        ('2026-01-01T00:00:01Z', 'train-abc-main-1', f'{NS}/{WF}/train-abc-main-1/main.log')]


class FakeStore:
    def __init__(self, objects):
        self.objects, self.reads = objects, []

    def get(self, bucket, key):
        self.reads.append((bucket, key))
        if key not in self.objects:
            raise objectstore.NotFound(key)
        return self.objects[key]


def _cluster(live_pods, workflow):
    c = object.__new__(k8s.Cluster)
    pods = [SimpleNamespace(metadata=SimpleNamespace(
        name=p, creation_timestamp=datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)))
        for p in live_pods]
    c.core = SimpleNamespace(
        list_namespaced_pod=lambda ns, label_selector: SimpleNamespace(items=pods),
        read_namespaced_pod_log=lambda pod, ns, container, tail_lines: f'live {pod}',
    )
    c.custom = SimpleNamespace(get_namespaced_custom_object=lambda *a: workflow)
    return c


def test_logs_fall_back_to_archive_when_pod_is_gone():
    k1, k2 = f'{NS}/{WF}/pod-1/main.log', f'{NS}/{WF}/pod-2/main.log'
    wf = _workflow(_node('pod-1', k1, '2026-01-01T00:00:01Z'), _node('pod-2', k2, '2026-01-01T00:00:04Z'))
    store = FakeStore({k1: b'line1\nline2\nline3\n', k2: b'never read'})
    text = _cluster(['pod-2'], wf).logs(NS, WF, tail_lines=2, store=store)
    assert '=== pod-1 (archivé) ===\nline2\nline3' in text
    assert '=== pod-2 ===\nlive pod-2' in text
    assert text.index('pod-1') < text.index('pod-2')
    assert store.reads == [('saiep-artifacts', k1)]  # live pod not re-read from the archive


def test_missing_archive_is_reported_not_raised():
    k1 = f'{NS}/{WF}/pod-1/main.log'
    text = _cluster([], _workflow(_node('pod-1', k1, 't'))).logs(NS, WF, store=FakeStore({}))
    assert 'journal archivé indisponible' in text


def test_logs_explains_container_starting_instead_of_bad_request():
    c = _cluster(['pod-1'], {})

    def starting(*args, **kwargs):
        raise ApiException(status=400, reason='Bad Request')

    c.core.read_namespaced_pod_log = starting
    text = c.logs(NS, WF)
    assert 'conteneur démarre' in text
    assert 'Bad Request' not in text


def test_logs_without_store_configured_use_live_pods_only(monkeypatch):
    for k in ('SAIEP_MINIO_ENDPOINT', 'SAIEP_MINIO_ACCESS_KEY', 'SAIEP_MINIO_SECRET_KEY'):
        monkeypatch.delenv(k, raising=False)
    assert _cluster(['pod-1'], {}).logs(NS, WF) == '=== pod-1 ===\nlive pod-1'
