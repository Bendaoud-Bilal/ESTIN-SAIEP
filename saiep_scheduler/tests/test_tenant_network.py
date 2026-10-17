"""Tenant egress to the API server follows the live endpoints (control-plane re-IP)."""
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from saiep_scheduler import k8s, tenants

NS = 'saiep-u-alice'


def _api_rule(policies):
    egress = next(p for p in policies if p['metadata']['name'] == 'saiep-allow-dns-and-proxy')
    return egress['spec']['egress'][-1]


def test_api_server_cidrs_are_host_routes():
    assert tenants.api_server_cidrs(['172.23.0.5', '10.0.0.1/32', 'fd00::1']) == [
        '172.23.0.5/32', '10.0.0.1/32', 'fd00::1/128']


def test_network_policies_allow_every_api_server_endpoint():
    policies = tenants.build_network_policies(NS, ['10.0.0.1/32', '10.0.0.2/32'], 6443)
    assert [p['metadata']['name'] for p in policies] == [
        'saiep-default-deny', 'saiep-allow-same-namespace', 'saiep-allow-dns-and-proxy']
    assert all(p['kind'] == 'NetworkPolicy' and p['metadata']['namespace'] == NS for p in policies)
    rule = _api_rule(policies)
    assert rule['to'] == [{'ipBlock': {'cidr': '10.0.0.1/32'}}, {'ipBlock': {'cidr': '10.0.0.2/32'}}]
    assert rule['ports'] == [{'protocol': 'TCP', 'port': 6443}]


def test_network_policies_need_an_endpoint():
    with pytest.raises(ValueError):
        tenants.build_network_policies(NS, [], 6443)


def test_bundle_network_policies_match_standalone_render():
    bundle = tenants.build_tenant_bundle('alice', api_server_cidrs=['172.23.0.5/32'], api_server_port=6443,
                                         executor_access_key='a', executor_secret_key='b')
    assert [o for o in bundle if o['kind'] == 'NetworkPolicy'] == \
        tenants.build_network_policies(NS, ['172.23.0.5/32'], 6443)


def _slice(*ips, port=6443):
    return SimpleNamespace(endpoints=[SimpleNamespace(addresses=list(ips))],
                           ports=[SimpleNamespace(port=port)])


def _cluster(endpoint_slice, provisioned=True):
    c = object.__new__(k8s.Cluster)
    c.applied = []
    c._apply = c.applied.append
    c.is_provisioned = lambda ns: provisioned

    def read(name, namespace):
        assert (name, namespace) == ('kubernetes', 'default')
        if isinstance(endpoint_slice, Exception):
            raise endpoint_slice
        return endpoint_slice
    c.discovery = SimpleNamespace(read_namespaced_endpoint_slice=read)
    return c


def test_provisioned_tenant_gets_policies_with_current_endpoint(monkeypatch):
    """Namespace created when the API server was 172.23.0.2; it moved to .5."""
    monkeypatch.setenv('SAIEP_API_SERVER_CIDR', '172.23.0.2/32')  # stale hub env
    c = _cluster(_slice('172.23.0.5'))
    assert c.ensure_tenant('alice') == NS
    assert [o['kind'] for o in c.applied] == ['NetworkPolicy'] * 3  # nothing else touched
    assert _api_rule(c.applied)['to'] == [{'ipBlock': {'cidr': '172.23.0.5/32'}}]


def test_endpoints_cover_ha_control_plane():
    c = _cluster(SimpleNamespace(
        endpoints=[SimpleNamespace(addresses=['10.0.0.1']), SimpleNamespace(addresses=['10.0.0.2'])],
        ports=[SimpleNamespace(port=6443)]))
    assert c.api_server_endpoints() == (['10.0.0.1/32', '10.0.0.2/32'], 6443)


def test_endpoints_fall_back_to_env_when_slice_unreadable(monkeypatch):
    monkeypatch.setenv('SAIEP_API_SERVER_CIDR', '172.18.0.2/32, 172.18.0.3/32')
    monkeypatch.setenv('SAIEP_API_SERVER_PORT', '6444')
    c = _cluster(ApiException(status=403))
    assert c.api_server_endpoints() == (['172.18.0.2/32', '172.18.0.3/32'], 6444)
