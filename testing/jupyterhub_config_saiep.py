"""Local dev config: ESTIN templates + /hub/jobs against a local cluster.

Dummy auth (any username, password from SAIEP_DEV_PASSWORD), simple spawner,
localhost only. Usage:
    export SAIEP_KUBECONFIG=~/.saiep/hub-kubeconfig SAIEP_API_SERVER_CIDR=... SAIEP_API_SERVER_PORT=6443
    export SAIEP_DEV_PASSWORD=<any>
    ./venv/bin/jupyterhub -f testing/jupyterhub_config_saiep.py
"""
import os
import sys

c = get_config()  # noqa

repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo)
import saiep_jobs  # noqa: E402

c.JupyterHub.authenticator_class = 'dummy'
c.DummyAuthenticator.password = os.environ['SAIEP_DEV_PASSWORD']
c.Authenticator.allow_all = True
c.JupyterHub.spawner_class = 'simple'
c.JupyterHub.bind_url = 'http://127.0.0.1:8000'
c.JupyterHub.template_paths = [os.path.join(repo, 'templates')]
c.JupyterHub.extra_handlers = saiep_jobs.job_handlers()
c.JupyterHub.default_url = '/hub/jobs'
c.JupyterHub.db_url = 'sqlite:////tmp/saiep-dev-hub.sqlite'
c.JupyterHub.cookie_secret_file = '/tmp/saiep-dev-hub-cookie-secret'
