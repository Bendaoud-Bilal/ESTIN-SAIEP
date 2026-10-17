"""Cluster providers.

kind    — local/dev: every node is a Docker container (start/stop = docker).
kubeadm — production: real machines; this host is the control plane, other
          machines join with the printed command. Node stop/start = drain/uncordon.
"""
import os

import yaml

from . import KUBERNETES_MINOR, STATE_DIR
from .tools import kubeadm_prepare_node

GPU_LABEL = ('gpu', 'true')


def kind_config(workers, gpu_workers):
    nodes = [{'role': 'control-plane'}]
    nodes += [{'role': 'worker', 'labels': {'saiep.estin.dz/pool': 'cpu'}} for _ in range(workers)]
    nodes += [{'role': 'worker', 'labels': {'saiep.estin.dz/pool': 'gpu', GPU_LABEL[0]: GPU_LABEL[1]}}
              for _ in range(gpu_workers)]
    return {'kind': 'Cluster', 'apiVersion': 'kind.x-k8s.io/v1alpha4', 'nodes': nodes}


class KindProvider:
    name = 'kind'

    def __init__(self, runner, cluster='saiep', state_dir=STATE_DIR):
        self.r, self.cluster, self.state_dir = runner, cluster, state_dir

    def exists(self):
        out = self.r.run(['kind', 'get', 'clusters'], check=False)
        return self.cluster in out.split()

    def create_cluster(self, workers=2, gpu_workers=0):
        os.makedirs(self.state_dir, exist_ok=True)
        path = os.path.join(self.state_dir, 'kind-config.yaml')
        with open(path, 'w') as f:
            yaml.safe_dump(kind_config(workers, gpu_workers), f)
        self.r.run(['kind', 'create', 'cluster', '--name', self.cluster, '--config', path,
                    '--wait', '180s'], capture=False)
        self.r.run(['kubectl', 'config', 'use-context', f'kind-{self.cluster}'])
        self.taint_gpu_nodes()

    def taint_gpu_nodes(self):
        self.r.run(['kubectl', 'taint', 'nodes', '-l', 'gpu=true', 'gpu=true:NoSchedule', '--overwrite'],
                   check=False)

    def recreate(self, workers, gpu_workers):
        """kind cannot add nodes to a running cluster: rebuild it (destructive)."""
        self.delete()
        self.create_cluster(workers, gpu_workers)

    def delete(self):
        self.r.run(['kind', 'delete', 'cluster', '--name', self.cluster], capture=False)

    def nodes(self):
        out = self.r.run(['kind', 'get', 'nodes', '--name', self.cluster], check=False)
        names = [n for n in out.split() if n.startswith(self.cluster)]
        # control plane first so the API is up before kubelets reconnect
        return sorted(names, key=lambda n: ('control-plane' not in n, n))

    def start_all(self):
        nodes = self.nodes()
        if nodes:
            self.r.run(['docker', 'start', *nodes])

    def stop_all(self):
        nodes = self.nodes()
        if nodes:
            self.r.run(['docker', 'stop', *reversed(nodes)])

    def start_node(self, node):
        self.r.run(['docker', 'start', node])

    def stop_node(self, node):
        self.r.run(['docker', 'stop', node])

    def shell(self, node):
        self.r.run(['docker', 'exec', '-it', node, 'bash'], interactive=True)

    def load_image(self, image):
        self.r.run(['kind', 'load', 'docker-image', image, '--name', self.cluster], capture=False)


class KubeadmProvider:
    name = 'kubeadm'
    POD_CIDR = '192.168.0.0/16'

    def __init__(self, runner, cluster='saiep', cni_manifest=None):
        self.r, self.cluster = runner, cluster
        self.cni_manifest = cni_manifest or os.environ.get('SAIEP_CNI_MANIFEST', '')

    def exists(self):
        return self.r.ok(['kubectl', 'cluster-info', '--request-timeout=5s'])

    def create_cluster(self, workers=0, gpu_workers=0):
        kubeadm_prepare_node(self.r, KUBERNETES_MINOR)
        self.r.run(['sudo', 'kubeadm', 'init', f'--pod-network-cidr={self.POD_CIDR}'], capture=False)
        home = os.path.expanduser('~')
        self.r.run(['sh', '-c', f'mkdir -p {home}/.kube && sudo cp /etc/kubernetes/admin.conf '
                                f'{home}/.kube/config && sudo chown $(id -u):$(id -g) {home}/.kube/config'])
        if self.cni_manifest:  # must enforce NetworkPolicy (Calico / Cilium)
            self.r.run(['kubectl', 'apply', '-f', self.cni_manifest], capture=False)

    def join_command(self):
        return self.r.run(['sudo', 'kubeadm', 'token', 'create', '--print-join-command']).strip()

    def label_gpu(self, node):
        self.r.run(['kubectl', 'label', 'node', node, 'gpu=true', '--overwrite'])
        self.r.run(['kubectl', 'taint', 'node', node, 'gpu=true:NoSchedule', '--overwrite'])

    def nodes(self):
        out = self.r.run(['kubectl', 'get', 'nodes', '-o', 'name'], check=False)
        return [n.removeprefix('node/') for n in out.split()]

    def _workers(self):
        out = self.r.run(['kubectl', 'get', 'nodes', '-l', '!node-role.kubernetes.io/control-plane',
                          '-o', 'name'], check=False)
        return [n.removeprefix('node/') for n in out.split()]

    def start_all(self):
        for node in self._workers():
            self.start_node(node)

    def stop_all(self):
        for node in self._workers():
            self.stop_node(node)

    def start_node(self, node):
        self.r.run(['kubectl', 'uncordon', node])

    def stop_node(self, node):
        self.r.run(['kubectl', 'drain', node, '--ignore-daemonsets', '--delete-emptydir-data',
                    '--timeout=300s'], capture=False)

    def shell(self, node):
        self.r.run(['kubectl', 'debug', f'node/{node}', '-it', '--image=busybox:1.36'], interactive=True)

    def load_image(self, image):
        raise RuntimeError(
            f'kubeadm: poussez {image} vers un registre accessible aux nœuds '
            '(SAIEP_WEBHOOK_IMAGE) avant le déploiement.'
        )

    def delete(self):
        self.r.run(['sudo', 'kubeadm', 'reset', '-f'], capture=False)


def get_provider(state, runner):
    cls = KubeadmProvider if state['provider'] == 'kubeadm' else KindProvider
    return cls(runner, cluster=state['cluster'])
