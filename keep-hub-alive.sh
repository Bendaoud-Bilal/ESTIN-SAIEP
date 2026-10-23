#!/usr/bin/env bash
# The Hub is a Kubernetes Deployment now; this no longer starts a local
# process. Run it from cron/systemd to ensure the Deployment is healthy.
kubectl -n saiep-system rollout status deploy/jupyterhub --timeout=60s
