"""Shared SAIEP Hub configuration: KubeSpawner notebooks in tenant namespaces.

Both the production config and the local-dev cluster config import this module.
The Hub itself must run inside the cluster so KubeSpawner can reach the Pod IP
of each single-user server.
"""
import asyncio
import json
import os
from datetime import datetime, timezone

from saiep_scheduler import k8s, notebooks, tenants

SIZING_FORM = """
<div class="saiep-sizing-form">
  <p>Veuillez dimensionner votre espace d'exécution :</p>
  <label for="cpu">Processeur (vCPU) :</label>
  <select name="cpu" class="form-control">
    <option value="0.5">0.5 vCPU</option>
    <option value="1" selected>1 vCPU</option>
    <option value="2">2 vCPU</option>
  </select>
  <br/>
  <label for="memory">Mémoire (RAM) :</label>
  <select name="memory" class="form-control">
    <option value="512M">512 Mo</option>
    <option value="1G" selected>1 Go</option>
    <option value="2G">2 Go</option>
    <option value="4G">4 Go</option>
  </select>
  <br/>
  <label for="duration">Durée estimée d'utilisation :</label>
  <select name="duration" class="form-control">
    <option value="1h" selected>1 heure</option>
    <option value="4h">4 heures</option>
    <option value="1d">1 journée</option>
    <option value="1w">1 semaine</option>
  </select>
  <br/>
  <label for="usage">Description de l'usage (librairies, type de calcul, dataset...) :</label>
  <textarea name="usage" class="form-control" rows="3"
    placeholder="Ex: entraînement d'un modèle scikit-learn sur un CSV de 200 Mo"></textarea>
</div>
"""

MEMORY = {'512M': '512Mi', '1G': '1Gi', '2G': '2Gi', '4G': '4Gi', '8G': '8Gi'}
DURATION = {'1h': 3600, '4h': 4 * 3600, '1d': 24 * 3600, '1w': 7 * 24 * 3600}


def options_from_form(formdata):
    return {
        'cpu': formdata.get('cpu', ['1'])[0],
        'memory': formdata.get('memory', ['1G'])[0],
        'duration': formdata.get('duration', ['1h'])[0],
        'usage': formdata.get('usage', [''])[0][:2000],
    }


def _memory(value):
    return MEMORY.get(str(value), str(value))


def _duration(value):
    return DURATION.get(str(value), 3600)


async def pre_spawn_hook(spawner):
    """Reserve the requested CPU/RAM and provision the tenant namespace."""
    import saiep_jobs

    options = spawner.user_options or {}
    cpu = str(options.get('cpu', '1'))
    # KubeSpawner's ByteSpecification accepts K/M/G/T, not Ki/Mi/Gi.
    memory = str(options.get('memory', '1G'))
    duration = _duration(options.get('duration', '1h'))
    user = spawner.user.name
    tier = saiep_jobs.USER_TIERS.get(user)

    error = tenants.check_fits(cpu, memory, 0, tier)
    if error:
        raise ValueError(error)

    spawner.cpu_limit = float(cpu)
    spawner.cpu_guarantee = float(cpu)
    spawner.mem_limit = memory
    spawner.mem_guarantee = memory
    spawner.extra_pod_config = {
        **(spawner.extra_pod_config or {}),
        'activeDeadlineSeconds': duration,
    }
    spawner.environment = {
        **(spawner.environment or {}),
        'SAIEP_CPU_REQUEST': cpu,
        'SAIEP_MEM_REQUEST': memory,
        'SAIEP_NOTEBOOK_DURATION': str(duration),
    }
    spawner._saiep_cpu = cpu
    spawner._saiep_memory = memory
    spawner._saiep_duration = duration

    try:
        path = os.environ.get('SAIEP_SPAWN_REQUESTS', '/srv/jupyterhub/spawn-requests.jsonl')
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, 'a') as fh:
            fh.write(json.dumps({
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'user': user,
                'options': options,
                'cpu': cpu,
                'memory': memory,
                'duration_seconds': duration,
            }, ensure_ascii=False) + '\n')
    except OSError:
        spawner.log.warning('Could not persist spawn sizing request')

    # Provision quota, limits, RBAC and NetworkPolicies before the Pod exists.
    cluster = saiep_jobs._get_cluster()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, cluster.ensure_tenant, user, tier)


