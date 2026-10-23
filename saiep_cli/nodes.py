"""Physical kubeadm node lifecycle used by TUI options 17–21.

The TUI runs on the target machine for prepare/join operations and on the
control plane for drain. No control-plane join is implemented here.
"""
import json
import os
import re
import shlex
import socket
from datetime import datetime, timezone

from . import tools

PROFILE_PATH = os.environ.get('SAIEP_NODE_PROFILE', '/etc/saiep/node-profile.json')
STORAGE_LABEL = 'saiep.estin.dz/storage'
GPU_LABEL = 'gpu'
GPU_TAINT = 'gpu=true:NoSchedule'
STORAGE_TAINT = f'{STORAGE_LABEL}=true:NoSchedule'
CPU_POOL_LABEL = 'saiep.estin.dz/pool=cpu'


class NodeError(RuntimeError):
    pass


def profile_path():
    return os.environ.get('SAIEP_NODE_PROFILE', PROFILE_PATH)


def read_profile(runner, path=None):
    path = path or profile_path()
    out = runner.run(['sudo', 'cat', path], check=False, echo=False).strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except ValueError as exc:
        raise NodeError(f'profil de nœud invalide ({path}) : {exc}') from exc


def write_profile(runner, profile, path=None):
    path = path or profile_path()
    directory = os.path.dirname(path)
    runner.run(['sudo', 'mkdir', '-p', directory], check=False, echo=False)
    data = json.dumps(profile, indent=2, ensure_ascii=False) + '\n'
    runner.run(['sudo', 'tee', path], input=data, capture=False, echo=False)
    runner.run(['sudo', 'chmod', '600', path], check=False, echo=False)
    return profile


def _now():
    return datetime.now(timezone.utc).isoformat()


def detect_gpu_hardware(runner):
    """Return True when NVIDIA hardware is present, regardless of driver state."""
    if runner.which('lspci'):
        return 'nvidia' in runner.run(['lspci'], check=False).lower()
    return runner.which('nvidia-smi') is not None


def detect_gpu(runner):
    """Return True when an NVIDIA GPU and a working driver are visible."""
    return bool(runner.which('nvidia-smi')) and runner.ok(['nvidia-smi'], echo=False)


def _nvidia_install(runner, log):
    """Install the host NVIDIA stack on Ubuntu/Debian; fail clearly otherwise."""
    if not runner.which('apt-get'):
        raise NodeError('installation GPU automatique supportée uniquement sur Debian/Ubuntu ; '
                        'installez le pilote NVIDIA et nvidia-container-toolkit manuellement.')
    if runner.which('ubuntu-drivers'):
        log('Installation du pilote NVIDIA via ubuntu-drivers…')
        runner.run(['sudo', 'ubuntu-drivers', 'autoinstall'], check=False, capture=False)
    else:
        raise NodeError('ubuntu-drivers est absent ; installez le pilote NVIDIA '
                        '(par exemple nvidia-driver-550) puis relancez cette option.')
    log('Installation de nvidia-container-toolkit…')
    if not runner.ok(['sudo', 'apt-get', 'install', '-y', 'nvidia-container-toolkit'],
                     echo=False):
        raise NodeError('nvidia-container-toolkit introuvable dans les dépôts. '
                        'Ajoutez le dépôt NVIDIA puis relancez cette option.')
    runner.run(['sudo', 'nvidia-ctk', 'runtime', 'configure', '--runtime=containerd'],
               check=False, capture=False)
    runner.run(['sudo', 'systemctl', 'restart', 'containerd'], check=False, capture=False)


def prepare_node(runner, *, gpu=False, k8s_minor='v1.36', log=print):
    """Prepare this machine to join a kubeadm cluster. Writes a root profile."""
    if os.name != 'posix':
        raise NodeError('la préparation physique nécessite Linux.')
    if not runner.ok(['sudo', '-n', 'true'], echo=False):
        log('[yellow]sudo demandera votre mot de passe.[/]')

    gpu_ready = detect_gpu(runner) if gpu else False
    requires_reboot = False
    if gpu:
        if not detect_gpu_hardware(runner):
            raise NodeError('aucun matériel NVIDIA détecté ; cette machine n\'est pas un nœud GPU.')
        if not gpu_ready:
            log('Pilote NVIDIA non opérationnel ; installation du pilote…')
            _nvidia_install(runner, log)
            requires_reboot = True
        if runner.which('nvidia-ctk'):
            runner.run(['sudo', 'nvidia-ctk', 'runtime', 'configure', '--runtime=containerd'],
                       check=False, capture=False)
            runner.run(['sudo', 'systemctl', 'restart', 'containerd'], check=False, capture=False)
            gpu_ready = True
        elif not requires_reboot:
            raise NodeError('nvidia-ctk absent : installez nvidia-container-toolkit.')
        if requires_reboot:
            raise NodeError('pilote NVIDIA installé. Redémarrez la machine, puis relancez '
                            'l\'option 18 pour vérifier le GPU avant de rejoindre le cluster.')

    tools.kubeadm_prepare_node(runner, k8s_minor)

    profile = {
        'schema': 1,
        'role': 'worker',
        'gpu': bool(gpu),
        'storage': False,
        'prepared_at': _now(),
        'kube_version': k8s_minor,
        'driver_ready': bool(gpu_ready) if gpu else True,
        'toolkit_ready': bool(runner.which('nvidia-ctk')) if gpu else True,
        'requires_reboot': False,
        'joined': False,
    }
    write_profile(runner, profile)
    log(f'[green]Nœud préparé ({k8s_minor}). Profil : {profile_path()}[/]')
    log('Sur le control plane : option 4 pour obtenir la commande de jointure, '
        'puis option 19 sur cette machine.')
    return profile


