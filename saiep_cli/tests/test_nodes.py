import json

import pytest

from saiep_cli import nodes
from saiep_cli.runner import Runner


class FakeRunner(Runner):
    def __init__(self, outputs=None):
        super().__init__()
        self.outputs = outputs or {}

    def run(self, cmd, **kw):
        self.history.append(list(cmd))
        for prefix, out in self.outputs.items():
            if tuple(cmd[:len(prefix)]) == prefix:
                return out
        return ''


def test_join_command_rejects_control_plane():
    with pytest.raises(nodes.NodeError, match='control plane'):
        nodes.validate_join_command(
            'kubeadm join 10.0.0.1:6443 --token a.b '
            '--discovery-token-ca-cert-hash sha256:abc --control-plane'
        )


def test_join_command_requires_token_and_hash():
    with pytest.raises(nodes.NodeError, match='--token'):
        nodes.validate_join_command('kubeadm join 10.0.0.1:6443')
    with pytest.raises(nodes.NodeError, match='discovery-token'):
        nodes.validate_join_command('kubeadm join 10.0.0.1:6443 --token a.b')


def test_join_command_is_parsed_and_redacted():
    args = nodes.validate_join_command(
        'kubeadm join 10.0.0.1:6443 --token abc.def '
        '--discovery-token-ca-cert-hash sha256:123'
    )
    assert args[0:3] == ['kubeadm', 'join', '10.0.0.1:6443']
    redacted = nodes.redact_join(args)
    assert 'abc.def' not in redacted
    assert '***' in redacted


def test_profile_round_trip(tmp_path, monkeypatch):
    path = tmp_path / 'node-profile.json'
    monkeypatch.setenv('SAIEP_NODE_PROFILE', str(path))
    profile = {'schema': 1, 'role': 'worker', 'gpu': True, 'joined': False}
    # write_profile uses sudo tee; emulate the file write for this unit test
    path.write_text(json.dumps(profile))
    r = FakeRunner({('sudo', 'cat', str(path)): path.read_text()})
    assert nodes.read_profile(r)['gpu'] is True
    assert nodes.profile_path() == str(path)


def test_prepare_node_dry_run():
    r = Runner(dry_run=True)
    profile = nodes.prepare_node(r, gpu=False, k8s_minor='v1.36', log=lambda *_: None)
    assert profile['role'] == 'worker'
    assert profile['gpu'] is False
    assert any('kubeadm' in ' '.join(c) for c in r.history)


def test_join_worker_rejects_unprepared_node():
    r = FakeRunner()
    with pytest.raises(nodes.NodeError, match='profil absent'):
        nodes.join_worker(r, 'kubeadm join 10.0.0.1:6443 --token a.b '
                             '--discovery-token-ca-cert-hash sha256:123')


def test_drain_keeps_node_object():
    r = FakeRunner()
    nodes.drain_node(r, 'worker-1', log=lambda *_: None)
    assert any(c[:3] == ['kubectl', 'drain', 'worker-1'] for c in r.history)
    assert not any(c[:3] == ['kubectl', 'delete', 'node'] for c in r.history)


def test_storage_commands_default_has_no_taint():
    cmds = nodes.storage_commands('worker-1')
    assert any('saiep.estin.dz/storage=true' in c for c in cmds)
    assert not any('taint' in c for c in cmds)
    assert any('taint' in c for c in nodes.storage_commands('worker-1', storage_only=True))
