import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from saiep_scheduler import advisor, records, tenants, workflows


# --- tenants -----------------------------------------------------------------
@pytest.mark.parametrize('user', ['alice', 'bob-2'])
def test_clean_username_maps_directly(user):
    assert tenants.namespace_for(user) == f'saiep-u-{user}'


def test_unclean_usernames_get_distinct_hashed_namespaces():
    a, b = tenants.namespace_for('Alice.X'), tenants.namespace_for('alice-x')
    assert a != b
    assert a.startswith('saiep-u-alice-x-')
    assert len(tenants.namespace_for('x' * 200)) <= 63


def test_tenant_uid_is_stable_and_in_range():
    uid = tenants.tenant_uid('saiep-u-alice')
    assert uid == tenants.tenant_uid('saiep-u-alice')
    assert tenants.UID_BASE <= uid < tenants.UID_BASE + tenants.UID_SPAN


EXECUTOR = {'executor_access_key': 'argo-executor', 'executor_secret_key': 's3cr-t_x'}


def test_bundle_contains_full_policy_set():
    bundle = tenants.build_tenant_bundle('alice', tier='phd', api_server_cidrs=['172.18.0.2/32'],
                                         api_server_port=6443, **EXECUTOR)
    kinds = [o['kind'] for o in bundle]
    for kind in ('Namespace', 'ResourceQuota', 'LimitRange', 'ServiceAccount',
                 'RoleBinding', 'NetworkPolicy'):
        assert kind in kinds
    assert kinds[0] == 'Namespace'  # namespace must be applied first
    ns = bundle[0]
    assert ns['metadata']['labels']['saiep.estin.dz/tenant'] == 'true'
    assert tenants.PROVISIONED_LABEL not in ns['metadata']['labels']  # set last, by k8s.ensure_tenant
    quota = next(o for o in bundle if o['kind'] == 'ResourceQuota')
    assert quota['spec']['hard']['limits.cpu'] == '4'
    assert all(o['metadata'].get('namespace', 'saiep-u-alice') == 'saiep-u-alice' for o in bundle)
    api = next(o for o in bundle if o['metadata']['name'] == 'saiep-allow-dns-and-proxy')
    assert api['spec']['egress'][-1]['to'][0]['ipBlock']['cidr'] == '172.18.0.2/32'


def test_bundle_carries_log_archive_credential_and_minio_egress():
    bundle = tenants.build_tenant_bundle('alice', **EXECUTOR)
    secret = next(o for o in bundle if o['kind'] == 'Secret')
    assert secret['metadata'] == {'name': 'saiep-artifacts', 'namespace': 'saiep-u-alice'}
    assert secret['stringData'] == {'accessKey': 'argo-executor', 'secretKey': 's3cr-t_x'}
    egress = next(o for o in bundle if o['metadata']['name'] == 'saiep-allow-dns-and-proxy')
    minio = [r for r in egress['spec']['egress']
             if r['to'][0].get('podSelector', {}).get('matchLabels') == {'app': 'saiep-minio'}]
    assert minio and minio[0]['ports'] == [{'protocol': 'TCP', 'port': 9000}]
    assert minio[0]['to'][0]['namespaceSelector']['matchLabels'] == {
        'kubernetes.io/metadata.name': 'saiep-storage'}


def test_bundle_requires_executor_credentials():
    with pytest.raises(ValueError, match='SAIEP_MINIO_EXECUTOR'):
        tenants.build_tenant_bundle('alice')


@pytest.mark.parametrize('q,expected', [('500m', 0.5), ('2', 2), ('0.5', 0.5), ('4Gi', 4 * 2**30),
                                        ('512Mi', 512 * 2**20)])
def test_quantity(q, expected):
    assert tenants.quantity(q) == expected


def test_check_fits_against_tier_limits():
    assert tenants.check_fits('1', '2Gi', 0, tier='external') is None
    assert 'vCPU' in tenants.check_fits('4', '1Gi', 0, tier='external')
    assert 'mémoire' in tenants.check_fits('1', '4Gi', 0, tier='external')
    assert 'GPU' in tenants.check_fits('1', '1Gi', 1, tier='external')
    assert tenants.check_fits('2', '4Gi', 1, tier='phd') is None


def test_unknown_tier_rejected():
    with pytest.raises(ValueError):
        tenants.build_tenant_bundle('alice', tier='vip', **EXECUTOR)


# --- workflows ---------------------------------------------------------------
def _form(**kw):
    base = {'name': 'train', 'image': 'python:3.12', 'command': 'python -c "print(1)"'}
    base.update(kw)
    return base


def test_valid_request_builds_cpu_workflow():
    req = workflows.JobRequest.from_form(_form(tier='background'))
    wf = workflows.build_workflow('saiep-u-alice', 'alice', req)
    assert wf['spec']['workflowTemplateRef'] == {'name': 'saiep-cpu-job', 'clusterScope': True}
    assert wf['spec']['podPriorityClassName'] == 'saiep-background'
    assert wf['metadata']['namespace'] == 'saiep-u-alice'
    params = {p['name']: p['value'] for p in wf['spec']['arguments']['parameters']}
    assert params['command'] == 'python -c "print(1)"'
    assert 'gpu' not in params


