"""Deploy the storage-node backup stack (options 21, control-plane mode).

The stack contains:
  - a dedicated backup MinIO on the storage node (never the live MinIO),
  - Velero + Kopia/restic node-agent for Kubernetes objects and PVC data,
  - a daily Velero Schedule,
  - an etcd + control-plane configuration snapshot CronJob.
"""
import json
import os
import secrets
from string import Template

from . import REPO_DIR, STATE_DIR

BACKUP_NS = 'saiep-backup'
VELERO_NS = 'velero'
MINIO_IMAGE = 'cgr.dev/chainguard/minio@sha256:c93745c56fccdaf0e5dcb88999a96a65a136428beb045ef5d546255a80d747a7'
MC_IMAGE = 'cgr.dev/chainguard/minio-client@sha256:8d736271bb105639f2348d04186e748bbed63c5392e4ce86641453c11b53f3c8'
ETCD_IMAGE = 'quay.io/coreos/etcd:v3.5.15'
ALPINE_IMAGE = 'docker.io/library/alpine:3.20'


def _credentials_path():
    return os.path.join(STATE_DIR, 'backup-minio.json')


def credentials():
    path = _credentials_path()
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    creds = {
        'rootUser': 'saiep-backup-root',
        'rootPassword': secrets.token_urlsafe(32),
    }
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(creds, fh, indent=2)
    return creds


def render_stack(node, mount_path, backup_size='100Gi'):
    path = os.path.join(REPO_DIR, 'scheduler', 'backup', 'backup-stack.yaml')
    creds = credentials()
    with open(path) as fh:
        return Template(fh.read()).safe_substitute(
            NODE=node,
            MOUNT_PATH=mount_path.rstrip('/'),
            BACKUP_SIZE=backup_size,
            MINIO_IMAGE=MINIO_IMAGE,
            MC_IMAGE=MC_IMAGE,
            ETCD_IMAGE=ETCD_IMAGE,
            ALPINE_IMAGE=ALPINE_IMAGE,
            ROOT_USER=creds['rootUser'],
            ROOT_PASSWORD=creds['rootPassword'],
        )


def render_velero_values():
    path = os.path.join(REPO_DIR, 'scheduler', 'backup', 'velero-values.yaml')
    creds = credentials()
    with open(path) as fh:
        return Template(fh.read()).safe_substitute(
            ACCESS_KEY=creds['rootUser'],
            SECRET_KEY=creds['rootPassword'],
        )


def deploy_backup_stack(runner, node, mount_path, *, backup_size='100Gi',
                        storage_only=False, log=print):
    """Apply backup MinIO, etcd CronJob, Velero and the daily Schedule."""
    log('Déploiement du MinIO de sauvegarde et des snapshots etcd…')
    runner.run(['kubectl', '-n', BACKUP_NS, 'delete', 'job', 'saiep-backup-minio-setup',
                '--ignore-not-found'], check=False)
    runner.run(['kubectl', 'apply', '-f', '-'], input=render_stack(
        node, mount_path, backup_size=backup_size))
    runner.run(['kubectl', '-n', BACKUP_NS, 'rollout', 'status',
                'deploy/saiep-backup-minio', '--timeout=300s'])
    runner.run(['kubectl', '-n', BACKUP_NS, 'wait', '--for=condition=complete',
                'job/saiep-backup-minio-setup', '--timeout=300s'])

    values_path = os.path.join(STATE_DIR, 'velero-values.yaml')
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(values_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(render_velero_values())

    log('Installation de Velero (Kopia/restic)…')
    runner.run(['helm', 'repo', 'add', 'vmware-tanzu',
                'https://vmware-tanzu.github.io/helm-charts'], check=False)
    runner.run(['helm', 'repo', 'update'])
    runner.run(['helm', 'upgrade', '--install', 'velero', 'vmware-tanzu/velero',
                '-n', VELERO_NS, '--create-namespace', '-f', values_path],
               capture=False)
    runner.run(['kubectl', '-n', VELERO_NS, 'rollout', 'status',
                'deploy/velero', '--timeout=300s'])
    runner.run(['kubectl', 'apply', '-f', os.path.join(
        REPO_DIR, 'scheduler', 'backup', 'velero-schedule.yaml')])

    log('[green]Pile de sauvegarde déployée.[/]')
    log('  MinIO de sauvegarde : saiep-backup/saiep-backup-minio')
    log('  Velero              : velero/velero, planification saiep-daily')
    log('  etcd + config       : CronJob saiep-backup/saiep-etcd-snapshot')
    log(f'  Données             : {mount_path}')
    log('Restauration : voir admin guide.docx, section nœud de stockage.')
