"""SAIEP Scheduling Layer — hub-side library.

Pure builders (tenants, workflows) + thin Kubernetes access (k8s) + advisory
AI hook (advisor) + append-only scheduling decision records (records).
Policy values live in YAML under scheduler/ and isolator/ (GitOps source of
truth); this package only renders and applies them.
"""
import os

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL_DOMAIN = 'saiep.estin.dz'
