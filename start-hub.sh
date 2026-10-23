#!/usr/bin/env bash
# JupyterHub runs inside Kubernetes (Deployment saiep-system/jupyterhub).
# This helper only exposes its UI on http://127.0.0.1:8000 for local access.
cd "$(dirname "$0")" || exit 1
if ! kubectl -n saiep-system get deploy/jupyterhub >/dev/null 2>&1; then
    echo "Hub Kubernetes absent : lancez 'python -m saiep_cli' (option 3 ou 11)." >&2
    exit 1
fi
exec kubectl -n saiep-system port-forward svc/jupyterhub 8000:8000