async def after_pod_created_hook(spawner, pod):
    """Record the notebook in the same history used by Argo jobs."""
    notebooks.start(
        user=spawner.user.name,
        namespace=spawner.namespace,
        pod=pod['metadata']['name'],
        cpu=str(getattr(spawner, '_saiep_cpu', '')),
        memory=str(getattr(spawner, '_saiep_memory', '')),
        duration=int(getattr(spawner, '_saiep_duration', 0)),
        image=spawner.image,
    )


def configure(c, base_dir, *, notebook_image=None, storage_class=None,
              storage_capacity=None):
    """Apply the KubeSpawner contract to a JupyterHub config object."""
    c.JupyterHub.spawner_class = 'saiep_scheduler.spawner.SAIEPSpawner'
    c.JupyterHub.template_paths = [os.path.join(base_dir, 'templates')]

    c.KubeSpawner.enable_user_namespaces = True
    c.KubeSpawner.user_namespace_template = 'saiep-u-{username}'
    c.KubeSpawner.user_namespace_labels = {'saiep.estin.dz/tenant': 'true'}
    c.KubeSpawner.image = notebook_image or os.environ.get('SAIEP_NOTEBOOK_IMAGE', 'saiep/notebook:dev')
    c.KubeSpawner.image_pull_policy = 'IfNotPresent'
    c.KubeSpawner.cmd = ['jupyterhub-singleuser']
    c.KubeSpawner.default_url = '/lab'
    c.KubeSpawner.working_dir = '/home/jovyan'
    c.KubeSpawner.service_account = 'saiep-runner'
    c.KubeSpawner.automount_service_account_token = False
    c.KubeSpawner.pod_name_template = 'saiep-nb-{username}'
    c.KubeSpawner.priority_class_name = 'saiep-batch'
    c.KubeSpawner.start_timeout = 900
    c.KubeSpawner.http_timeout = 120
    c.KubeSpawner.events_enabled = False

    # Persistent home. KubeSpawner creates the PVC; the volume below mounts it.
    c.KubeSpawner.pvc_name_template = 'saiep-nb-{username}'
    c.KubeSpawner.storage_pvc_ensure = True
    c.KubeSpawner.storage_capacity = (
        storage_capacity or os.environ.get('SAIEP_NOTEBOOK_STORAGE', '10Gi')
    )
    c.KubeSpawner.storage_class = (
        storage_class or os.environ.get('SAIEP_STORAGE_CLASS', 'standard')
    )
    c.KubeSpawner.storage_access_modes = ['ReadWriteOnce']
    c.KubeSpawner.volumes = [{
        'name': 'home',
        'persistentVolumeClaim': {'claimName': 'saiep-nb-{username}'},
    }]
    c.KubeSpawner.volume_mounts = [{
        'name': 'home',
        'mountPath': '/home/jovyan',
    }]

    c.KubeSpawner.extra_labels = {
        'saiep.estin.dz/workload': 'notebook',
        'saiep.estin.dz/isolation-tier': 'standard',
        'saiep.estin.dz/user': '{username}',
    }
    # The admission webhook enforces the final security context; these values
    # also make a standalone (webhook-less) cluster safe.
    c.KubeSpawner.pod_security_context = {
        'runAsNonRoot': True,
        'seccompProfile': {'type': 'RuntimeDefault'},
    }
    c.KubeSpawner.container_security_context = {
        'allowPrivilegeEscalation': False,
        'readOnlyRootFilesystem': True,
        'capabilities': {'drop': ['ALL']},
        'seccompProfile': {'type': 'RuntimeDefault'},
    }

    c.Spawner.options_form = SIZING_FORM
    c.Spawner.options_from_form = options_from_form
    c.Spawner.pre_spawn_hook = pre_spawn_hook
    c.KubeSpawner.after_pod_created_hook = after_pod_created_hook

    c.JupyterHub.hub_ip = '0.0.0.0'
    # Hub API: bind locally. The proxy runs in the same Pod, so its route must
    # use loopback; otherwise the Service has no ready endpoint until the
    # readiness probe succeeds (a bootstrap deadlock).
    c.JupyterHub.hub_bind_url = os.environ.get('SAIEP_HUB_BIND_API', 'http://0.0.0.0:8081')
    c.JupyterHub.hub_connect_url = 'http://127.0.0.1:8081'
    # Notebooks run in other namespaces and must use the Service DNS name.
    c.KubeSpawner.hub_connect_url = os.environ.get(
        'SAIEP_HUB_CONNECT_URL',
        'http://jupyterhub.saiep-system.svc.cluster.local:8081',
    )
    return c
