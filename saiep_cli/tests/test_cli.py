import pytest
import yaml

from saiep_cli import platform, projects, providers, tools
from saiep_cli.runner import Runner
from saiep_cli.state import State


class FakeRunner(Runner):
    """Records commands; answers from a {first-args-tuple: output} table."""

    def __init__(self, outputs=None, present=('docker', 'kubectl', 'kind', 'helm')):
        super().__init__()
        self.outputs = outputs or {}
        self.present = set(present)

    def which(self, name):
        return f'/usr/bin/{name}' if name in self.present else None

    def run(self, cmd, **kw):
        self.history.append(list(cmd))
        for prefix, out in self.outputs.items():
            if tuple(cmd[:len(prefix)]) == prefix:
                return out
        return ''


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, 'STATE_DIR', str(tmp_path))
    return State(directory=str(tmp_path))


def test_hub_manifest_renders_without_placeholders():
    from string import Template
    text = Template(open(platform._p('scheduler', 'hub', 'hub.yaml')).read()).substitute(
        HUB_IMAGE='saiep/jupyterhub:dev',
        HUB_CONFIG='/app/testing/jupyterhub_config_saiep.py',
        PUBLIC_URL='http://127.0.0.1:8000',
        NOTEBOOK_IMAGE='saiep/notebook:dev',
        STORAGE_CLASS='standard',
        HUB_STORAGE='5Gi',
        NOTEBOOK_STORAGE='10Gi',
    )
    assert '${' not in text
    docs = [d for d in yaml.safe_load_all(text) if d]
    assert [d['kind'] for d in docs] == ['PersistentVolumeClaim', 'Deployment', 'Service']
    deploy = docs[1]
    env = {e['name']: e['value'] for e in deploy['spec']['template']['spec']['containers'][0]['env']}
    assert env['SAIEP_HUB_BIND_API'] == 'http://0.0.0.0:8081'
    assert env['SAIEP_NOTEBOOK_IMAGE'] == 'saiep/notebook:dev'


def test_kind_config_labels_gpu_workers():
    cfg = providers.kind_config(workers=2, gpu_workers=1)
    roles = [n['role'] for n in cfg['nodes']]
    assert roles == ['control-plane', 'worker', 'worker', 'worker']
    assert cfg['nodes'][-1]['labels']['gpu'] == 'true'


def test_kind_stop_and_start_order():
    r = FakeRunner({('kind', 'get', 'nodes'): 'saiep-worker\nsaiep-control-plane\nsaiep-worker2\n'})
    p = providers.KindProvider(r)
    p.start_all()
    assert r.history[-1] == ['docker', 'start', 'saiep-control-plane', 'saiep-worker', 'saiep-worker2']
    p.stop_all()
    assert r.history[-1][:2] == ['docker', 'stop'] and r.history[-1][-1] == 'saiep-control-plane'


def test_kubeadm_stop_node_drains():
    r = FakeRunner()
    providers.KubeadmProvider(r).stop_node('w1')
    assert r.history[-1][:3] == ['kubectl', 'drain', 'w1']


def test_install_only_missing_tools():
    r = FakeRunner(present=('docker', 'kubectl'))
    assert tools.missing_tools(r) == ['kind', 'helm']
    assert tools.install_commands('kind', system='Darwin') == [['brew', 'install', 'kind']]
    linux = tools.install_commands('helm', system='Linux', arch='amd64')
    assert any('helm-v' in ' '.join(c) for c in linux)
    assert tools.install_commands('docker', system='Linux') == []


