"""Fresh install + verification of the cluster toolchain.

Installs only what is missing (kubectl, kind, helm; docker is checked, not
installed). Does not touch the JupyterHub venv. On macOS uses Homebrew; on
Linux downloads pinned official release binaries into /usr/local/bin.
For a kubeadm (bare-metal) node, kubeadm_prepare_node() runs the OS +
containerd + kubeadm phases from the team's install notes.
"""
import platform

from . import HELM_VERSION, KIND_VERSION

TOOLS = ('docker', 'kubectl', 'kind', 'helm')


def _arch():
    m = platform.machine().lower()
    return 'arm64' if m in ('arm64', 'aarch64') else 'amd64'


def install_commands(tool, system=None, arch=None):
    """Commands to install one tool; [] if the tool must be installed by hand."""
    system = (system or platform.system()).lower()
    arch = arch or _arch()
    if system == 'darwin':
        return [['brew', 'install', {'kubectl': 'kubernetes-cli'}.get(tool, tool)]] if tool != 'docker' else []
    if tool == 'kubectl':
        return [
            ['sh', '-c', 'curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/$(curl -fsSL '
                         f'https://dl.k8s.io/release/stable.txt)/bin/linux/{arch}/kubectl"'],
            ['sudo', 'install', '-m', '0755', '/tmp/kubectl', '/usr/local/bin/kubectl'],
        ]
    if tool == 'kind':
        return [
            ['curl', '-fsSLo', '/tmp/kind', f'https://kind.sigs.k8s.io/dl/{KIND_VERSION}/kind-linux-{arch}'],
            ['sudo', 'install', '-m', '0755', '/tmp/kind', '/usr/local/bin/kind'],
        ]
    if tool == 'helm':
        return [
            ['curl', '-fsSLo', '/tmp/helm.tgz', f'https://get.helm.sh/helm-{HELM_VERSION}-linux-{arch}.tar.gz'],
            ['tar', '-xzf', '/tmp/helm.tgz', '-C', '/tmp'],
            ['sudo', 'install', '-m', '0755', f'/tmp/linux-{arch}/helm', '/usr/local/bin/helm'],
        ]
    return []  # docker: distro-specific, install manually


def missing_tools(runner):
    return [t for t in TOOLS if not runner.which(t)]


def fresh_install(runner, log=print):
    for tool in missing_tools(runner):
        cmds = install_commands(tool)
        if not cmds:
            log(f'[yellow]{tool}: installez-le manuellement (https://docs.docker.com/engine/install/)[/]')
            continue
        log(f'Installation de {tool}…')
        for cmd in cmds:
            runner.run(cmd, capture=False)
    return missing_tools(runner)


def kubeadm_prepare_node(runner, k8s_minor):
    """OS prep + containerd + kubeadm/kubelet/kubectl on a Debian/Ubuntu node (needs sudo)."""
    script = f"""set -e
swapoff -a && sed -i '/ swap / s/^/#/' /etc/fstab
modprobe overlay && modprobe br_netfilter
printf 'overlay\\nbr_netfilter\\n' > /etc/modules-load.d/k8s.conf
printf 'net.bridge.bridge-nf-call-iptables = 1\\nnet.ipv4.ip_forward = 1\\n' > /etc/sysctl.d/k8s.conf
sysctl --system
apt-get update && apt-get install -y containerd apt-transport-https ca-certificates curl gpg
mkdir -p /etc/containerd && containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
systemctl restart containerd && systemctl enable containerd
mkdir -p /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/{k8s_minor}/deb/Release.key | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/{k8s_minor}/deb/ /' > /etc/apt/sources.list.d/kubernetes.list
apt-get update && apt-get install -y kubelet kubeadm kubectl && apt-mark hold kubelet kubeadm kubectl
"""
    runner.run(['sudo', 'bash', '-c', script], capture=False)


def verify(runner, kubeconfig_ok=True):
    """Return list of (check, ok, detail)."""
    results = []
    for tool in TOOLS:
        path = runner.which(tool)
        results.append((f'outil {tool}', bool(path), path or 'absent'))
    results.append(('démon docker', runner.ok(['docker', 'info']), ''))
    if not kubeconfig_ok:
        return results
    cluster = runner.ok(['kubectl', 'cluster-info', '--request-timeout=5s'])
    results.append(('cluster joignable', cluster, ''))
    if not cluster:
        return results
    checks = [
        ('PriorityClasses SAIEP', ['kubectl', 'get', 'priorityclass', 'saiep-batch', 'saiep-background']),
        ('Argo Workflows', ['kubectl', '-n', 'argo', 'rollout', 'status', 'deploy/workflow-controller', '--timeout=5s']),
        ('Modèles de calcul', ['kubectl', 'get', 'clusterworkflowtemplate', 'saiep-cpu-job', 'saiep-gpu-job']),
        ('Webhook d\'admission', ['kubectl', '-n', 'saiep-system', 'rollout', 'status', 'deploy/saiep-admission', '--timeout=5s']),
        ('Proxy de sortie', ['kubectl', '-n', 'saiep-egress', 'rollout', 'status', 'deploy/saiep-egress-proxy', '--timeout=5s']),
        ('RuntimeClass gVisor', ['kubectl', 'get', 'runtimeclass', 'gvisor']),
    ]
    for name, cmd in checks:
        results.append((name, runner.ok(cmd), ''))
    return results
