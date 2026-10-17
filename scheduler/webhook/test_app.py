import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import app  # noqa: E402

NS = 'saiep-u-alice'


@pytest.fixture
def policy():
    return app.Policy(env={})


def pod(**spec):
    base = {
        'priorityClassName': 'saiep-batch',
        'containers': [{'name': 'main', 'image': 'python:3.12'}],
    }
    base.update(spec)
    return {'metadata': {'name': 'p', 'labels': {}}, 'spec': base}


def admission(obj, op='CREATE'):
    return {'request': {'uid': 'u1', 'kind': {'kind': 'Pod'}, 'operation': op,
                        'namespace': NS, 'object': obj}}


@pytest.mark.parametrize('image,expected', [
    ('python:3.12', 'docker.io/library/python:3.12'),
    ('python', 'docker.io/library/python:latest'),
    ('pytorch/pytorch:2.4', 'docker.io/pytorch/pytorch:2.4'),
    ('quay.io/argoproj/argoexec:v3.6.2', 'quay.io/argoproj/argoexec:v3.6.2'),
    ('localhost:5000/x', 'localhost:5000/x:latest'),
])
def test_normalize_image(image, expected):
    assert app.normalize_image(image) == expected


def test_lookalike_registry_not_allowed(policy):
    assert not app.image_allowed('docker.io.evil.com/library/python:3', policy.image_allowlist)
    assert not app.image_allowed('evil/python:3', policy.image_allowlist)
    assert app.image_allowed('python:3.12', policy.image_allowlist)


def test_mutation_injects_security_context_and_defaults(policy):
    out = app.mutate(pod(), NS, policy)
    spec = out['spec']
    uid = app.tenant_uid(NS)
    assert spec['securityContext']['runAsUser'] == uid
    assert spec['securityContext']['seccompProfile'] == {'type': 'RuntimeDefault'}
    c = spec['containers'][0]
    sc = c['securityContext']
    assert sc['readOnlyRootFilesystem'] and not sc['allowPrivilegeEscalation']
    assert sc['capabilities'] == {'drop': ['ALL']}
    assert {'name': app.TMP_VOLUME, 'mountPath': '/tmp'} in c['volumeMounts']
    assert c['resources']['limits'] == {'cpu': '500m', 'memory': '512Mi'}
    assert out['metadata']['labels'][app.ISOLATION_LABEL] == 'standard'
    assert 'runtimeClassName' not in spec


def test_conflicting_security_context_is_normalised(policy):
    p = pod(securityContext={'runAsUser': 0},
            containers=[{'name': 'main', 'image': 'python:3.12',
                         'securityContext': {'runAsUser': 0, 'allowPrivilegeEscalation': True,
                                             'capabilities': {'add': ['NET_ADMIN']}}}])
    out = app.mutate(p, NS, policy)
    assert out['spec']['securityContext']['runAsUser'] != 0
    sc = out['spec']['containers'][0]['securityContext']
    assert sc['runAsUser'] != 0 and 'add' not in sc['capabilities']


def test_mutation_is_idempotent(policy):
    once = app.mutate(pod(), NS, policy)
    assert app.mutate(once, NS, policy) == once


def test_gpu_pod_gets_gpu_node_placement(policy):
    p = pod(containers=[{'name': 'main', 'image': 'python:3.12',
                         'resources': {'limits': {'nvidia.com/gpu': '1'}}}])
    spec = app.mutate(p, NS, policy)['spec']
    assert spec['nodeSelector'] == {'gpu': 'true'}
    assert spec['tolerations'][0]['key'] == 'gpu'


def test_explicit_resources_are_kept(policy):
    p = pod(containers=[{'name': 'main', 'image': 'python:3.12',
                         'resources': {'limits': {'cpu': '2', 'memory': '4Gi'}}}])
    limits = app.mutate(p, NS, policy)['spec']['containers'][0]['resources']['limits']
    assert limits == {'cpu': '2', 'memory': '4Gi'}


@pytest.mark.parametrize('bad,reason', [
    ({'containers': [{'name': 'm', 'image': 'evil/miner:1'}]}, 'allowlist'),
    ({'initContainers': [{'name': 'i', 'image': 'evil/miner:1'}]}, 'allowlist'),
    ({'priorityClassName': 'system-cluster-critical'}, 'priorityClassName'),
    ({'priorityClassName': None}, 'priorityClassName'),
    ({'hostNetwork': True}, 'hostNetwork'),
    ({'volumes': [{'name': 'h', 'hostPath': {'path': '/'}}]}, 'hostPath'),
    ({'containers': [{'name': 'm', 'image': 'python:3', 'securityContext': {'privileged': True}}]},
     'privileged'),
])
def test_violations_denied(policy, bad, reason):
    resp = app.review(admission(pod(**bad)), policy, 'mutate')['response']
    assert not resp['allowed']
    assert reason in resp['status']['message']


def test_privileged_is_checked_before_normalisation(policy):
    # mutate() would reset privileged=False; the request must still be denied.
    p = pod(containers=[{'name': 'm', 'image': 'python:3', 'securityContext': {'privileged': True}}])
    assert not app.review(admission(p), policy, 'mutate')['response']['allowed']


def test_sandboxed_without_runtime_denied(policy):
    p = pod()
    p['metadata']['labels'][app.ISOLATION_LABEL] = 'sandboxed'
    assert not app.review(admission(p), policy, 'mutate')['response']['allowed']


def test_sandboxed_with_runtime_sets_runtime_class():
    policy = app.Policy(env={'SAIEP_SANDBOX_RUNTIME': 'gvisor'})
    p = pod()
    p['metadata']['labels'][app.ISOLATION_LABEL] = 'sandboxed'
    assert app.mutate(p, NS, policy)['spec']['runtimeClassName'] == 'gvisor'


def test_egress_proxy_env_injected():
    policy = app.Policy(env={'SAIEP_EGRESS_PROXY': 'http://proxy:3128'})
    env = app.mutate(pod(), NS, policy)['spec']['containers'][0]['env']
    assert {'name': 'HTTPS_PROXY', 'value': 'http://proxy:3128'} in env


def test_mutate_response_patch_applies_normalised_spec(policy):
    resp = app.review(admission(pod()), policy, 'mutate')['response']
    assert resp['allowed'] and resp['patchType'] == 'JSONPatch'
    patch = json.loads(base64.b64decode(resp['patch']))
    spec_op = next(op for op in patch if op['path'] == '/spec')
    assert spec_op['value'] == app.mutate(pod(), NS, policy)['spec']


def test_validate_accepts_normalised_and_rejects_raw(policy):
    normalised = app.mutate(pod(), NS, policy)
    assert app.review(admission(normalised), policy, 'validate')['response']['allowed']
    assert not app.review(admission(pod()), policy, 'validate')['response']['allowed']


@pytest.mark.parametrize('path,expected', [
    ('/mutate?timeout=5s', 'mutate'), ('/validate', 'validate'), ('/healthz', 'healthz'),
])
def test_route_ignores_query_string(path, expected):
    assert app.route(path) == expected


def test_server_handles_real_api_server_path(policy):
    import threading
    import urllib.request

    app.Handler.policy = policy
    server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f'http://127.0.0.1:{server.server_port}/mutate?timeout=5s',
            data=json.dumps(admission(pod())).encode(), method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        assert body['response']['allowed'] and body['response']['uid'] == 'u1'
    finally:
        server.shutdown()


def test_non_create_operations_pass_through(policy):
    resp = app.review(admission(pod(hostNetwork=True), op='UPDATE'), policy, 'mutate')['response']
    assert resp['allowed'] and 'patch' not in resp
