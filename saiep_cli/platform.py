"""Deploy the SAIEP Scheduling + Isolator stack onto the current kube context.

Order matters: PriorityClasses/RBAC -> Argo (+fair-share config, artifact
repository) -> job templates -> MinIO store -> isolator (egress proxy,
RuntimeClass) -> admission webhook (last, so platform pods are never blocked by it).
"""
import base64
import json
import os
import secrets
from string import Template

from . import ARGO_VERSION, REPO_DIR, STATE_DIR

ARGO_INSTALL = f'https://github.com/argoproj/argo-workflows/releases/download/{ARGO_VERSION}/install.yaml'
WEBHOOK_IMAGE = os.environ.get('SAIEP_WEBHOOK_IMAGE', 'saiep/admission-webhook:dev')
WEBHOOK_SVC = 'saiep-admission.saiep-system.svc'
EGRESS_PROXY_URL = 'http://saiep-egress-proxy.saiep-egress.svc.cluster.local:3128'
MINIO_NS, MINIO_SVC = 'saiep-storage', 'saiep-minio'


def _p(*parts):
    return os.path.join(REPO_DIR, *parts)


def apply_file(runner, path):
    runner.run(['kubectl', 'apply', '-f', path])


def apply_text(runner, text):
    runner.run(['kubectl', 'apply', '-f', '-'], input=text)


def ensure_webhook_tls(runner, tls_dir=None):
    """Self-signed CA + serving cert for the webhook Service (reused if present)."""
    tls_dir = tls_dir or os.path.join(STATE_DIR, 'tls')
    os.makedirs(tls_dir, mode=0o700, exist_ok=True)
    f = lambda name: os.path.join(tls_dir, name)  # noqa: E731
    if not os.path.exists(f('tls.crt')):
        with open(f('ext.cnf'), 'w') as fh:
            fh.write(f'subjectAltName=DNS:{WEBHOOK_SVC},DNS:{WEBHOOK_SVC}.cluster.local\n')
        runner.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3650',
                    '-keyout', f('ca.key'), '-out', f('ca.crt'), '-subj', '/CN=saiep-admission-ca'])
        runner.run(['openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-keyout', f('tls.key'),
                    '-out', f('tls.csr'), '-subj', f'/CN={WEBHOOK_SVC}'])
        runner.run(['openssl', 'x509', '-req', '-in', f('tls.csr'), '-CA', f('ca.crt'),
                    '-CAkey', f('ca.key'), '-CAcreateserial', '-out', f('tls.crt'), '-days', '825',
                    '-extfile', f('ext.cnf')])
    return tls_dir


def render_webhook(ca_pem, image=WEBHOOK_IMAGE, sandbox_runtime='', egress_proxy=EGRESS_PROXY_URL):
    with open(_p('scheduler', 'webhook', 'deploy.yaml')) as fh:
        return Template(fh.read()).substitute(
            IMAGE=image,
            CA_BUNDLE=base64.b64encode(ca_pem).decode(),
            SANDBOX_RUNTIME=sandbox_runtime,
            EGRESS_PROXY=egress_proxy,
        )


def minio_credentials(state_dir=None):
    """Generated once into STATE_DIR/minio.json (0600) and reused afterwards."""
    path = os.path.join(state_dir or STATE_DIR, 'minio.json')
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    creds = {
        'rootUser': 'saiep-root',
        'rootPassword': secrets.token_urlsafe(32),
        'executorAccessKey': 'argo-executor',
        'executorSecretKey': secrets.token_urlsafe(32),
        'hubAccessKey': 'saiep-hub',
        'hubSecretKey': secrets.token_urlsafe(32),
    }
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(creds, fh, indent=2)
    return creds


