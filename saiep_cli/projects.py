"""Admin "projects": workloads pinned to a chosen node, imported from GitHub
(raw manifest URL, blob URL or repository) or a local path.

Each project gets its own namespace saiep-p-<name>; only namespaced
workload kinds are accepted (no cluster-scoped objects such as
ClusterRoleBindings from third-party repos). Rendered manifests are kept in
~/.saiep/projects/<name>.yaml so stop/start/uninstall are exact.
"""
import glob
import os
import re
import urllib.request

import yaml

from . import STATE_DIR

ALLOWED_KINDS = {
    'Deployment', 'StatefulSet', 'DaemonSet', 'Job', 'CronJob', 'Pod', 'Service',
    'ConfigMap', 'Secret', 'PersistentVolumeClaim', 'ServiceAccount', 'Ingress',
}
POD_TEMPLATE_PATHS = {
    'Deployment': ('spec', 'template', 'spec'),
    'StatefulSet': ('spec', 'template', 'spec'),
    'DaemonSet': ('spec', 'template', 'spec'),
    'Job': ('spec', 'template', 'spec'),
    'CronJob': ('spec', 'jobTemplate', 'spec', 'template', 'spec'),
    'Pod': ('spec',),
}
SCALABLE = ('Deployment', 'StatefulSet')
_NAME = re.compile(r'^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$')


def namespace_for(project):
    return f'saiep-p-{project}'


def _github_raw(url):
    m = re.match(r'https://github\.com/([^/]+)/([^/]+)/blob/(.+)', url)
    return f'https://raw.githubusercontent.com/{m[1]}/{m[2]}/{m[3]}' if m else url


def load_documents(source, runner, workdir):
    """Return YAML docs from a URL, git repo or local path."""
    texts = []
    if source.startswith(('http://', 'https://')) and source.endswith(('.yaml', '.yml')):
        with urllib.request.urlopen(_github_raw(source), timeout=30) as resp:
            texts.append(resp.read(5 * 1024 * 1024).decode())
    elif source.startswith(('https://', 'git@')):
        runner.run(['rm', '-rf', workdir], check=False)
        runner.run(['git', 'clone', '--depth', '1', source, workdir], capture=False)
        texts += _read_tree(workdir)
    elif os.path.isdir(source):
        texts += _read_tree(source)
    else:
        with open(source) as fh:
            texts.append(fh.read())
    docs = []
    for text in texts:
        docs += [d for d in yaml.safe_load_all(text) if isinstance(d, dict) and d.get('kind')]
    return docs


def _read_tree(root):
    out = []
    for path in sorted(glob.glob(os.path.join(root, '**', '*.y*ml'), recursive=True)):
        if '/.git' in path or '/.github' in path:
            continue
        with open(path) as fh:
            out.append(fh.read())
    return out


def render(project, docs, node):
    """Pin workloads to node, force the project namespace, drop disallowed kinds."""
    ns = namespace_for(project)
    kept, skipped = [{'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {
        'name': ns, 'labels': {'app.kubernetes.io/part-of': 'saiep', 'saiep.estin.dz/project': project}}}], []
    for doc in docs:
        kind = doc['kind']
        if kind not in ALLOWED_KINDS:
            skipped.append(f"{kind}/{doc.get('metadata', {}).get('name', '?')}")
            continue
        doc.setdefault('metadata', {})['namespace'] = ns
        if node and kind in POD_TEMPLATE_PATHS:
            spec = doc
            for key in POD_TEMPLATE_PATHS[kind]:
                spec = spec.setdefault(key, {})
            spec.setdefault('nodeSelector', {})['kubernetes.io/hostname'] = node
        kept.append(doc)
    return kept, skipped


def manifest_path(project):
    return os.path.join(STATE_DIR, 'projects', f'{project}.yaml')


def deploy(runner, state, project, source, node):
    if not _NAME.match(project):
        raise ValueError('nom de projet invalide (a-z, 0-9, -)')
    os.makedirs(os.path.join(STATE_DIR, 'projects'), mode=0o700, exist_ok=True)
    docs = load_documents(source, runner, os.path.join(STATE_DIR, 'projects', f'{project}-src'))
    rendered, skipped = render(project, docs, node)
    path = manifest_path(project)
    with open(path, 'w') as fh:
        yaml.safe_dump_all(rendered, fh, sort_keys=False)
    runner.run(['kubectl', 'apply', '-f', path])
    replicas = {
        f"{d['kind'].lower()}/{d['metadata']['name']}": d.get('spec', {}).get('replicas', 1)
        for d in rendered if d['kind'] in SCALABLE
    }
    state['projects'][project] = {'source': source, 'node': node, 'namespace': namespace_for(project),
                                  'manifest': path, 'replicas': replicas}
    state.save()
    return rendered, skipped


def scale(runner, state, project, up):
    info = state['projects'][project]
    for ref, count in info['replicas'].items():
        runner.run(['kubectl', '-n', info['namespace'], 'scale', ref, f'--replicas={count if up else 0}'])


def uninstall(runner, state, project):
    info = state['projects'].pop(project)
    runner.run(['kubectl', 'delete', '-f', info['manifest'], '--ignore-not-found', '--wait=false'], check=False)
    runner.run(['kubectl', 'delete', 'namespace', info['namespace'], '--ignore-not-found'], check=False)
    for path in (info['manifest'], os.path.join(STATE_DIR, 'projects', f'{project}-src')):
        runner.run(['rm', '-rf', path], check=False)
    state.save()
