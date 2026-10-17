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

from . import tenants

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

    def logs(self, namespace, name, tail_lines=500):
        pods = self.core.list_namespaced_pod(
            namespace, label_selector=f'workflows.argoproj.io/workflow={name}'
        ).items
        chunks = []
        for pod in sorted(pods, key=lambda p: p.metadata.creation_timestamp):
            try:
                text = self.core.read_namespaced_pod_log(
                    pod.metadata.name, namespace, container='main', tail_lines=tail_lines
                )
            except ApiException as e:
                text = f'(journal indisponible: {e.reason})'
            chunks.append(f'=== {pod.metadata.name} ===\n{text}')
        return '\n'.join(chunks)