def validate_join_command(command):
    """Parse and validate a kubeadm worker join command; return argv after kubeadm."""
    if not command or not command.strip():
        raise NodeError('commande de jointure vide')
    try:
        argv = shlex.split(command.strip())
    except ValueError as exc:
        raise NodeError(f'commande de jointure invalide : {exc}') from exc
    if len(argv) < 3 or argv[0] != 'kubeadm' or argv[1] != 'join':
        raise NodeError('la commande doit commencer par « kubeadm join »')
    if '--control-plane' in argv:
        raise NodeError('l\'ajout d\'un control plane est hors périmètre.')
    if '--token' not in argv:
        raise NodeError('la commande doit contenir --token.')
    if '--discovery-token-ca-cert-hash' not in argv:
        raise NodeError('la commande doit contenir --discovery-token-ca-cert-hash.')
    endpoint = argv[2]
    if not re.match(r'^[A-Za-z0-9._:-]+$', endpoint):
        raise NodeError(f'endpoint de jointure invalide : {endpoint}')
    return argv


def redact_join(args):
    out, hide = [], False
    for arg in args:
        if hide:
            out.append('***')
            hide = False
            continue
        out.append(arg)
        if arg == '--token':
            hide = True
    return ' '.join(shlex.quote(a) for a in out)


def _cluster_reachable(runner):
    return runner.ok(['kubectl', 'cluster-info', '--request-timeout=5s'], echo=False)


def _apply_node_labels(runner, node, profile, *, storage_only=False, log=print):
    if not _cluster_reachable(runner):
        log('[yellow]kubectl ne joint pas le cluster depuis cette machine. '
            'Exécutez ces commandes sur le control plane :[/]')
        if profile.get('gpu'):
            log(f'  kubectl label node {node} {GPU_LABEL}=true --overwrite')
            log(f'  kubectl taint node {node} {GPU_TAINT} --overwrite')
        else:
            log(f'  kubectl label node {node} {CPU_POOL_LABEL} --overwrite')
        if profile.get('storage'):
            log(f'  kubectl label node {node} {STORAGE_LABEL}=true --overwrite')
            if storage_only:
                log(f'  kubectl taint node {node} {STORAGE_TAINT} --overwrite')
        return
    if profile.get('gpu'):
        out = runner.run(['kubectl', 'get', 'node', node, '-o',
                          'jsonpath={.status.capacity.nvidia\\.com/gpu}'], check=False).strip()
        if not out or out == '0':
            log('[yellow]nvidia.com/gpu absent : installez le device plugin NVIDIA avant '
                'de labeliser ce nœud comme GPU.[/]')
            log(f'  kubectl label node {node} {GPU_LABEL}=true --overwrite')
            log(f'  kubectl taint node {node} {GPU_TAINT} --overwrite')
            return
        runner.run(['kubectl', 'label', 'node', node, f'{GPU_LABEL}=true', '--overwrite'])
        runner.run(['kubectl', 'taint', 'node', node, GPU_TAINT, '--overwrite'])
    else:
        runner.run(['kubectl', 'label', 'node', node, CPU_POOL_LABEL, '--overwrite'],
                   check=False)
    if profile.get('storage'):
        runner.run(['kubectl', 'label', 'node', node, f'{STORAGE_LABEL}=true', '--overwrite'])
        if storage_only:
            runner.run(['kubectl', 'taint', 'node', node, STORAGE_TAINT, '--overwrite'])


