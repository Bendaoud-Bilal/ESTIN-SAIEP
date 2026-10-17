# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

ESTIN-SAIEP is a **fork of JupyterHub** (upstream `jupyterhub/` package, `jsx/` admin UI, `share/`, `docs/`, `ci/` are mostly unmodified upstream) with a SAIEP layer on top: a secure, scheduled AI/compute platform for ESTIN researchers and students. Most SAIEP-specific work lives in the repo root and in `scheduler/`, `isolator/`, `monitoring/`, `static/`, `templates/` — not in the `jupyterhub/` package. Avoid editing `jupyterhub/` unless the change truly belongs in the core.

## Commands

Requirements: Python 3.12, Node 20, Docker + kind/kubectl for the cluster layers, `configurable-http-proxy` (`npm i -g`).

```bash
python3 -m venv venv && source venv/bin/activate
pip install -e .
pip install jupyterlab notebook Pillow jupyterhub-nativeauthenticator docker kubernetes rich pyyaml
bash setup_ui.sh                              # SAIEP branding patch (favicons, JupyterLab CSS/title) — rerun after reinstalling venv
```

- Run the hub (production-like): `./start-hub.sh` — sources `secrets.env` (copy from `secrets.env.example`; `SAIEP_PROMETHEUS_TOKEN` is **required**, config raises `KeyError` without it) then `exec ./venv/bin/jupyterhub -f jupyterhub_config.py`.
- Run the hub for generic upstream testing (no admin privileges, dummy auth): `jupyterhub -f testing/jupyterhub_config.py`.
- Python tests (upstream suite): `pytest jupyterhub/tests` · single test: `pytest jupyterhub/tests/test_api.py::test_name` · browser tests (excluded by default via `-m 'not browser'`): `pytest -m browser`. `pytest.ini` runs asyncio tests in auto mode.
- JS tests: `npm test` (karma) · admin React UI (`jsx/`): `npm run jsx:install-run <script>` (e.g. `test`, `build`, `lint`).
- SCSS: `npm run css` / `npm run build:watch`.
- Docs: `nox -s docs` or `make -C docs html`.
- Lint/format: `pre-commit run --all-files` (`.pre-commit-config.yaml`).

- SAIEP tests: `./venv/bin/python -m pytest saiep_scheduler scheduler/webhook saiep_cli` (no cluster needed). `saiep_admin.py`/`saiep_jobs.py` handlers have no unit tests.
- Cluster console: `python -m saiep_cli` (Rich menu; `--dry-run` prints commands). Option 3 creates a kind (dev) or kubeadm cluster and deploys the SAIEP stack; 12 writes the hub kubeconfig + env lines for `secrets.env`. State, TLS certs and kubeconfig live in `~/.saiep` (`SAIEP_HOME`).
- Local hub against that cluster (dummy auth, jobs page): `SAIEP_DEV_PASSWORD=… SAIEP_KUBECONFIG=~/.saiep/hub-kubeconfig ./venv/bin/jupyterhub -f testing/jupyterhub_config_saiep.py`.
- Webhook image (dev): `docker build -t saiep/admission-webhook:dev scheduler/webhook && kind load docker-image saiep/admission-webhook:dev --name saiep`, then `kubectl -n saiep-system rollout restart deploy/saiep-admission`.

## SAIEP architecture

**Hub config (`jupyterhub_config.py`, ~220 lines)** is the integration point and is deployment-specific (hard-coded `PUBLIC_URL`/IP `104.248.22.193`, bind `172.17.0.1:8000`, admins `r4y4n3` and `litan`). Key behaviors:
- Auth: `SAIEPAuthenticator` (NativeAuthenticator subclass). Signup is request-only (`open_signup=False`); admin `litan` approves at `/hub/authorize`. `SAIEPSignUpHandler` appends researcher profile fields to `researcher-profiles.jsonl`.
- Post-login routing by role via `default_url`: `r4y4n3` → `/hub/admin`, `litan` → `/hub/authorize`, others → `/hub/spawn`; anonymous → `/hub/landing` (serves `static/landing/index.html`).
- Spawner: `simple` spawner plus a sizing form (CPU/RAM/duration/usage). `pre_spawn_hook` logs each request to `spawn-requests.jsonl` — this is the **training dataset for the future AI sizing model**, and the limits are only passed as env vars (`SAIEP_CPU_REQUEST`, `SAIEP_MEM_REQUEST`), not enforced yet.
- Monitoring: a `prometheus` service token with the `read:metrics` role scrapes `/hub/metrics`; Prometheus stays on `127.0.0.1:9090` (see `monitoring/prometheus/prometheus.yml.example`).
- TLS: `Caddyfile` terminates HTTPS (sslip.io name, TLS-ALPN-01 on 443 only because port 80 is taken) and reverse-proxies to the hub; the hub trusts `X-Forwarded-*` from the Docker bridge.

