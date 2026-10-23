"""Local-dev cluster Hub: ESTIN templates + /hub/jobs with KubeSpawner.

This config is meant to run *inside* the kind cluster (the Hub is a Pod), so
KubeSpawner can reach notebook Pod IPs. Use the CLI option "Démarrer
JupyterHub" to deploy it and port-forward the Hub Service.

Dummy auth: any username, password from SAIEP_DEV_PASSWORD.
"""
import os
import sys

c = get_config()  # noqa

repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo)

import saiep_hub_config  # noqa: E402
import saiep_jobs  # noqa: E402

c.JupyterHub.authenticator_class = 'dummy'
c.DummyAuthenticator.password = os.environ['SAIEP_DEV_PASSWORD']
c.Authenticator.allow_all = True
c.JupyterHub.bind_url = os.environ.get('SAIEP_HUB_BIND_URL', 'http://0.0.0.0:8000')
c.JupyterHub.public_url = os.environ.get('SAIEP_HUB_PUBLIC_URL', 'http://127.0.0.1:8000')
c.JupyterHub.db_url = os.environ.get(
    'SAIEP_HUB_DB', 'sqlite:////srv/jupyterhub/jupyterhub-dev.sqlite'
)
c.JupyterHub.cookie_secret_file = os.environ.get(
    'SAIEP_HUB_COOKIE_SECRET', '/srv/jupyterhub/jupyterhub-dev-cookie-secret'
)
c.JupyterHub.extra_handlers = saiep_jobs.job_handlers()
c.JupyterHub.default_url = '/hub/spawn?next=%2Fhub%2Fjobs'

saiep_hub_config.configure(c, repo)
