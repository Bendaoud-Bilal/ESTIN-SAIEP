"""Notebook session records and log archiving for the KubeSpawner layer.

Each interactive notebook is a KubeSpawner Pod in the user's tenant namespace.
This module keeps the append-only session history shown on ``/hub/jobs``
("Mes calculs"), and archives notebook logs to the same MinIO bucket/key
convention as Argo job logs so the existing Journal action keeps working after
the Pod is deleted.
"""
import json
import os
import threading
from datetime import datetime, timezone

from . import objectstore

DEFAULT_LOG = '/srv/jupyterhub/notebook-sessions.jsonl'


def _now():
    return datetime.now(timezone.utc).isoformat()


def log_path():
    return os.environ.get('SAIEP_NOTEBOOK_LOG', DEFAULT_LOG)


def archive_key(namespace, pod_name):
    return f'{namespace}/notebook/{pod_name}/main.log'


def _append(record, path=None):
    path = path or log_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    with open(path, 'a') as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    return record


def _read(path=None):
    path = path or log_path()
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as fh:
        for line in fh:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def start(*, user, namespace, pod, cpu, memory, duration, image, path=None):
    """Record a notebook Pod after KubeSpawner created it."""
    return _append({
        'stage': 'start',
        'timestamp': _now(),
        'user': user,
        'namespace': namespace,
        'pod': pod,
        'job': f'notebook-{user}',
        'cpu': cpu,
        'memory': memory,
        'gpu': '0',
        'duration_seconds': int(duration),
        'image': image,
        'log_key': archive_key(namespace, pod),
    }, path=path)


def finish(*, user, namespace, pod, phase='Succeeded', message='', log_key='', path=None):
    """Record the end of a notebook session (idempotent enough for retries)."""
    return _append({
        'stage': 'stop',
        'timestamp': _now(),
        'user': user,
        'namespace': namespace,
        'pod': pod,
        'phase': phase,
        'message': message,
        'log_key': log_key or archive_key(namespace, pod),
    }, path=path)


def sessions(path=None, live=None):
    """Merge start/stop records into the format used by the jobs table.

    A notebook Pod name is reused across restarts, so only the latest start
    wins; a stop record is attached only if it happened after that start.
    ``live`` is an optional {pod_name: phase} map used to avoid showing a
    notebook as still running after its Pod disappeared or completed.
    """
    latest = {}
    for rec in _read(path):
        pod = rec.get('pod')
        if not pod:
            continue
        if rec.get('stage') == 'start':
            latest[pod] = {'start': rec, 'stop': {}}
        elif rec.get('stage') == 'stop':
            entry = latest.get(pod)
            if entry and (rec.get('timestamp') or '') >= (entry['start'].get('timestamp') or ''):
                entry['stop'] = rec
    out = []
    for pod, entry in latest.items():
        start_rec, stop = entry['start'], entry['stop']
        phase = stop.get('phase') or 'Running'
        if not stop and live is not None:
            pod_phase = live.get(pod)
            if pod_phase == 'Failed':
                phase = 'Failed'
            elif pod_phase == 'Pending':
                phase = 'Pending'
            elif pod_phase in (None, 'Succeeded'):
                phase = 'Succeeded'
        out.append({
            'kind': 'notebook',
            'name': pod,
            'job': start_rec.get('job') or f"notebook-{start_rec.get('user', '')}",
            'phase': phase,
            'message': stop.get('message', ''),
            'created': start_rec.get('timestamp'),
            'started': start_rec.get('timestamp'),
            'finished': stop.get('timestamp'),
            'image': start_rec.get('image'),
            'cpu': start_rec.get('cpu'),
            'memory': start_rec.get('memory'),
            'gpu': '0',
            'namespace': start_rec.get('namespace'),
            'user': start_rec.get('user'),
            'log_key': stop.get('log_key') or start_rec.get('log_key'),
            'duration_seconds': start_rec.get('duration_seconds'),
        })
    return sorted(out, key=lambda item: item.get('created') or '', reverse=True)


def find(name, path=None):
    return next((item for item in sessions(path) if item['name'] == name), None)


def archive(spawner):
    """Best-effort copy of the live Pod log to MinIO before KubeSpawner deletes it."""
    namespace = getattr(spawner, 'namespace', None)
    pod = getattr(spawner, 'pod_name', None)
    if not namespace or not pod:
        return ''
    key = archive_key(namespace, pod)
    try:
        from . import k8s
        text = k8s.Cluster().pod_logs(namespace, pod, tail_lines=None)
        store = objectstore.ObjectStore(
            access_key=os.environ.get('SAIEP_MINIO_EXECUTOR_ACCESS_KEY'),
            secret_key=os.environ.get('SAIEP_MINIO_EXECUTOR_SECRET_KEY'),
        )
        store.put(objectstore.LOGS_BUCKET, key, text.encode())
        return key
    except Exception:
        return ''


def read_log(namespace, name, tail_lines=500):
    """Live Pod log first, then the archived object. Returns display text."""
    record = find(name)
    if record and record.get('phase') == 'Running':
        try:
            from . import k8s
            return k8s.Cluster().pod_logs(namespace, name, tail_lines=tail_lines)
        except Exception:
            pass
    log_key = (record or {}).get('log_key') or archive_key(namespace, name)
    try:
        data = objectstore.ObjectStore().get(objectstore.LOGS_BUCKET, log_key)
        return '\n'.join(data.decode(errors='replace').splitlines()[-tail_lines:])
    except Exception as exc:
        if record:
            return f'(journal archivé indisponible: {exc})'
        raise


class SessionRecorder:
    """Small helper used by the spawner/config without a global lock."""

    def __init__(self, path=None):
        self.path = path or log_path()
        self._lock = threading.Lock()

    def start(self, **kwargs):
        with self._lock:
            return start(path=self.path, **kwargs)

    def finish(self, **kwargs):
        with self._lock:
            return finish(path=self.path, **kwargs)

    def sessions(self):
        with self._lock:
            return sessions(self.path)