def test_start_docker_enables_service_and_waits(monkeypatch):
    class DownThenUp(FakeRunner):
        checks = 0

        def ok(self, cmd, *, echo=True):
            if cmd == ['docker', 'info']:
                self.checks += 1
                return self.checks > 1
            return True

    r = DownThenUp()
    monkeypatch.setattr(tools.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(tools.time, 'sleep', lambda _: None)
    assert tools.start_docker(r, log=lambda *_: None)
    assert ['sudo', 'systemctl', 'enable', '--now', 'docker'] in r.history


def test_render_pins_node_forces_namespace_and_drops_cluster_objects():
    docs = [
        {'kind': 'Deployment', 'metadata': {'name': 'api', 'namespace': 'kube-system'},
         'spec': {'replicas': 2, 'template': {'spec': {'containers': []}}}},
        {'kind': 'CronJob', 'metadata': {'name': 'c'},
         'spec': {'jobTemplate': {'spec': {'template': {'spec': {}}}}}},
        {'kind': 'ClusterRoleBinding', 'metadata': {'name': 'pwn'}},
    ]
    kept, skipped = projects.render('demo', docs, 'saiep-worker')
    assert kept[0]['kind'] == 'Namespace' and kept[0]['metadata']['name'] == 'saiep-p-demo'
    dep = kept[1]
    assert dep['metadata']['namespace'] == 'saiep-p-demo'
    assert dep['spec']['template']['spec']['nodeSelector'] == {'kubernetes.io/hostname': 'saiep-worker'}
    assert projects.PIN_TOLERATION in dep['spec']['template']['spec']['tolerations']
    cron = kept[2]['spec']['jobTemplate']['spec']['template']['spec']
    assert cron['nodeSelector']['kubernetes.io/hostname'] == 'saiep-worker'
    assert skipped == ['ClusterRoleBinding/pwn']


def test_github_blob_url_rewritten_to_raw():
    assert projects._github_raw('https://github.com/a/b/blob/main/k8s/app.yaml') == \
        'https://raw.githubusercontent.com/a/b/main/k8s/app.yaml'


def test_load_documents_skips_template_files(tmp_path):
    (tmp_path / 'app.yaml').write_text(yaml.safe_dump({'apiVersion': 'apps/v1', 'kind': 'Deployment',
                                                       'metadata': {'name': 'web'}}))
    # Same shape as isolator/network/networkpolicy.yaml: ${...} inside a flow mapping.
    (tmp_path / 'template.yaml').write_text(
        'kind: NetworkPolicy\nspec:\n  egress:\n    - {protocol: TCP, port: ${API_SERVER_PORT}}\n')
    docs = projects.load_documents(str(tmp_path), FakeRunner(), str(tmp_path / 'work'))
    assert [d['kind'] for d in docs] == ['Deployment']


def test_load_documents_reports_real_invalid_yaml(tmp_path):
    (tmp_path / 'broken.yaml').write_text('kind: Deployment\nspec: [unclosed\n')
    with pytest.raises(ValueError, match='YAML invalide'):
        projects.load_documents(str(tmp_path), FakeRunner(), str(tmp_path / 'work'))


def test_platform_repository_is_not_a_project(tmp_path):
    (tmp_path / 'saiep_cli').mkdir()
    (tmp_path / 'jupyterhub_config.py').write_text('')
    with pytest.raises(ValueError, match='plateforme ESTIN-SAIEP'):
        projects.load_documents(str(tmp_path), FakeRunner(), str(tmp_path / 'work'))


def test_project_lifecycle_from_local_path(tmp_path, state):
    src = tmp_path / 'app.yaml'
    src.write_text(yaml.safe_dump({'apiVersion': 'apps/v1', 'kind': 'Deployment',
                                   'metadata': {'name': 'web'},
                                   'spec': {'replicas': 3, 'template': {'spec': {}}}}))
    r = FakeRunner()
    projects.deploy(r, state, 'demo', str(src), 'saiep-worker')
    assert r.history[-1][:3] == ['kubectl', 'apply', '-f']
    assert State(directory=state.directory)['projects']['demo']['replicas'] == {'deployment/web': 3}
    projects.scale(r, state, 'demo', up=False)
    assert r.history[-1][-1] == '--replicas=0'
    projects.scale(r, state, 'demo', up=True)
    assert r.history[-1][-1] == '--replicas=3'
    projects.uninstall(r, state, 'demo')
    assert ['kubectl', 'delete', 'namespace', 'saiep-p-demo', '--ignore-not-found'] in r.history
    assert 'demo' not in State(directory=state.directory)['projects']


def test_empty_project_source_rejected(tmp_path, state):
    (tmp_path / 'template.yaml').write_text('kind: Pod\nspec: {x: ${VAR}}\n')
    with pytest.raises(ValueError, match='aucun manifest'):
        projects.deploy(FakeRunner(), state, 'empty', str(tmp_path), 'saiep-worker')


def test_invalid_project_name_rejected(state):
    with pytest.raises(ValueError):
        projects.deploy(FakeRunner(), state, 'Bad Name', '/dev/null', 'n')


def test_render_webhook_substitutes_everything():
    text = platform.render_webhook(b'PEM', image='img:1', sandbox_runtime='gvisor')
    assert '${' not in text
    docs = [d for d in yaml.safe_load_all(text) if d]
    mwc = next(d for d in docs if d['kind'] == 'MutatingWebhookConfiguration')
    assert mwc['webhooks'][0]['clientConfig']['caBundle'] == 'UEVN'
    assert mwc['webhooks'][0]['namespaceSelector']['matchLabels'] == {'saiep.estin.dz/tenant': 'true'}


def test_deploy_stack_order_webhook_last(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, 'STATE_DIR', str(tmp_path))
    r = Runner(dry_run=True)

    class P:
        def load_image(self, image):
            r.history.append(['kind', 'load', image])

    platform.deploy_stack(r, P(), log=lambda *_: None)
    flat = [' '.join(c) for c in r.history]
    argo = next(i for i, c in enumerate(flat) if 'argo-workflows/releases' in c)
    templates = next(i for i, c in enumerate(flat) if c.endswith('scheduler/argo/workflows.yaml'))
    minio = next(i for i, c in enumerate(flat) if c.endswith('scheduler/storage/minio.yaml'))
    webhook = next(i for i, c in enumerate(flat) if 'deploy/saiep-admission' in c)
    assert argo < templates < minio < webhook


def test_minio_credentials_generated_once_private_and_kept_off_argv(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(platform, 'STATE_DIR', str(tmp_path))
    r = Runner(dry_run=True)
    platform.deploy_minio(r)
    creds = platform.minio_credentials()
    assert creds == platform.minio_credentials()  # reused, not regenerated
    assert os.stat(tmp_path / 'minio.json').st_mode & 0o077 == 0
    assert len({creds['rootPassword'], creds['executorSecretKey'], creds['hubSecretKey']}) == 3
    argv = ' '.join(' '.join(c) for c in r.history)
    assert creds['rootPassword'] not in argv and creds['hubSecretKey'] not in argv


def test_state_round_trip(tmp_path):
    s = State(directory=str(tmp_path))
    s['provider'] = 'kubeadm'
    s.save()
    assert State(directory=str(tmp_path))['provider'] == 'kubeadm'


def test_refresh_tenant_network_rerenders_every_tenant_with_current_endpoint():
    applied = []

    class Recording(FakeRunner):
        def run(self, cmd, **kw):
            if kw.get('input'):
                applied.append(kw['input'])
            return super().run(cmd, **kw)

    r = Recording({
        ('kubectl', 'get', 'namespaces'): 'saiep-u-alice saiep-u-bob',
        ('kubectl', 'get', 'endpointslice'): '172.23.0.5 6443',
    })
    assert platform.refresh_tenant_network(r, log=lambda *_: None) == ['saiep-u-alice', 'saiep-u-bob']
    assert len(applied) == 2
    for ns, text in zip(['saiep-u-alice', 'saiep-u-bob'], applied):
        docs = list(yaml.safe_load_all(text))
        assert {d['metadata']['namespace'] for d in docs} == {ns}
        api = next(d for d in docs if d['metadata']['name'] == 'saiep-allow-dns-and-proxy')
        assert api['spec']['egress'][-1]['to'] == [{'ipBlock': {'cidr': '172.23.0.5/32'}}]
    assert ['kubectl', 'apply', '--server-side', '--force-conflicts',
            '--field-manager=saiep-scheduler', '-f', '-'] in r.history


def test_refresh_tenant_network_is_noop_without_tenants():
    r = FakeRunner()
    assert platform.refresh_tenant_network(r, log=lambda *_: None) == []
    assert not any(c[:3] == ['kubectl', 'get', 'endpointslice'] for c in r.history)


def test_api_server_endpoint_lists_all_addresses():
    r = FakeRunner({('kubectl', 'get', 'endpointslice'): '10.0.0.1 10.0.0.2 6443'})
    assert platform.api_server_endpoint(r) == (['10.0.0.1/32', '10.0.0.2/32'], '6443')