def test_gpu_request_uses_gpu_template():
    req = workflows.JobRequest.from_form(_form(gpu=1))
    wf = workflows.build_workflow('saiep-u-alice', 'alice', req)
    assert wf['spec']['workflowTemplateRef']['name'] == 'saiep-gpu-job'


@pytest.mark.parametrize('bad', [
    {'name': 'Bad_Name'}, {'name': ''}, {'image': 'evil image'}, {'command': ''},
    {'cpu': '64'}, {'memory': '1Ti'}, {'gpu': 8}, {'tier': 'interactive'},
    {'isolation': 'none'}, {'timeout_seconds': 10}, {'retries': 99}, {'gpu': 'x'},
])
def test_invalid_requests_rejected(bad):
    with pytest.raises(ValueError):
        workflows.JobRequest.from_form(_form(**bad))


# --- advisor -----------------------------------------------------------------
def test_advisor_noop_without_url():
    assert advisor.recommend({'cpu': '1'}, url='') is None


def _serve(handler_cls):
    server = HTTPServer(('127.0.0.1', 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_port}'


def test_advisor_returns_recommendation():
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = json.dumps({'cpu': '2'}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server, url = _serve(H)
    try:
        assert advisor.recommend({'cpu': '1'}, url=url) == {'cpu': '2'}
    finally:
        server.shutdown()


def test_advisor_times_out_to_none():
    import time

    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(1.0)

        def log_message(self, *a):
            pass

    server, url = _serve(Slow)
    try:
        start = time.monotonic()
        assert advisor.recommend({}, url=url, timeout=0.2) is None
        assert time.monotonic() - start < 0.9
    finally:
        server.shutdown()


# --- records -----------------------------------------------------------------
def _wf(phase):
    return {
        'metadata': {'namespace': 'saiep-u-alice', 'name': 'train-abc'},
        'spec': {'arguments': {'parameters': [{'name': 'cpu', 'value': '1'},
                                              {'name': 'image', 'value': 'x'}]}},
        'status': {'phase': phase, 'resourcesDuration': {'cpu': 12, 'memory': 30}},
    }


def test_outcome_recorded_once_and_survives_restart(tmp_path):
    path = tmp_path / 'decisions.jsonl'
    log = records.DecisionLog(str(path))
    log.intent(user='alice', namespace='saiep-u-alice', workflow='train-abc',
               request={'cpu': '1'}, decision='accepted')
    assert log.outcome(_wf('Running')) is None
    rec = log.outcome(_wf('Succeeded'))
    assert rec['requested'] == {'cpu': '1'}
    assert rec['resources_duration'] == {'cpu': 12, 'memory': 30}
    assert log.outcome(_wf('Succeeded')) is None
    assert records.DecisionLog(str(path)).outcome(_wf('Succeeded')) is None
    stages = [json.loads(line)['stage'] for line in path.read_text().splitlines()]
    assert stages == ['intent', 'outcome']


class _Store:
    def __init__(self, fail=0):
        self.fail, self.puts = fail, []

    def put(self, bucket, key, data, content_type):
        if self.fail:
            self.fail -= 1
            raise records.objectstore.ObjectStoreError('down')
        self.puts.append((bucket, key, json.loads(data), content_type))


def test_records_ship_to_minio_one_object_each_and_stay_local(tmp_path):
    store = _Store(fail=1)  # first upload attempt fails, retry succeeds
    shipper = records.RecordShipper(store, backoff=0)
    log = records.DecisionLog(str(tmp_path / 'd.jsonl'), shipper=shipper)
    a = log.intent(user='alice', namespace='saiep-u-alice', workflow='w1', request={},
                   decision='accepted')
    b = log.outcome(_wf('Succeeded'))
    shipper.flush()
    assert [p[2] for p in store.puts] == [a, b]
    assert {p[0] for p in store.puts} == {'saiep-records'}
    keys = [p[1] for p in store.puts]
    assert len(set(keys)) == 2 and all(k.startswith('decisions/') and k.endswith('.json') for k in keys)
    assert '-intent-' in keys[0] and '-outcome-' in keys[1]
    assert len((tmp_path / 'd.jsonl').read_text().splitlines()) == 2


def test_shipping_failure_keeps_local_record(tmp_path):
    shipper = records.RecordShipper(_Store(fail=99), attempts=2, backoff=0)
    log = records.DecisionLog(str(tmp_path / 'd.jsonl'), shipper=shipper)
    log.intent(user='a', namespace='n', workflow=None, request=None, decision='rejected')
    shipper.flush()
    assert len((tmp_path / 'd.jsonl').read_text().splitlines()) == 1
