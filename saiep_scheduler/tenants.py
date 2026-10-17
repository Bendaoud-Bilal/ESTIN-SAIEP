"""Namespace provisioning: one namespace per user (saiep-u-<user>).

build_tenant_bundle() renders the full policy set for a user from
scheduler/tenants/tenant-template.yaml + isolator/network/networkpolicy.yaml,
using the quota tier from scheduler/policies/quota-tiers.yaml.
"""
import hashlib
import ipaddress
import json
import os
import re
import zlib
from string import Template

import yaml

from . import LABEL_DOMAIN, REPO_DIR

NAMESPACE_PREFIX = 'saiep-u-'
PROVISIONED_LABEL = f'{LABEL_DOMAIN}/provisioned'
# Bump when the bundle gains objects: older namespaces are re-applied once.
BUNDLE_VERSION_LABEL = f'{LABEL_DOMAIN}/bundle-version'
BUNDLE_VERSION = '2'  # 2: MinIO log-archive Secret + MinIO egress
TENANT_LABEL = f'{LABEL_DOMAIN}/tenant'

TENANT_TEMPLATE = os.path.join(REPO_DIR, 'scheduler', 'tenants', 'tenant-template.yaml')
NETWORK_TEMPLATE = os.path.join(REPO_DIR, 'isolator', 'network', 'networkpolicy.yaml')
QUOTA_TIERS = os.path.join(REPO_DIR, 'scheduler', 'policies', 'quota-tiers.yaml')

# Tenant UIDs live in [UID_BASE, UID_BASE + UID_SPAN): far from system and
# image-default users. Must stay in sync with scheduler/webhook/app.py.
UID_BASE = 20000
UID_SPAN = 40000


def namespace_for(username):
    """Deterministic DNS-1123 namespace name for a JupyterHub username.

    Usernames that are not already a clean slug get a short hash suffix so
    two different usernames can never map to the same namespace.
    """
    slug = re.sub(r'[^a-z0-9-]+', '-', username.lower()).strip('-')
    if slug != username or not slug:
        digest = hashlib.sha256(username.encode()).hexdigest()[:8]
        slug = f'{slug[:40]}-{digest}'.strip('-')
    return (NAMESPACE_PREFIX + slug)[:63].rstrip('-')


def tenant_uid(namespace):
    """Per-tenant UID, derived from the namespace name (no API lookup needed)."""
    return UID_BASE + zlib.crc32(namespace.encode()) % UID_SPAN


def load_tiers(path=QUOTA_TIERS):
    with open(path) as f:
        return yaml.safe_load(f)


_UNITS = {'m': 1e-3, 'Ki': 2**10, 'Mi': 2**20, 'Gi': 2**30, 'Ti': 2**40, 'k': 1e3, 'M': 1e6, 'G': 1e9}


def quantity(value):
    """Parse a Kubernetes quantity ('500m', '2', '4Gi') to a float."""
    value = str(value)
    for suffix in sorted(_UNITS, key=len, reverse=True):
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * _UNITS[suffix]
    return float(value)


def check_fits(cpu, memory, gpu, tier=None, tiers=None):
    """Reject at intake what admission would reject later (LimitRange max,
    ResourceQuota ceilings). Returns an error message or None."""
    tiers = tiers or load_tiers()
    tier = tier or tiers['default_tier']
    spec = tiers['tiers'][tier]
    max_cpu, max_mem = spec['container_max']['cpu'], spec['container_max']['memory']
    if quantity(cpu) > quantity(max_cpu):
        return f'palier « {tier} » : {max_cpu} vCPU maximum par calcul'
    if quantity(memory) > quantity(max_mem):
        return f'palier « {tier} » : {max_mem} de mémoire maximum par calcul'
    if int(gpu) > int(spec['quota']['gpu']):
        return f'palier « {tier} » : {spec["quota"]["gpu"]} GPU autorisé(s)'
    return None


def _render(path, values):
    with open(path) as f:
        text = Template(f.read()).substitute(values)
    return [doc for doc in yaml.safe_load_all(text) if doc]


def api_server_cidrs(addresses):
    """Host CIDRs (/32, /128) for API server endpoint addresses."""
    return [str(ipaddress.ip_network(a)) for a in addresses]


def build_network_policies(namespace, api_server_cidrs=('10.96.0.1/32',), api_server_port=443):
    """The isolator's NetworkPolicies for one tenant namespace (invariant I-7:
    content comes only from isolator/network/networkpolicy.yaml)."""
    if not api_server_cidrs:
        raise ValueError('at least one API server endpoint is required')
    peers = [{'ipBlock': {'cidr': cidr}} for cidr in api_server_cidrs]
    return _render(NETWORK_TEMPLATE, {
        'NAMESPACE': namespace,
        'API_SERVER_PEERS': json.dumps(peers),  # JSON is YAML flow syntax
        'API_SERVER_PORT': int(api_server_port),
    })


def build_tenant_bundle(username, tier=None, api_server_cidrs=('10.96.0.1/32',),
                        api_server_port=443, tiers=None, executor_access_key='',
                        executor_secret_key=''):
    """Return the ordered list of objects for a user's namespace."""
    if not (executor_access_key and executor_secret_key):
        raise ValueError('MinIO executor credentials (SAIEP_MINIO_EXECUTOR_*) are required')
    tiers = tiers or load_tiers()
    tier = tier or tiers['default_tier']
    if tier not in tiers['tiers']:
        raise ValueError(f'unknown quota tier: {tier}')
    spec = tiers['tiers'][tier]
    namespace = namespace_for(username)
    values = {
        'NAMESPACE': namespace,
        'USERNAME': username,
        'TIER': tier,
        'UID': tenant_uid(namespace),
        'QUOTA_CPU': spec['quota']['cpu'],
        'QUOTA_MEMORY': spec['quota']['memory'],
        'QUOTA_GPU': spec['quota']['gpu'],
        'QUOTA_PODS': spec['quota']['pods'],
        'DEFAULT_CPU': spec['container_default']['cpu'],
        'DEFAULT_MEMORY': spec['container_default']['memory'],
        'MAX_CPU': spec['container_max']['cpu'],
        'MAX_MEMORY': spec['container_max']['memory'],
        'EXECUTOR_ACCESS_KEY': executor_access_key,
        'EXECUTOR_SECRET_KEY': executor_secret_key,
    }
    return (_render(TENANT_TEMPLATE, values)
            + build_network_policies(namespace, api_server_cidrs, api_server_port))
