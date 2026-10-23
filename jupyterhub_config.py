import json
import os
import sys
from datetime import datetime, timezone

from jupyterhub.handlers import BaseHandler
from nativeauthenticator import NativeAuthenticator
from nativeauthenticator.handlers import SignUpHandler

base_dir = os.path.dirname(os.path.abspath(__file__))

# Native SAIEP admin interface (resources + monitoring) lives in saiep_admin.py
sys.path.insert(0, base_dir)
import saiep_admin
import saiep_hub_config
# Job intake → Argo Workflows (Scheduling Layer, Path 2). Enabled when the Hub
# runs in-cluster or SAIEP_KUBECONFIG is set (see secrets.env.example).
import saiep_jobs

PUBLIC_URL = os.environ.get('SAIEP_HUB_PUBLIC_URL', 'https://104.248.22.193.sslip.io')
INTERNAL_HUB_URL = os.environ.get('SAIEP_HUB_BIND_URL', 'http://0.0.0.0:8000')

# Secrets are injected from secrets.env (sourced by start-hub.sh) and are NOT
# committed to git. See secrets.env.example for the expected variables.
PROMETHEUS_TOKEN = os.environ['SAIEP_PROMETHEUS_TOKEN']

c.JupyterHub.template_paths = [os.path.join(base_dir, 'templates')]

# The Hub runs as a Pod; Caddy/port-forward reaches it through the Service.
c.JupyterHub.bind_url = INTERNAL_HUB_URL
c.JupyterHub.public_url = PUBLIC_URL

# Caddy terminates TLS and forwards X-Forwarded-Proto=https so the Hub
# issues secure cookies and correct redirects.
c.JupyterHub.trusted_downstream_ips = ['172.17.0.0/16', '127.0.0.1', '10.0.0.0/8']

# Persistent Hub state lives on the saiep-hub-data PVC.
c.JupyterHub.db_url = os.environ.get(
    'SAIEP_HUB_DB', 'sqlite:////srv/jupyterhub/jupyterhub.sqlite'
)
c.JupyterHub.cookie_secret_file = os.environ.get(
    'SAIEP_HUB_COOKIE_SECRET', '/srv/jupyterhub/jupyterhub_cookie_secret'
)

# --- Authentication: demande d'inscription + activation par l'admin -------
# Workflow demanded by LITAN: users request an account at /hub/signup with
# their ESTIN email; admin LITAN verifies identity at /hub/authorize and
# activates the account. Only then can the user log in.
#
# At signup we also collect the researcher's project context (requested by
# Dalil HADJOUT): research theme, project type (NLP / Vision / Time series /
# other), lab and supervisor. These are persisted to researcher-profiles.jsonl
# so LITAN can review them at activation time and so they feed later analytics.
RESEARCHER_PROFILES_LOG = os.path.join(base_dir, 'researcher-profiles.jsonl')


class SAIEPSignUpHandler(SignUpHandler):
    """Captures the extra research fields from the signup form before
    delegating to NativeAuthenticator's normal account-creation logic."""

    async def post(self):
        username = self.get_body_argument('username', '', strip=True)
        # Only record genuinely new signup requests (skip retries on an
        # existing username, e.g. validation errors).
        if username and not self.authenticator.user_exists(username):
            profile = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'username': username,
                'email': self.get_body_argument('email', '', strip=True),
                'project_theme': self.get_body_argument('project_theme', '', strip=True)[:500],
                'project_type': self.get_body_argument('project_type', '', strip=True),
                'lab': self.get_body_argument('lab', '', strip=True)[:200],
                'supervisor': self.get_body_argument('supervisor', '', strip=True)[:200],
                'description': self.get_body_argument('research_description', '', strip=True)[:2000],
            }
            try:
                with open(RESEARCHER_PROFILES_LOG, 'a') as f:
                    f.write(json.dumps(profile, ensure_ascii=False) + '\n')
            except OSError:
                self.log.warning('Could not persist researcher profile for %s', username)
        await super().post()