**`saiep_admin.py`** is the native admin UI that replaced Portainer and Grafana (do not reintroduce third-party tools). It registers Tornado handlers via `resource_handlers()`: `/hub/swarm` (single-page UI from `static/admin/index.html`), `/hub/swarm/api/<section>`, `/hub/swarm/action` (Docker/Swarm container, volume, network, node actions through the `docker` SDK), and `/hub/monitoring` (+ `/api/metrics`, queried from Prometheus). Every handler requires a JupyterHub admin; blocking Docker calls go through `_run()` (executor) — keep them off the event loop.

**Branding/UI:** `templates/` overrides JupyterHub Jinja templates (loaded via `template_paths`); `static/` holds the landing page and admin SPA. UI copy is French.

## Scheduling + Isolator layers (Kubernetes)

Design sources live outside the repo in `../ESTIN SAIEP files/`. The adjusted, authoritative scheduler design is `Scheduler/apraoch-to-apply-k8s.txt` ("Path 2", batch-only); it wins over `scheduler_architecture_v2.txt` where they conflict. Files with "not to work with" in the name are obsolete. See `scheduler/README.md` and `isolator/README.md`.

Request path: `/hub/jobs` (`saiep_jobs.py`) → `saiep_scheduler` provisions `saiep-u-<user>` (one namespace per user) → creates an Argo `Workflow` referencing ClusterWorkflowTemplate `saiep-cpu-job`/`saiep-gpu-job` → Argo `namespaceParallelism` queues per user → pod admission (LimitRange, `saiep-admission` mutate/validate webhook, ResourceQuota, Pod Security `restricted`) → kube-scheduler (PriorityClass, `gpu=true` nodes) → isolator (NetworkPolicy, Squid egress proxy, optional gVisor) → `scheduling-decisions.jsonl` (intent/outcome records = AI training data, also shipped one-object-per-record to MinIO `saiep-records`). Argo archives job logs to MinIO `saiep-artifacts` (`scheduler/storage/minio.yaml`); `/hub/jobs` falls back to them after pod GC.

Invariants to preserve:
- Every job is an Argo Workflow; the hub never creates bare pods. Notebook servers stay on the simple spawner.
- Policy values live in YAML (`scheduler/policies/*.yaml`, `scheduler/tenants/tenant-template.yaml`, `isolator/network/networkpolicy.yaml`); Python only renders/applies them. Templates use `string.Template`, so a literal `${` in those YAML files breaks rendering.
- Tenant namespaces get `saiep.estin.dz/provisioned=true` only after every object applied; failed first provisioning deletes the namespace.
- The webhook (`scheduler/webhook/app.py`, stdlib only) only intercepts namespaces labelled `saiep.estin.dz/tenant=true`, fails closed, checks violations on the object *as submitted* (never launder e.g. `privileged`), and must stay idempotent (`/validate` compares against `mutate(pod)`). `UID_BASE/UID_SPAN` are duplicated in `saiep_scheduler/tenants.py` — keep in sync.
- `sandboxed` isolation is denied unless `SAIEP_SANDBOX_RUNTIME` is set — never downgraded.
- AI advisor (`saiep_scheduler/advisor.py`) is advisory only, 2 s timeout, no-op without `SAIEP_AI_URL`.
- Argo v4 CRDs reject templated values inside `container.resources`: sizing goes through `podSpecPatch`.
- Archived-log keys come from WorkflowTaskResults (writable by the tenant pod): the hub only fetches keys under `<namespace>/<workflow>/` in `saiep-artifacts`.
- Tenant NetworkPolicies come only from `isolator/network/networkpolicy.yaml` (via `tenants.build_network_policies`, I-7). The API server egress rule uses the live `default/kubernetes` EndpointSlice and is re-applied on every `ensure_tenant()` and by CLI "Redéployer la pile SAIEP" — never pin it to a fixed IP.
- Adding objects to the tenant bundle: bump `tenants.BUNDLE_VERSION` so existing namespaces re-apply it.
- MinIO: upstream images are gone; Chainguard images pinned by digest. Hub env: `SAIEP_MINIO_ENDPOINT`, `SAIEP_MINIO_ACCESS_KEY/SECRET_KEY` (hub user), `SAIEP_MINIO_EXECUTOR_ACCESS_KEY/SECRET_KEY` (required to provision tenants), printed by CLI option 12.

Security Layer (Kyverno, organisation RBAC, Zero Trust) is **disabled for this version** — see `security/README.md`; the scheduler's own guards (admission webhook, tenant RBAC, `saiep-hub-tenant-scope` policy) stay on.

Not implemented: ArgoCD GitOps, HPA/VPA, Falco install verification, kubeadm path end-to-end (only kind was tested).

## Conventions and gotchas

- `secrets.env`, `researcher-profiles.jsonl`, `spawn-requests.jsonl`, `scheduling-decisions.jsonl` and `venv/` are gitignored; never commit them.
- `setup_ui.sh` patches files inside `venv/` and `share/` with `sed` (creates `index.html.bak`); it assumes `venv/lib/python3.12`.
- Recent history is PR-merge style (`reconcile: native SAIEP interfaces…`, `Merge PR #2: SAIEP production deploy`); upstream JupyterHub files should stay close to upstream to ease merges.
