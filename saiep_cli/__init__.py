"""SAIEP installer / cluster console. Run: python -m saiep_cli"""
import os

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.environ.get('SAIEP_HOME', os.path.expanduser('~/.saiep'))

# Pinned versions installed by "Fresh install" when missing.
KUBERNETES_MINOR = 'v1.36'  # pkgs.k8s.io apt channel for kubeadm nodes
KIND_VERSION = 'v0.33.0'
HELM_VERSION = 'v3.18.6'
ARGO_VERSION = 'v4.1.5'