class SAIEPAuthenticator(NativeAuthenticator):
    """NativeAuthenticator that swaps in our research-aware signup handler."""

    def get_handlers(self, app):
        handlers = super().get_handlers(app)
        return [
            (path, SAIEPSignUpHandler if path == r'/signup' else handler)
            for path, handler in handlers
        ]


c.JupyterHub.authenticator_class = SAIEPAuthenticator
c.Authenticator.admin_users = {'r4y4n3', 'litan'}
# NativeAuthenticator enforces account activation itself (is_authorized) in
# authenticate(); JupyterHub >= 5 additionally requires an explicit allow
# rule, so allow_all here does NOT bypass the admin-approval workflow.
c.Authenticator.allow_all = True
c.NativeAuthenticator.open_signup = False          # activation by admin required
c.NativeAuthenticator.ask_email_on_signup = True   # ESTIN email for identity check
c.NativeAuthenticator.minimum_password_length = 8
c.NativeAuthenticator.allowed_failed_logins = 5
c.NativeAuthenticator.seconds_before_next_try = 600

# --- Page d'accueil publique (landing) -------------------------------------
# Page de présentation SAIEP affichée aux visiteurs (intégrée depuis le
# travail de l'équipe). Servie publiquement à /hub/landing.
class LandingHandler(BaseHandler):
    async def get(self):
        landing = os.path.join(base_dir, 'static', 'landing', 'index.html')
        with open(landing, 'r', encoding='utf-8') as f:
            self.finish(f.read())


# --- Gestion des ressources (interface NATIVE SAIEP) -----------------------
# Remplace l'outil tiers Portainer : interface complète intégrée à JupyterHub
# (conteneurs, cluster Swarm, volumes, réseaux, monitoring interne). Code dans
# saiep_admin.py. Réservée aux administrateurs.
c.JupyterHub.extra_handlers = [
    (r'/landing', LandingHandler),
] + saiep_admin.resource_handlers() + saiep_jobs.job_handlers()

# --- Calculs (Argo) : palier de quota par utilisateur ------------------------
# phd | researcher | external (scheduler/policies/quota-tiers.yaml). Les
# utilisateurs absents reçoivent le palier par défaut.
saiep_jobs.USER_TIERS = {}

# --- Redirection par rôle après connexion ----------------------------------
# Les visiteurs non connectés voient la page d'accueil ; après connexion,
# chaque rôle arrive directement sur son espace : le super admin sur l'IHM
# de configuration, l'admin LITAN sur l'activation des comptes, les
# utilisateurs sur leur serveur (formulaire de dimensionnement).
def default_url(handler):
    user = handler.current_user
    if user is None:
        return '/hub/landing'
    if user.name == 'r4y4n3':
        return '/hub/admin'
    if user.name == 'litan':
        return '/hub/authorize'
    # After login: sizing form -> Mes calculs -> notebook.
    return '/hub/spawn?next=%2Fhub%2Fjobs'

c.JupyterHub.default_url = default_url

# --- Spawner: KubeSpawner notebooks in saiep-u-<user> ----------------------
# CPU/RAM from the sizing form become Pod requests=limits; the duration becomes
# the Pod activeDeadlineSeconds. The Hub must run in-cluster so it can reach
# each notebook Pod directly.
saiep_hub_config.configure(c, base_dir)

# --- Monitoring : interface NATIVE SAIEP (Grafana supprimé) ----------------
# Plus aucun outil tiers : le monitoring est notre propre interface
# (/hub/monitoring, voir saiep_admin.py) alimentée directement par Prometheus.
# Prometheus reste interne (127.0.0.1:9090) et scrute /hub/metrics avec ce
# jeton de service.
c.JupyterHub.services = [
    {
        'name': 'prometheus',
        'api_token': PROMETHEUS_TOKEN,
    },
]
c.JupyterHub.load_roles = [
    {
        'name': 'metrics-scraper',
        'scopes': ['read:metrics'],
        'services': ['prometheus'],
    },
]