def _secret(name, namespace, data):
    # Applied through stdin so secret values never appear in a process argv.
    return json.dumps({'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                       'metadata': {'name': name, 'namespace': namespace}, 'stringData': data})


def deploy_minio(runner):
    creds = minio_credentials()
    runner.run(['kubectl', 'create', 'namespace', MINIO_NS], check=False)
    apply_text(runner, _secret('saiep-minio-root', MINIO_NS,
                               {k: creds[k] for k in ('rootUser', 'rootPassword')}))
    apply_text(runner, _secret('saiep-minio-users', MINIO_NS,
                               {k: v for k, v in creds.items() if not k.startswith('root')}))
    # Jobs are immutable: re-create the (idempotent) bootstrap job each deploy.
    runner.run(['kubectl', '-n', MINIO_NS, 'delete', 'job', 'saiep-minio-setup', '--ignore-not-found'])
    apply_file(runner, _p('scheduler', 'storage', 'minio.yaml'))
    runner.run(['kubectl', '-n', MINIO_NS, 'rollout', 'status', f'deploy/{MINIO_SVC}', '--timeout=300s'])
    runner.run(['kubectl', '-n', MINIO_NS, 'wait', '--for=condition=complete', 'job/saiep-minio-setup',
                '--timeout=300s'])


def deploy_stack(runner, provider, log=print, sandbox_runtime='', with_falco=False):
    log('1/7 Priorités et rôles…')
    apply_file(runner, _p('scheduler', 'policies', 'priorityclass.yaml'))
    apply_file(runner, _p('scheduler', 'rbac.yaml'))

    log(f'2/7 Argo Workflows {ARGO_VERSION}…')
    runner.run(['kubectl', 'create', 'namespace', 'argo'], check=False)
    runner.run(['kubectl', 'apply', '-n', 'argo', '--server-side', '--force-conflicts', '-f', ARGO_INSTALL])
    apply_file(runner, _p('scheduler', 'argo', 'controller-config.yaml'))
    runner.run(['kubectl', '-n', 'argo', 'rollout', 'restart', 'deploy/workflow-controller'])
    runner.run(['kubectl', '-n', 'argo', 'rollout', 'status', 'deploy/workflow-controller', '--timeout=300s'])

    log('3/7 Modèles de calcul (ClusterWorkflowTemplates)…')
    apply_file(runner, _p('scheduler', 'argo', 'workflows.yaml'))

    log('4/7 Stockage MinIO (journaux archivés, décisions)…')
    deploy_minio(runner)

    log('5/7 Isolation : proxy de sortie, RuntimeClass…')
    apply_file(runner, _p('isolator', 'network', 'egress-proxy.yaml'))
    apply_file(runner, _p('isolator', 'runtime', 'runtimeclass.yaml'))

    log("6/7 Webhook d'admission…")
    if WEBHOOK_IMAGE.endswith(':dev'):
        runner.run(['docker', 'build', '-t', WEBHOOK_IMAGE, _p('scheduler', 'webhook')], capture=False)
        provider.load_image(WEBHOOK_IMAGE)
    tls = ensure_webhook_tls(runner)
    secret = runner.run(['kubectl', '-n', 'saiep-system', 'create', 'secret', 'tls', 'saiep-admission-tls',
                         f'--cert={tls}/tls.crt', f'--key={tls}/tls.key', '--dry-run=client', '-o', 'yaml'])
    apply_text(runner, secret)
    ca_pem = b''
    if not runner.dry_run:
        with open(os.path.join(tls, 'ca.crt'), 'rb') as fh:
            ca_pem = fh.read()
    apply_text(runner, render_webhook(ca_pem, sandbox_runtime=sandbox_runtime))
    runner.run(['kubectl', '-n', 'saiep-system', 'rollout', 'restart', 'deploy/saiep-admission'])
    runner.run(['kubectl', '-n', 'saiep-system', 'rollout', 'status', 'deploy/saiep-admission', '--timeout=180s'])
    runner.run(['kubectl', '-n', 'saiep-egress', 'rollout', 'status', 'deploy/saiep-egress-proxy', '--timeout=180s'])

    if with_falco:
        log('7/7 Falco (détection à l\'exécution)…')
        runner.run(['helm', 'repo', 'add', 'falcosecurity', 'https://falcosecurity.github.io/charts'], check=False)
        runner.run(['helm', 'repo', 'update'])
        runner.run(['helm', 'upgrade', '--install', 'falco', 'falcosecurity/falco', '-n', 'falco',
                    '--create-namespace', '-f', _p('isolator', 'security', 'falco.yaml')], capture=False)
    else:
        log('7/7 Falco ignoré (optionnel).')


def api_server_endpoint(runner):
    out = runner.run(['kubectl', 'get', 'endpointslice', '-n', 'default',
                      '-l', 'kubernetes.io/service-name=kubernetes', '-o',
                      'jsonpath={.items[0].endpoints[0].addresses[0]} {.items[0].ports[0].port}'])
    ip, _, port = out.strip().partition(' ')
    return ip, port or '6443'


def write_hub_kubeconfig(runner, path=None):
    """kubeconfig for the saiep-hub ServiceAccount, + env lines for secrets.env."""
    path = path or os.path.join(STATE_DIR, 'hub-kubeconfig')
    secret = json.loads(runner.run(['kubectl', '-n', 'saiep-system', 'get', 'secret',
                                    'saiep-hub-token', '-o', 'json']))
    token = base64.b64decode(secret['data']['token']).decode()
    ca = secret['data']['ca.crt']
    server = runner.run(['kubectl', 'config', 'view', '--minify', '-o',
                         'jsonpath={.clusters[0].cluster.server}']).strip()
    config = {
        'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'saiep-hub',
        'clusters': [{'name': 'saiep', 'cluster': {'server': server, 'certificate-authority-data': ca}}],
        'users': [{'name': 'saiep-hub', 'user': {'token': token}}],
        'contexts': [{'name': 'saiep-hub', 'context': {'cluster': 'saiep', 'user': 'saiep-hub'}}],
    }
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(config, fh, indent=2)
    ip, port = api_server_endpoint(runner)
    return path, [
        f'export SAIEP_KUBECONFIG={path}',
        f'export SAIEP_API_SERVER_CIDR={ip}/32',
        f'export SAIEP_API_SERVER_PORT={port}',
    ] + minio_env_lines(runner)


def minio_env_lines(runner):
    """Hub-side MinIO settings. The ClusterIP is reachable from a kubeadm node
    (hub on the control plane); with kind on macOS use a port-forward instead:
    kubectl -n saiep-storage port-forward svc/saiep-minio 9000:9000"""
    creds = minio_credentials()
    cluster_ip = runner.run(['kubectl', '-n', MINIO_NS, 'get', 'svc', MINIO_SVC, '-o',
                             'jsonpath={.spec.clusterIP}']).strip()
    return [
        f'export SAIEP_MINIO_ENDPOINT=http://{cluster_ip or "127.0.0.1"}:9000',
        f"export SAIEP_MINIO_ACCESS_KEY={creds['hubAccessKey']}",
        f"export SAIEP_MINIO_SECRET_KEY={creds['hubSecretKey']}",
        f"export SAIEP_MINIO_EXECUTOR_ACCESS_KEY={creds['executorAccessKey']}",
        f"export SAIEP_MINIO_EXECUTOR_SECRET_KEY={creds['executorSecretKey']}",
    ]
