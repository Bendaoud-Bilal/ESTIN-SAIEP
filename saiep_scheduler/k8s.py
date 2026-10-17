"""Thin, blocking Kubernetes access for the hub. Call from an executor.

Configured by env:
  SAIEP_KUBECONFIG        kubeconfig for the saiep-hub identity (required to enable jobs)
  SAIEP_API_SERVER_CIDR   API server endpoint CIDR allowed in tenant egress (e.g. 172.18.0.2/32)
  SAIEP_API_SERVER_PORT   API server endpoint port (default 6443)
  SAIEP_MINIO_EXECUTOR_ACCESS_KEY / _SECRET_KEY
                          write-only MinIO user copied into each tenant namespace
                          for Argo log archiving
"""
import os

from kubernetes import client, config, dynamic
from kubernetes.client.rest import ApiException

from . import objectstore, tenants

FIELD_MANAGER = 'saiep-scheduler'
ARGO_GROUP, ARGO_VERSION, WF_PLURAL = 'argoproj.io', 'v1alpha1', 'workflows'


def enabled():
    return bool(os.environ.get('SAIEP_KUBECONFIG'))


class Cluster:
    def __init__(self, kubeconfig=None):
        kubeconfig = kubeconfig or os.environ['SAIEP_KUBECONFIG']
        self.api_client = config.new_client_from_config(config_file=kubeconfig)
        self.core = client.CoreV1Api(self.api_client)
        self.custom = client.CustomObjectsApi(self.api_client)
        self.dynamic = dynamic.DynamicClient(self.api_client)

    # --- provisioning --------------------------------------------------------
    def _apply(self, obj):
        res = self.dynamic.resources.get(api_version=obj['apiVersion'], kind=obj['kind'])
        res.server_side_apply(
            body=obj,
            name=obj['metadata']['name'],
            namespace=obj['metadata'].get('namespace'),
            field_manager=FIELD_MANAGER,
            force_conflicts=True,
        )

    def is_provisioned(self, namespace):
        try:
            ns = self.core.read_namespace(namespace)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        labels = ns.metadata.labels or {}
        return (labels.get(tenants.PROVISIONED_LABEL) == 'true'
                and labels.get(tenants.BUNDLE_VERSION_LABEL) == tenants.BUNDLE_VERSION)

    def ensure_tenant(self, username, tier=None):
        """Provision the user's namespace atomically (invariant I-8).

        All objects are applied, then the namespace is marked provisioned.
        If anything fails on a namespace we created, it is deleted again.
        """
        namespace = tenants.namespace_for(username)
        if self.is_provisioned(namespace):
            return namespace
        existed = True
        try:
            self.core.read_namespace(namespace)
        except ApiException as e:
            if e.status != 404:
                raise
            existed = False
        bundle = tenants.build_tenant_bundle(
            username,
            tier=tier,
            api_server_cidr=os.environ.get('SAIEP_API_SERVER_CIDR', '10.96.0.1/32'),
            api_server_port=int(os.environ.get('SAIEP_API_SERVER_PORT', '6443')),
            executor_access_key=os.environ.get('SAIEP_MINIO_EXECUTOR_ACCESS_KEY', ''),
            executor_secret_key=os.environ.get('SAIEP_MINIO_EXECUTOR_SECRET_KEY', ''),
        )
        try:
            for obj in bundle:
                self._apply(obj)
            self.core.patch_namespace(
                namespace, {'metadata': {'labels': {
                    tenants.PROVISIONED_LABEL: 'true',
                    tenants.BUNDLE_VERSION_LABEL: tenants.BUNDLE_VERSION,
                }}}
            )
        except Exception:
            if not existed:
                try:
                    self.core.delete_namespace(namespace)
                except ApiException:
                    pass
            raise
        return namespace

    # --- workflows -----------------------------------------------------------
    def submit(self, workflow):
        return self.custom.create_namespaced_custom_object(
            ARGO_GROUP, ARGO_VERSION, workflow['metadata']['namespace'], WF_PLURAL, workflow
        )

    def list_workflows(self, namespace):
        try:
            res = self.custom.list_namespaced_custom_object(
                ARGO_GROUP, ARGO_VERSION, namespace, WF_PLURAL
            )
        except ApiException as e:
            if e.status == 404:
                return []
            raise
        return res.get('items', [])

    def stop(self, namespace, name):
        # Argo honours spec.shutdown=Terminate: stops pods, marks workflow Failed.
        return self.custom.patch_namespaced_custom_object(
            ARGO_GROUP, ARGO_VERSION, namespace, WF_PLURAL, name, {'spec': {'shutdown': 'Terminate'}}
        )

    def get_workflow(self, namespace, name):
        return self.custom.get_namespaced_custom_object(
            ARGO_GROUP, ARGO_VERSION, namespace, WF_PLURAL, name
        )

    def logs(self, namespace, name, tail_lines=500, store=None):
        """Main-container logs of every pod of the workflow: live pods from
        the API, garbage-collected ones from the MinIO archive (archiveLogs)."""
        pods = self.core.list_namespaced_pod(
            namespace, label_selector=f'workflows.argoproj.io/workflow={name}'
        ).items
        entries = []
        for pod in pods:
            try:
                text = self.core.read_namespaced_pod_log(
                    pod.metadata.name, namespace, container='main', tail_lines=tail_lines
                )
            except ApiException as e:
                text = f'(journal indisponible: {e.reason})'
            entries.append((pod.metadata.creation_timestamp.isoformat(), pod.metadata.name, text))
        live = {pod_name for _, pod_name, _ in entries}
        if store is None and objectstore.enabled():
            store = objectstore.ObjectStore()
        if store is not None:
            try:
                wf = self.get_workflow(namespace, name)
            except ApiException:
                wf = {}
            for started, pod_name, key in archived_logs(wf, namespace, name):
                if pod_name in live:
                    continue
                try:
                    data = store.get(objectstore.LOGS_BUCKET, key).decode(errors='replace')
                    text = '\n'.join(data.splitlines()[-tail_lines:])
                except objectstore.ObjectStoreError as e:
                    text = f'(journal archivé indisponible: {e})'
                entries.append((started, f'{pod_name} (archivé)', text))
        return '\n'.join(f'=== {pod_name} ===\n{text}' for _, pod_name, text in sorted(entries))


def archived_logs(workflow, namespace, name):
    """(startedAt, pod name, key) of each archived main log of a workflow.

    Artifact locations come from WorkflowTaskResults, which the tenant pod's
    own identity can write: only keys under this workflow's prefix in the
    logs bucket are trusted, so a forged location cannot reach another
    tenant's logs through the hub's read credential.
    """
    prefix = f'{namespace}/{name}/'
    out = []
    for node in (workflow.get('status', {}).get('nodes') or {}).values():
        if node.get('type') != 'Pod':
            continue
        for art in (node.get('outputs') or {}).get('artifacts') or []:
            s3 = art.get('s3') or {}
            key = s3.get('key', '')
            if (art.get('name') != 'main-logs' or s3.get('bucket', objectstore.LOGS_BUCKET)
                    != objectstore.LOGS_BUCKET or not key.startswith(prefix) or '..' in key):
                continue
            pod_name = key[len(prefix):].split('/', 1)[0]
            out.append((node.get('startedAt') or '', pod_name, key))
    return out
