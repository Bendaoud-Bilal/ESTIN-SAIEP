# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

ESTIN-SAIEP is a **fork of JupyterHub** (upstream `jupyterhub/` package, `jsx/` admin UI, `share/`, `docs/`, `ci/` are mostly unmodified upstream) with a SAIEP layer on top: a secure, scheduled AI/compute platform for ESTIN researchers and students. Most SAIEP-specific work lives in the repo root and in `scheduler/`, `isolator/`, `security/`, `monitoring/`, `static/`, `templates/` — not in the `jupyterhub/` package. Avoid editing `jupyterhub/` unless the change truly belongs in the core.

## Commands

Requirements: Python 3.12, Node 20, `configurable-http-proxy` (`npm i -g`).

```bash
python3 -m venv venv && source venv/bin/activate
pip install -e .
pip install jupyterlab notebook Pillow        # plus nativeauthenticator, docker (imported by the SAIEP config)
bash setup_ui.sh                              # SAIEP branding patch (favicons, JupyterLab CSS/title) — rerun after reinstalling venv
```

- Run the hub (production-like): `./start-hub.sh` — sources `secrets.env` (copy from `secrets.env.example`; `SAIEP_PROMETHEUS_TOKEN` is **required**, config raises `KeyError` without it) then `exec ./venv/bin/jupyterhub -f jupyterhub_config.py`.
- Run the hub for generic upstream testing (no admin privileges, dummy auth): `jupyterhub -f testing/jupyterhub_config.py`.
- Python tests (upstream suite): `pytest jupyterhub/tests` · single test: `pytest jupyterhub/tests/test_api.py::test_name` · browser tests (excluded by default via `-m 'not browser'`): `pytest -m browser`. `pytest.ini` runs asyncio tests in auto mode.
- JS tests: `npm test` (karma) · admin React UI (`jsx/`): `npm run jsx:install-run <script>` (e.g. `test`, `build`, `lint`).
- SCSS: `npm run css` / `npm run build:watch`.
- Docs: `nox -s docs` or `make -C docs html`.
- Lint/format: `pre-commit run --all-files` (`.pre-commit-config.yaml`).

There are no tests for the SAIEP layer (`saiep_admin.py`, `jupyterhub_config.py`).

## SAIEP architecture

**Hub config (`jupyterhub_config.py`, ~220 lines)** is the integration point and is deployment-specific (hard-coded `PUBLIC_URL`/IP `104.248.22.193`, bind `172.17.0.1:8000`, admins `r4y4n3` and `litan`). Key behaviors:
- Auth: `SAIEPAuthenticator` (NativeAuthenticator subclass). Signup is request-only (`open_signup=False`); admin `litan` approves at `/hub/authorize`. `SAIEPSignUpHandler` appends researcher profile fields to `researcher-profiles.jsonl`.
- Post-login routing by role via `default_url`: `r4y4n3` → `/hub/admin`, `litan` → `/hub/authorize`, others → `/hub/spawn`; anonymous → `/hub/landing` (serves `static/landing/index.html`).
- Spawner: `simple` spawner plus a sizing form (CPU/RAM/duration/usage). `pre_spawn_hook` logs each request to `spawn-requests.jsonl` — this is the **training dataset for the future AI sizing model**, and the limits are only passed as env vars (`SAIEP_CPU_REQUEST`, `SAIEP_MEM_REQUEST`), not enforced yet.
- Monitoring: a `prometheus` service token with the `read:metrics` role scrapes `/hub/metrics`; Prometheus stays on `127.0.0.1:9090` (see `monitoring/prometheus/prometheus.yml.example`).
- TLS: `Caddyfile` terminates HTTPS (sslip.io name, TLS-ALPN-01 on 443 only because port 80 is taken) and reverse-proxies to the hub; the hub trusts `X-Forwarded-*` from the Docker bridge.

**`saiep_admin.py`** is the native admin UI that replaced Portainer and Grafana (do not reintroduce third-party tools). It registers Tornado handlers via `resource_handlers()`: `/hub/swarm` (single-page UI from `static/admin/index.html`), `/hub/swarm/api/<section>`, `/hub/swarm/action` (Docker/Swarm container, volume, network, node actions through the `docker` SDK), and `/hub/monitoring` (+ `/api/metrics`, queried from Prometheus). Every handler requires a JupyterHub admin; blocking Docker calls go through `_run()` (executor) — keep them off the event loop.

**Branding/UI:** `templates/` overrides JupyterHub Jinja templates (loaded via `template_paths`); `static/` holds the landing page and admin SPA. UI copy is French.

## Target architecture (design docs, mostly not implemented)

Design sources live outside the repo in `../ESTIN SAIEP files/` (`review and modify based on the advancement/new_arch.md`, `Scheduler/scheduler_architecture_v2.txt`, `Scheduler/scheduler_tech_study_v2.txt`, `Scheduler/apraoch-to-apply-k8s.txt`). Files with "not to work with" in the name are obsolete. Layer pipeline: **User Interface → Access Gateway → Scheduler → Isolator**, wrapped by a Security Layer and Monitoring/AI-Optimization/Storage.

Scheduler decisions (latest, batch-only "Path 2"):
- **Every execution request enters through Argo Workflows** (WorkflowTemplates for CPU/GPU jobs; plain stateless Pods/Jobs, no StatefulSets). No KubeSpawner / JupyterHub-direct interactive pod path.
- Admission happens **before** scheduling: custom validating/mutating webhook (quota check, image allowlist, LimitRange default injection) + Kyverno policies.
- Priority/queueing = `PriorityClass` (Batch 500, Background 100) + Argo namespace parallelism; Services are for networking only, never scheduling.
- Placement = native kube-scheduler; the AI optimization module is an optional advisory step (2 s timeout) and must be a no-op stub until a model exists.
- One namespace per user or project (undecided — decide once), each with `ResourceQuota` + `LimitRange`.
- Isolation is the Isolator's job: scheduler only passes a label; `RuntimeClass` (gVisor/Kata) + per-namespace `NetworkPolicy` + Falco enforce it.
- Cluster: single kubeadm cluster; a machine = a node; GPU capability is a label (`gpu=true`) applied at join. ArgoCD (GitOps) delivers all of the above YAML.
- A planned product CLI (`../ESTIN SAIEP files/cli_support.txt`): ASCII-banner + Rich interactive menu for installing/verifying k8s, creating/running/stopping the cluster and node containers, and uninstalling.

**Current state of the scaffolding:** the files `scheduler/webhook/app.py`, `scheduler/argo/workflows.yaml`, `scheduler/policies/priorityclass.yaml`, `security/kyverno/policies.yaml`, `security/rbac/roles.yaml`, `isolator/**/*.yaml` and `monitoring/prometheus/servicemonitor.yaml` are **empty placeholders**. The running system today is JupyterHub + simple spawner + Docker/Swarm admin UI; the Kubernetes scheduler design is not wired in. Draft cluster bootstrap commands (kubeadm/containerd, ArgoCD workflow, `deploy.sh`) are in `../ESTIN SAIEP files/Scheduler/inial manifests/`.

## Conventions and gotchas

- `secrets.env`, `researcher-profiles.jsonl`, `spawn-requests.jsonl` and `venv/` are gitignored; never commit them.
- `setup_ui.sh` patches files inside `venv/` and `share/` with `sed` (creates `index.html.bak`); it assumes `venv/lib/python3.12`.
- Recent history is PR-merge style (`reconcile: native SAIEP interfaces…`, `Merge PR #2: SAIEP production deploy`); upstream JupyterHub files should stay close to upstream to ease merges.
