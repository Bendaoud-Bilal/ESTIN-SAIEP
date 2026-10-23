"""SAIEP Job Intake — JupyterHub side of the Scheduling Layer (Path 2).

Every execution request from a researcher becomes an Argo Workflow in the
user's own namespace (saiep-u-<user>), provisioned on first use. Exposes:
  GET  /hub/jobs                    -> ESTIN-styled jobs page (templates/jobs.html)
  GET  /hub/jobs/api/list           -> the user's workflows (JSON)
  POST /hub/jobs/api/submit         -> validate + submit a job
  GET  /hub/jobs/api/logs/<name>    -> main-container logs
  POST /hub/jobs/api/stop/<name>    -> terminate a workflow
Users only ever see their own namespace: it is derived from the logged-in
user, never from request input. Disabled (page shows a notice) unless
SAIEP_KUBECONFIG is set.
"""
import asyncio
import json
import os
import re

from tornado import web

from jupyterhub.handlers import BaseHandler
from saiep_scheduler import advisor, k8s, notebooks, objectstore, records, tenants, workflows

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DECISIONS_LOG = os.path.join(BASE_DIR, 'scheduling-decisions.jsonl')

# Images offered in the form; the admission webhook allowlist is the
# authoritative check, this keeps the UX error early and clear.
JOB_IMAGES = [
    i.strip()
    for i in os.environ.get(
        'SAIEP_JOB_IMAGES',
        'python:3.12-slim,pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime,tensorflow/tensorflow:2.17.0',
    ).split(',')
    if i.strip()
]

# username -> quota tier (phd | researcher | external); others get the
# default tier from scheduler/policies/quota-tiers.yaml. Set from jupyterhub_config.py.
USER_TIERS = {}

_WF_NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
_cluster = None
_decisions = None


def _get_cluster():
    global _cluster
    if _cluster is None:
        _cluster = k8s.Cluster()
    return _cluster


def _get_decisions():
    global _decisions
    if _decisions is None:
        # Local JSONL stays the primary copy; MinIO gets one object per record.
        shipper = records.RecordShipper(objectstore.ObjectStore()) if objectstore.enabled() else None
        _decisions = records.DecisionLog(DECISIONS_LOG, shipper=shipper)
    return _decisions


async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _summary(wf):
    meta, status = wf.get('metadata', {}), wf.get('status', {})
    params = {p['name']: p.get('value') for p in wf.get('spec', {}).get('arguments', {}).get('parameters', [])}
    labels = meta.get('labels') or {}
    return {
        'kind': 'workflow',
        'name': meta.get('name'),
        'job': labels.get('saiep.estin.dz/job', ''),
        'tier': labels.get('saiep.estin.dz/tier', ''),
        'phase': status.get('phase') or 'Pending',
        'message': status.get('message', ''),
        'created': meta.get('creationTimestamp'),
        'started': status.get('startedAt'),
        'finished': status.get('finishedAt'),
        'image': params.get('image'),
        'cpu': params.get('cpu'),
        'memory': params.get('memory'),
        'gpu': params.get('gpu', '0'),
    }


class _JobsBase(BaseHandler):
    def _namespace(self):
        return tenants.namespace_for(self.current_user.name)

    def _json(self, data, status=200):
        self.set_status(status)
        self.set_header('Content-Type', 'application/json')
        self.finish(json.dumps(data))

    def _require_enabled(self):
        if not k8s.enabled():
            raise web.HTTPError(503, 'Cluster SAIEP non configuré')

    def _notebook(self, name):
        record = notebooks.find(name)
        return record if record and record.get('namespace') == self._namespace() else None


class JobsPageHandler(_JobsBase):
    @web.authenticated
    async def get(self):
        html = await self.render_template(
            'jobs.html',
            jobs_enabled=k8s.enabled(),
            images=JOB_IMAGES,
            cpu_choices=workflows.CPU_CHOICES,
            memory_choices=workflows.MEMORY_CHOICES,
            gpu_choices=workflows.GPU_CHOICES,
        )
        self.finish(html)


class JobsListHandler(_JobsBase):
    @web.authenticated
    async def get(self):
        self._require_enabled()
        items = await _run(_get_cluster().list_workflows, self._namespace())
        decisions = _get_decisions()
        for wf in items:
            decisions.outcome(wf)  # metrics emitter: records each termination once
        jobs = [_summary(w) for w in items]
        live = await _run(_get_cluster().notebook_pods, self._namespace())
        jobs += [j for j in notebooks.sessions(live=live)
                 if j.get('namespace') == self._namespace()]
        jobs.sort(key=lambda j: j.get('created') or '', reverse=True)
        self._json({'jobs': jobs})


class JobsSubmitHandler(_JobsBase):
    @web.authenticated
    async def post(self):
        self._require_enabled()
        user = self.current_user.name
        namespace = self._namespace()
        decisions = _get_decisions()
        try:
            body = json.loads(self.request.body or b'{}')
            req = workflows.JobRequest.from_form(body)
            if req.image not in JOB_IMAGES:
                raise ValueError('image non proposée par la plateforme')
            too_big = tenants.check_fits(req.cpu, req.memory, req.gpu, USER_TIERS.get(user))
            if too_big:
                raise ValueError(too_big)
        except ValueError as e:
            decisions.intent(user=user, namespace=namespace, workflow=None,
                             request=None, decision='rejected', reason=str(e))
            self._json({'error': str(e)}, 400)
            return
        cluster = _get_cluster()
        try:
            await _run(cluster.ensure_tenant, user, USER_TIERS.get(user))
            recommendation = await _run(advisor.recommend, req.as_dict())
            created = await _run(cluster.submit, workflows.build_workflow(namespace, user, req))
        except Exception as e:
            self.log.exception('SAIEP job submission failed for %s', user)
            decisions.intent(user=user, namespace=namespace, workflow=None,
                             request=req.as_dict(), decision='rejected', reason=f'cluster: {e}')
            self._json({'error': "Échec de la soumission au cluster"}, 502)
            return
        name = created['metadata']['name']
        decisions.intent(user=user, namespace=namespace, workflow=name,
                         request=req.as_dict(), decision='accepted',
                         recommendation=recommendation)
        self._json({'name': name, 'ai_recommendation': recommendation}, 201)


class JobsLogsHandler(_JobsBase):
    @web.authenticated
    async def get(self, name):
        self._require_enabled()
        if not _WF_NAME.match(name):
            raise web.HTTPError(400, 'nom invalide')
        if self._notebook(name):
            text = await _run(notebooks.read_log, self._namespace(), name)
        else:
            text = await _run(_get_cluster().logs, self._namespace(), name)
        self._json({'logs': text})


class JobsStopHandler(_JobsBase):
    @web.authenticated
    async def post(self, name):
        self._require_enabled()
        if not _WF_NAME.match(name):
            raise web.HTTPError(400, 'nom invalide')
        if self._notebook(name):
            spawner = self.current_user.spawner
            if not spawner or getattr(spawner, 'pod_name', None) != name:
                raise web.HTTPError(404, 'serveur notebook arrêté')
            await spawner.stop()
        else:
            await _run(_get_cluster().stop, self._namespace(), name)
        self._json({'stopped': name})


def job_handlers():
    return [
        (r'/jobs', JobsPageHandler),
        (r'/jobs/api/list', JobsListHandler),
        (r'/jobs/api/submit', JobsSubmitHandler),
        (r'/jobs/api/logs/([a-z0-9-]+)', JobsLogsHandler),
        (r'/jobs/api/stop/([a-z0-9-]+)', JobsStopHandler),
    ]
