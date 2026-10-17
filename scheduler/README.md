# SAIEP Scheduling Layer

Decides what runs, where, when and with how much. Implements the adjusted
"Path 2" design (`ESTIN SAIEP files/Scheduler/apraoch-to-apply-k8s.txt`):
**every execution request is an Argo Workflow**; there is no direct pod
path from JupyterHub (notebook servers keep the simple spawner).

## Request pipeline

```
/hub/jobs (saiep_jobs.py)                      1. Job intake — validate form, own namespace only
  └─ ensure_tenant()  saiep-u-<user>           namespace bundle, applied once, atomically
  └─ advisor.recommend()                       AI hook, 2 s timeout, advisory only (no-op until SAIEP_AI_URL)
  └─ Workflow -> ClusterWorkflowTemplate       saiep-cpu-job | saiep-gpu-job
       │  Argo controller: namespaceParallelism  3. Queue / fair share per user
       ▼
  Pod CREATE ── LimitRange defaults ── saiep-admission /mutate   2. Admission (normalise or deny)
             ── ResourceQuota (over quota -> Argo re-queues)
             ── Pod Security "restricted" ── saiep-admission /validate
       ▼
  kube-scheduler: PriorityClass, nodeSelector gpu=true    4. Placement (native)
       ▼
  Isolator: NetworkPolicy, egress proxy, RuntimeClass     (isolator/)
       ▼
  scheduling-decisions.jsonl  intent + outcome            7. Metrics emitter (AI training data)
```

## Files

| Path | Purpose |
|------|---------|
| `policies/priorityclass.yaml` | `saiep-batch` (500, preempts) / `saiep-background` (100, never preempts) |
| `policies/quota-tiers.yaml` | `phd` / `researcher` / `external` quota + per-container defaults/maxima |
| `tenants/tenant-template.yaml` | Namespace, ResourceQuota, LimitRange, ServiceAccount, RoleBindings |
| `argo/workflows.yaml` | CPU and GPU job blueprints (ClusterWorkflowTemplates) |
| `argo/controller-config.yaml` | Argo fair share (`namespaceParallelism`) and executor sizing |
| `rbac.yaml` | `saiep-runner-executor`, `saiep-tenant`, `saiep-hub` roles; hub ServiceAccount |
| `webhook/app.py` | Admission webhook (stdlib only). Image + priority allowlist, security context, per-tenant UID, GPU placement, isolation tier |
| `webhook/deploy.yaml` | Webhook Deployment/Service + Mutating/Validating configurations (tenant namespaces only) |

Hub-side code: `saiep_scheduler/` (builders, k8s access, advisor, records) and `saiep_jobs.py`.

## Decisions

- One namespace per user (`saiep-u-<user>`), marked `saiep.estin.dz/provisioned=true`
  only after every object applied; a failed first provisioning deletes the namespace.
- The webhook is scoped to namespaces labelled `saiep.estin.dz/tenant=true` and
  fails closed. Platform namespaces are never intercepted.
- Quota is enforced by native `ResourceQuota`; the webhook does not duplicate it.
- A `sandboxed` request without an installed sandbox runtime is denied, never downgraded.
- Policy values live in YAML (GitOps). Code only renders and applies them.

## Deploy and test

```bash
python -m saiep_cli                       # 3: create cluster + stack, 12: hub kubeconfig
./venv/bin/python -m pytest saiep_scheduler scheduler/webhook saiep_cli
```
