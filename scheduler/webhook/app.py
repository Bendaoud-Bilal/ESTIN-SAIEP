"""SAIEP admission webhook — scheduling normalisation gate (arch v2 §2.5, §2.7.2).

Runs on every Pod CREATE in tenant namespaces (label saiep.estin.dz/tenant=true),
whatever submitted it (Argo, kubectl, ...).

/mutate   normalises the pod: security context, per-tenant UID, writable /tmp,
          resource defaults, GPU node placement, isolation tier label ->
          RuntimeClass, egress proxy env. Denies non-compliant pods.
/validate re-checks the final pod after all mutations (defence in depth).

Quota is NOT re-implemented here: the native ResourceQuota admission rejects
over-quota pods and Argo re-queues them. Stdlib only (no third-party deps).
Every decision is logged as one JSON line on stdout (audit trail).
"""
import base64
import copy
import json
import os
import ssl
import sys
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LABEL = 'saiep.estin.dz'
ISOLATION_LABEL = f'{LABEL}/isolation-tier'
UID_BASE, UID_SPAN = 20000, 40000  # keep in sync with saiep_scheduler/tenants.py
GPU_RESOURCE = 'nvidia.com/gpu'
TMP_VOLUME = 'saiep-tmp'


class Policy:
    """Policy bundle, read from env (set by the Deployment / GitOps)."""

    def __init__(self, env=None):
        env = env if env is not None else os.environ
        get = lambda k, d: env.get(k, d)  # noqa: E731
        split = lambda k, d: [x.strip() for x in get(k, d).split(',') if x.strip()]  # noqa: E731
        self.image_allowlist = split(
            'SAIEP_IMAGE_ALLOWLIST',
            'docker.io/library/python:,docker.io/library/busybox:,'
            'docker.io/pytorch/pytorch:,docker.io/tensorflow/tensorflow:,'
            'quay.io/argoproj/argoexec:',
        )
        self.priority_classes = set(split('SAIEP_PRIORITY_CLASSES', 'saiep-batch,saiep-background'))
        self.default_cpu = get('SAIEP_DEFAULT_CPU', '500m')
        self.default_memory = get('SAIEP_DEFAULT_MEMORY', '512Mi')
        self.sandbox_runtime = get('SAIEP_SANDBOX_RUNTIME', '')  # e.g. gvisor; empty = unavailable
        self.egress_proxy = get('SAIEP_EGRESS_PROXY', '')  # e.g. http://saiep-egress-proxy.saiep-egress:3128
        self.gpu_node_label = get('SAIEP_GPU_NODE_LABEL', 'gpu=true')


def normalize_image(image):
    """Fully qualify an image reference: python:3.12 -> docker.io/library/python:3.12."""
    name = image.split('@')[0]
    first = name.split('/')[0]
    has_registry = '/' in name and ('.' in first or ':' in first or first == 'localhost')
    if not has_registry:
        image = ('docker.io/library/' if '/' not in name else 'docker.io/') + image
    if ':' not in image.rsplit('/', 1)[-1] and '@' not in image:
        image += ':latest'
    return image


def image_allowed(image, allowlist):
    full = normalize_image(image)
    return any(full.startswith(prefix) for prefix in allowlist)


def tenant_uid(namespace):
    return UID_BASE + zlib.crc32(namespace.encode()) % UID_SPAN


def _containers(spec):
    for key in ('initContainers', 'containers', 'ephemeralContainers'):
        for c in spec.get(key) or []:
            yield key, c


def violations(pod, policy):
    """Policy checks that cannot be fixed by mutation -> deny."""
    spec = pod.get('spec', {})
    errs = []
    for flag in ('hostNetwork', 'hostPID', 'hostIPC'):
        if spec.get(flag):
            errs.append(f'{flag} is not allowed')
    for vol in spec.get('volumes') or []:
        if 'hostPath' in vol:
            errs.append(f'hostPath volume {vol.get("name")!r} is not allowed')
    for _, c in _containers(spec):
        if not image_allowed(c.get('image', ''), policy.image_allowlist):
            errs.append(f'image {c.get("image")!r} is not on the SAIEP allowlist')
        if (c.get('securityContext') or {}).get('privileged'):
            errs.append(f'container {c.get("name")!r} requests privileged mode')
    pc = spec.get('priorityClassName')
    if pc not in policy.priority_classes:
        errs.append(f'priorityClassName {pc!r} not allowed (use one of {sorted(policy.priority_classes)})')
    tier = (pod.get('metadata', {}).get('labels') or {}).get(ISOLATION_LABEL, 'standard')
    if tier not in ('standard', 'sandboxed'):
        errs.append(f'unknown isolation tier {tier!r}')
    elif tier == 'sandboxed' and not policy.sandbox_runtime:
        errs.append('sandboxed isolation requested but no sandbox runtime is installed')
    return errs