def join_worker(runner, join_command, *, storage=False, storage_only=False, log=print):
    """Join this machine using a validated kubeadm worker command."""
    profile = read_profile(runner)
    if not profile:
        raise NodeError(f'profil absent ({profile_path()}). Lancez d\'abord l\'option 17 ou 18.')
    if profile.get('requires_reboot'):
        raise NodeError('un redémarrage est requis avant la jointure.')
    if profile.get('gpu') and not profile.get('driver_ready'):
        raise NodeError('le pilote NVIDIA n\'est pas prêt ; relancez l\'option 18.')
    args = validate_join_command(join_command)
    log(f"$ sudo {redact_join(args)}")
    runner.run(['sudo', *args], capture=False, echo=False)

    node = socket.gethostname()
    if '--node-name' in args:
        try:
            node = args[args.index('--node-name') + 1]
        except IndexError:
            pass
    profile.update({
        'joined': True,
        'joined_at': _now(),
        'storage': bool(storage),
        'storage_only': bool(storage_only),
        'node_name': node,
    })
    write_profile(runner, profile)
    _apply_node_labels(runner, node, profile, storage_only=storage_only, log=log)
    log(f'[green]Nœud {node} joint au cluster.[/]')
    log(f'  kubectl get node {node} -o wide')
    return node


def drain_node(runner, node, *, log=print):
    log(f'Drain de {node}…')
    runner.run(['kubectl', 'drain', node, '--ignore-daemonsets', '--delete-emptydir-data',
                '--force', '--timeout=300s'], capture=False)
    runner.run(['kubectl', 'cordon', node], check=False)
    log(f'[green]{node} drainé et cordonné. Le Node n\'est pas supprimé.[/]')
    log('Pour arrêter la machine : option 20 en mode local, ou :')
    log('  sudo systemctl stop kubelet containerd')
    log('  sudo poweroff   # seulement après confirmation')


def stop_local_services(runner, *, poweroff=False, log=print):
    runner.run(['sudo', 'systemctl', 'stop', 'kubelet', 'containerd'], capture=False)
    log('[green]kubelet et containerd arrêtés.[/]')
    if poweroff:
        runner.run(['sudo', 'poweroff'], capture=False)
        log('Extinction demandée.')


def list_nodes(runner):
    out = runner.run(['kubectl', 'get', 'nodes', '-o', 'name'], check=False)
    return [name.removeprefix('node/') for name in out.split()]


def worker_nodes(runner):
    out = runner.run(['kubectl', 'get', 'nodes', '-l',
                      '!node-role.kubernetes.io/control-plane', '-o', 'name'], check=False)
    return [name.removeprefix('node/') for name in out.split()]


def discover_disks(runner):
    return runner.run(['lsblk', '-o', 'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL'],
                      check=False).strip()


def prepare_data_disk(runner, device, mount_path='/srv/saiep-storage', *, log=print):
    """Mount a dedicated data disk and create the backup layout.

    This is intentionally explicit: callers must confirm device and mkfs.
    """
    if not re.match(r'^/dev/[A-Za-z0-9._/-]+$', device):
        raise NodeError(f'périphérique invalide : {device}')
    if not re.match(r'^/srv/[A-Za-z0-9._/-]+$', mount_path):
        raise NodeError(f'point de montage invalide : {mount_path}')
    fs = runner.run(['sudo', 'blkid', '-s', 'TYPE', '-o', 'value', device],
                    check=False).strip()
    if not fs:
        log(f'Formatage ext4 de {device}…')
        runner.run(['sudo', 'mkfs.ext4', '-F', device], capture=False)
    runner.run(['sudo', 'mkdir', '-p', mount_path], capture=False)
    if not runner.ok(['mountpoint', '-q', mount_path], echo=False):
        runner.run(['sudo', 'mount', device, mount_path], capture=False)
    uuid = runner.run(['sudo', 'blkid', '-s', 'UUID', '-o', 'value', device],
                      check=False).strip()
    if uuid:
        line = f'UUID={uuid} {mount_path} ext4 defaults,nofail 0 2\n'
        current = runner.run(['sudo', 'cat', '/etc/fstab'], check=False).strip()
        if mount_path not in current:
            runner.run(['sudo', 'tee', '-a', '/etc/fstab'], input=line,
                       capture=False, echo=False)
    for sub in ('minio', 'etcd', 'cluster-config', 'logs', 'restore-tests'):
        runner.run(['sudo', 'mkdir', '-p', os.path.join(mount_path, sub)], check=False)
    log(f'[green]Disque prêt : {device} -> {mount_path}[/]')
    return mount_path


def storage_commands(node, mount_path='/srv/saiep-storage', storage_only=False):
    cmds = [f'kubectl label node {node} {STORAGE_LABEL}=true --overwrite']
    if storage_only:
        cmds.append(f'kubectl taint node {node} {STORAGE_TAINT} --overwrite')
    return cmds
