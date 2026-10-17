# SAIEP Security Layer — disabled in this version

The cross-cutting Security Layer from the target architecture (Zero Trust,
Kyverno policy engine, organisation-wide RBAC, access control and audit
beyond the platform's own components) is **out of scope for v1** and is not
deployed. The empty `kyverno/` and `rbac/` placeholders were removed.

Still enforced in v1, because the scheduler and isolator need them to
function (not part of this layer):

- Admission webhook `saiep-admission` (image/priority allowlist, restricted
  security context, per-tenant UID) — `scheduler/webhook/`
- Tenant RBAC (`saiep-tenant`, `saiep-runner-executor`) and the hub identity
  confined to `saiep-u-*` by the `saiep-hub-tenant-scope` admission policy —
  `scheduler/rbac.yaml`
- Pod Security `restricted` on tenant namespaces, NetworkPolicy and egress
  proxy — `scheduler/tenants/`, `isolator/`

When this layer is picked up, start from the "Security Layer" box in
`../ESTIN SAIEP files/review and modify based on the advancement/new_arch.md`.