def mutate(pod, namespace, policy):
    """Return the normalised pod (input is not modified)."""
    pod = copy.deepcopy(pod)
    meta = pod.setdefault('metadata', {})
    labels = meta.setdefault('labels', {}) or {}
    meta['labels'] = labels
    labels.setdefault(ISOLATION_LABEL, 'standard')
    spec = pod.setdefault('spec', {})
    uid = tenant_uid(namespace)

    # Pod-level security context: policy values win over anything requested.
    psc = spec.get('securityContext') or {}
    psc.update({
        'runAsNonRoot': True,
        'runAsUser': uid,
        'runAsGroup': uid,
        'fsGroup': uid,
        'seccompProfile': {'type': 'RuntimeDefault'},
    })
    spec['securityContext'] = psc

    volumes = spec.get('volumes') or []
    if not any(v.get('name') == TMP_VOLUME for v in volumes):
        volumes.append({'name': TMP_VOLUME, 'emptyDir': {}})
    spec['volumes'] = volumes

    wants_gpu = False
    for kind, c in _containers(spec):
        c['securityContext'] = {
            'runAsNonRoot': True,
            'runAsUser': uid,
            'runAsGroup': uid,
            'allowPrivilegeEscalation': False,
            'privileged': False,
            'readOnlyRootFilesystem': True,
            'capabilities': {'drop': ['ALL']},
            'seccompProfile': {'type': 'RuntimeDefault'},
        }
        if kind == 'ephemeralContainers':
            continue
        mounts = c.get('volumeMounts') or []
        if not any(m.get('mountPath') == '/tmp' for m in mounts):
            mounts.append({'name': TMP_VOLUME, 'mountPath': '/tmp'})
        c['volumeMounts'] = mounts
        env = c.get('env') or []
        names = {e.get('name') for e in env}
        if 'HOME' not in names:
            env.append({'name': 'HOME', 'value': '/tmp'})
        if policy.egress_proxy:
            for var in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
                if var not in names:
                    env.append({'name': var, 'value': policy.egress_proxy})
            if 'NO_PROXY' not in names:
                env.append({'name': 'NO_PROXY', 'value': '.svc,.cluster.local,10.0.0.0/8,172.16.0.0/12'})
        c['env'] = env
        res = c.get('resources') or {}
        for section in ('requests', 'limits'):
            vals = res.get(section) or {}
            vals.setdefault('cpu', policy.default_cpu)
            vals.setdefault('memory', policy.default_memory)
            res[section] = vals
        c['resources'] = res
        if GPU_RESOURCE in res.get('limits', {}) or GPU_RESOURCE in res.get('requests', {}):
            wants_gpu = True

    if wants_gpu:
        key, _, value = policy.gpu_node_label.partition('=')
        selector = spec.get('nodeSelector') or {}
        selector[key] = value
        spec['nodeSelector'] = selector
        tolerations = spec.get('tolerations') or []
        tol = {'key': key, 'operator': 'Equal', 'value': value, 'effect': 'NoSchedule'}
        if tol not in tolerations:
            tolerations.append(tol)
        spec['tolerations'] = tolerations

    if labels[ISOLATION_LABEL] == 'sandboxed' and policy.sandbox_runtime:
        spec['runtimeClassName'] = policy.sandbox_runtime
    return pod


def review(admission, policy, mode):
    """Build an AdmissionReview response for mode 'mutate' or 'validate'."""
    req = admission.get('request') or {}
    uid = req.get('uid', '')
    pod = req.get('object') or {}
    namespace = req.get('namespace') or pod.get('metadata', {}).get('namespace', '')
    response = {'uid': uid, 'allowed': True}
    if req.get('kind', {}).get('kind') == 'Pod' and req.get('operation') == 'CREATE':
        # Check the object as submitted: normalisation must never launder a
        # forbidden request (e.g. privileged=true) into an allowed one.
        errs = violations(pod, policy)
        candidate = mutate(pod, namespace, policy) if mode == 'mutate' else pod
        if mode == 'validate' and not errs:
            # The final object must already carry the normalised fields.
            if mutate(pod, namespace, policy)['spec'] != pod.get('spec'):
                errs.append('pod spec was modified after SAIEP normalisation')
        if errs:
            response.update(allowed=False, status={'code': 403, 'message': '; '.join(errs)})
        elif mode == 'mutate':
            patch = [
                {'op': 'replace' if 'metadata' in pod and 'labels' in pod['metadata'] else 'add',
                 'path': '/metadata/labels', 'value': candidate['metadata']['labels']},
                {'op': 'replace', 'path': '/spec', 'value': candidate['spec']},
            ]
            response['patchType'] = 'JSONPatch'
            response['patch'] = base64.b64encode(json.dumps(patch).encode()).decode()
        audit(mode, namespace, pod, response, errs)
    return {'apiVersion': 'admission.k8s.io/v1', 'kind': 'AdmissionReview', 'response': response}


def audit(mode, namespace, pod, response, errs):
    meta = pod.get('metadata', {})
    print(json.dumps({
        'ts': datetime.now(timezone.utc).isoformat(),
        'component': 'saiep-admission',
        'mode': mode,
        'namespace': namespace,
        'pod': meta.get('name') or meta.get('generateName'),
        'workflow': (meta.get('labels') or {}).get('workflows.argoproj.io/workflow'),
        'allowed': response['allowed'],
        'reasons': errs,
    }), flush=True)


def route(path):
    """'/mutate?timeout=5s' -> 'mutate' (the API server appends a timeout query)."""
    return urlsplit(path).path.strip('/')


class Handler(BaseHTTPRequestHandler):
    policy = None

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if route(self.path) == 'healthz':
            self._send(200, {'ok': True})
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        mode = route(self.path)
        if mode not in ('mutate', 'validate'):
            self._send(404, {'error': 'not found'})
            return
        length = int(self.headers.get('Content-Length', 0))
        if length > 3 * 1024 * 1024:
            self._send(413, {'error': 'too large'})
            return
        try:
            admission = json.loads(self.rfile.read(length))
        except ValueError:
            self._send(400, {'error': 'invalid json'})
            return
        self._send(200, review(admission, self.policy, mode))

    def log_message(self, fmt, *args):  # access log -> stderr, audit stays on stdout
        sys.stderr.write(fmt % args + '\n')


def main():
    Handler.policy = Policy()
    port = int(os.environ.get('PORT', '8443'))
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    cert = os.environ.get('TLS_CERT', '/tls/tls.crt')
    key = os.environ.get('TLS_KEY', '/tls/tls.key')
    if os.path.exists(cert):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(json.dumps({'component': 'saiep-admission', 'event': 'start', 'port': port}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
