"""SAIEP console — interactive installer and cluster manager.

    python -m saiep_cli              # interactive menu
    python -m saiep_cli --dry-run    # print commands instead of running them
"""
import argparse
import os
import re
import secrets
import shlex
import shutil
import sys

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from . import KUBERNETES_MINOR, REPO_DIR, STATE_DIR, backup, nodes, platform, projects, tools
from .providers import KubeadmProvider, KindProvider, get_provider
from .runner import CommandError, Runner
from .state import State

BANNER = r"""
   ███████╗ █████╗ ██╗███████╗██████╗
   ██╔════╝██╔══██╗██║██╔════╝██╔══██╗
   ███████╗███████║██║█████╗  ██████╔╝
   ╚════██║██╔══██║██║██╔══╝  ██╔═══╝
   ███████║██║  ██║██║███████╗██║
   ╚══════╝╚═╝  ╚═╝╚═╝╚══════╝╚═╝
"""
NAVY, RED = '#34495e', '#e54c3f'

MENU = [
    ('1', 'Démarrer Docker (démon)', 'start_docker'),
    ('2', 'Installation initiale (outils manquants)', 'fresh_install'),
    ('3', "Vérifier l'installation", 'verify'),
    ('4', 'Créer le cluster (+ pile SAIEP)', 'create_cluster'),
    ('5', 'Construire les images Hub + Notebook', 'build_hub_images'),
    ('6', 'Configurer secrets.env (token + kubeconfig)', 'setup_secrets'),
    ('7', 'Démarrer JupyterHub (cluster + port-forward)', 'run_hub'),
    ('8', 'Ajouter des nœuds au cluster', 'add_nodes'),
    ('9', 'Préparer un nœud physique (CPU)', 'prepare_physical'),
    ('10', 'Préparer un nœud physique (GPU)', 'prepare_physical_gpu'),
    ('11', 'Rejoindre le cluster (worker)', 'join_cluster'),
    ('12', 'Rejoindre le cluster comme nœud de stockage', 'storage_node'),
    ('13', 'Arrêter un nœud', 'stop_physical_node'),
    ('14', 'Affecter des pods à un nœud (GitHub / local / terminal)', 'assign'),
    ('15', 'Démarrer tout le cluster', 'run_all'),
    ('16', 'Démarrer un nœud ou un projet', 'run_one'),
    ('17', 'Arrêter le cluster', 'stop_all'),
    ('18', 'Arrêter un nœud ou un projet', 'stop_one'),
    ('19', 'Désinstaller un projet (et ses dépendances)', 'uninstall'),
    ('20', 'Redéployer la pile SAIEP', 'deploy_stack'),
    ('21', 'Générer le kubeconfig du hub', 'hub_kubeconfig'),
    ('0', 'Quitter', None),
]


class Console_:
    def __init__(self, dry_run=False):
        self.c = Console()
        self.r = Runner(dry_run=dry_run, echo=lambda cmd: self.c.print(f'[dim]$ {cmd}[/]'))
        self.state = State()
        self.provider = get_provider(self.state, self.r)

    # --- ui helpers ----------------------------------------------------------
    def header(self):
        self.c.print(f'[bold {RED}]{BANNER}[/]')
        self.c.print(Panel.fit(
            f'[bold white]Secure AI Execution Platform — ESTIN[/]\n'
            f'fournisseur: [bold]{self.state["provider"]}[/]   cluster: [bold]{self.state["cluster"]}[/]   '
            f'projets: [bold]{len(self.state["projects"])}[/]',
            border_style=NAVY))

    def menu(self):
        table = Table(show_header=False, box=None, padding=(0, 2))
        for key, label, _ in MENU:
            table.add_row(f'[bold {RED}]{key:>2}[/]', label)
        self.c.print(Panel(table, title='[bold]Menu[/]', border_style=NAVY))
        return Prompt.ask('Choix', choices=[k for k, _, _ in MENU], show_choices=False)

    def pick(self, title, options):
        if not options:
            self.c.print('[yellow]Rien à sélectionner.[/]')
            return None
        for i, opt in enumerate(options, 1):
            self.c.print(f'  [bold]{i}[/]. {opt}')
        idx = IntPrompt.ask(title, choices=[str(i) for i in range(1, len(options) + 1)])
        return options[idx - 1]

    def target(self):
        kind = Prompt.ask('Cible', choices=['nœud', 'projet'], default='projet')
        if kind == 'nœud':
            return 'node', self.pick('Nœud', self.provider.nodes())
        return 'project', self.pick('Projet', sorted(self.state['projects']))

    # --- actions ---------------------------------------------------------------
    def fresh_install(self):
        missing = tools.missing_tools(self.r)
        if not missing:
            self.c.print('[green]Tous les outils sont déjà installés.[/]')
        else:
            self.c.print(f'Outils manquants : {", ".join(missing)}')
            left = tools.fresh_install(self.r, log=self.c.print)
            self.c.print('[green]Terminé.[/]' if not left else f'[yellow]Toujours manquants : {left}[/]')
        if self.r.which('docker'):
            if tools.docker_daemon_ok(self.r):
                self.c.print('[green]Le démon Docker est déjà démarré.[/]')
            else:
                self.c.print('[yellow]Docker est installé mais son démon ne tourne pas.[/]')
                if Confirm.ask('Démarrer Docker maintenant ?', default=True):
                    tools.start_docker(self.r, log=self.c.print)
        if isinstance(self.provider, KubeadmProvider) and Confirm.ask(
                'Préparer ce nœud pour kubeadm (swap, containerd, kubelet) ?', default=False):
            from . import KUBERNETES_MINOR
            tools.kubeadm_prepare_node(self.r, KUBERNETES_MINOR)

    def _show_verify(self, results):
        table = Table(title='Vérification', border_style=NAVY)
        table.add_column('Contrôle')
        table.add_column('État')
        table.add_column('Détail', style='dim')
        for name, ok, detail in results:
            table.add_row(name, '[green]OK[/]' if ok else f'[{RED}]ÉCHEC[/]', detail)
        self.c.print(table)

    def verify(self):
        results = tools.verify(self.r)
        self._show_verify(results)
        docker_ok = next((ok for name, ok, _ in results if name == 'démon docker'), False)
        if not docker_ok and self.r.which('docker'):
            self.c.print('[yellow]Le démon Docker est arrêté. Commandes équivalentes :[/]')
            self.c.print('  sudo systemctl enable --now docker')
            self.c.print('  docker info')
            if Confirm.ask('Démarrer Docker maintenant ?', default=True):
                if tools.start_docker(self.r, log=self.c.print):
                    self._show_verify(tools.verify(self.r))
                else:
                    self.c.print('[yellow]Consultez : sudo journalctl -u docker -n 100 --no-pager[/]')

    def create_cluster(self):
        provider = Prompt.ask('Fournisseur', choices=['kind', 'kubeadm'], default=self.state['provider'])
        self.state['provider'] = provider
        self.state['cluster'] = Prompt.ask('Nom du cluster', default=self.state['cluster'])
        self.state.save()
        self.provider = get_provider(self.state, self.r)
        if self.provider.exists():
            self.c.print('[yellow]Le cluster existe déjà.[/]')
        elif provider == 'kind':
            workers = IntPrompt.ask('Nœuds CPU', default=2)
            gpus = IntPrompt.ask('Nœuds GPU (étiquetés gpu=true)', default=0)
            self.provider.create_cluster(workers, gpus)
        else:
            self.provider.create_cluster()
            self.c.print(f'Commande de jonction pour les autres machines :\n[bold]{self.provider.join_command()}[/]')
        if Confirm.ask('Déployer la pile SAIEP (Argo, webhook, isolation) ?', default=True):
            self.deploy_stack()

    def add_nodes(self):
        if isinstance(self.provider, KubeadmProvider):
            self.c.print('Sur chaque nouvelle machine : option 1 (préparer le nœud), puis :')
            self.c.print(f'[bold]sudo {self.provider.join_command()}[/]')
            if Confirm.ask('Étiqueter un nœud comme GPU ?', default=False):
                node = self.pick('Nœud', self.provider.nodes())
                if node:
                    self.provider.label_gpu(node)
            return
        self.c.print('[yellow]kind ne peut pas ajouter de nœuds à chaud : le cluster sera recréé '
                     '(les projets et calculs seront perdus).[/]')
        workers = IntPrompt.ask('Nombre total de nœuds CPU', default=2)
        gpus = IntPrompt.ask('Nombre total de nœuds GPU', default=0)
        if Confirm.ask('Recréer le cluster maintenant ?', default=False):
            self.provider.recreate(workers, gpus)
            self.state['projects'] = {}
            self.state.save()
            self.deploy_stack()

    def assign(self):
        node = self.pick('Nœud cible', self.provider.nodes())
        if not node:
            return
        mode = Prompt.ask('Source', choices=['github', 'local', 'terminal'], default='github')
        if mode == 'terminal':
            self.provider.shell(node)
            return
        name = Prompt.ask('Nom du projet (a-z, 0-9, -)')
        source = Prompt.ask('URL GitHub (dépôt ou fichier .yaml)' if mode == 'github' else 'Chemin local')
        rendered, skipped = projects.deploy(self.r, self.state, name, source, node)
        self.c.print(f'[green]{len(rendered) - 1} objet(s) déployé(s) dans {projects.namespace_for(name)} '
                     f'sur {node}.[/]')
        if skipped:
            self.c.print(f'[yellow]Ignorés (types non autorisés) : {", ".join(skipped)}[/]')

    def run_all(self):
        self.provider.start_all()
        for name in self.state['projects']:
            projects.scale(self.r, self.state, name, up=True)

    def stop_all(self):
        if Confirm.ask('Arrêter tout le cluster ?', default=False):
            self.provider.stop_all()

    def run_one(self):
        kind, name = self.target()
        if name:
            self.provider.start_node(name) if kind == 'node' else projects.scale(self.r, self.state, name, True)

    def stop_one(self):
        kind, name = self.target()
        if name:
            self.provider.stop_node(name) if kind == 'node' else projects.scale(self.r, self.state, name, False)

    def uninstall(self):
        name = self.pick('Projet', sorted(self.state['projects']))
        if name and Confirm.ask(f'Supprimer {name} et son espace {projects.namespace_for(name)} ?', default=False):
            projects.uninstall(self.r, self.state, name)
            self.c.print('[green]Projet désinstallé.[/]')

    def deploy_stack(self):
        falco = Confirm.ask('Installer aussi Falco (détection à l\'exécution) ?', default=False)
        default_hub = 'dev' if getattr(self.provider, 'name', 'kind') == 'kind' else 'prod'
        hub_mode = Prompt.ask('Hub JupyterHub', choices=['dev', 'prod'], default=default_hub)
        platform.deploy_stack(self.r, self.provider, log=self.c.print, with_falco=falco,
                              dev_hub=(hub_mode == 'dev'))
        self.c.print('[green]Pile SAIEP déployée.[/]')

    def hub_kubeconfig(self):
        path, env = platform.write_hub_kubeconfig(self.r)
        self.c.print(f'[green]kubeconfig écrit : {path}[/]\nAjoutez à secrets.env :')
        self.c.print('\n'.join(env))

    def start_docker(self):
        if not self.r.which('docker'):
            self.c.print('[red]Docker CLI absent. Installez Docker Engine puis relancez.[/]')
            return
        if tools.start_docker(self.r, log=self.c.print):
            self.c.print('[yellow]Si le cluster kind existait déjà, utilisez l\'option 6 pour '
                         'redémarrer ses nœuds.[/]')

    def build_hub_images(self):
        platform.build_image(self.r, self.provider, platform.NOTEBOOK_IMAGE,
                             os.path.join(REPO_DIR, 'notebook', 'Dockerfile'), self.c.print)
        platform.build_image(self.r, self.provider, platform.HUB_IMAGE,
                             os.path.join(REPO_DIR, 'hub', 'Dockerfile'), self.c.print)
        self.c.print('[green]Images Hub + Notebook prêtes.[/]')

    def setup_secrets(self):
        path = os.path.join(REPO_DIR, 'secrets.env')
        if not os.path.exists(path):
            shutil.copyfile(os.path.join(REPO_DIR, 'secrets.env.example'), path)
            self.c.print('[green]secrets.env créé depuis secrets.env.example.[/]')
        with open(path) as fh:
            text = fh.read()
        token = _env_value(text, 'SAIEP_PROMETHEUS_TOKEN')
        if not token or token.startswith('replace-'):
            text = _set_env(text, 'SAIEP_PROMETHEUS_TOKEN', secrets.token_hex(32))
            self.c.print('[green]SAIEP_PROMETHEUS_TOKEN généré.[/]')
        else:
            self.c.print('[green]SAIEP_PROMETHEUS_TOKEN déjà présent (conservé).[/]')
        kubeconfig = os.path.join(STATE_DIR, 'hub-kubeconfig')
        if os.path.exists(kubeconfig):
            text = _set_env(text, 'SAIEP_KUBECONFIG', kubeconfig)
            self.c.print(f'[green]SAIEP_KUBECONFIG -> {kubeconfig}[/]')
        if self.r.ok(['kubectl', 'cluster-info', '--request-timeout=5s'], echo=False):
            if Confirm.ask('Générer aussi le kubeconfig du hub et les variables MinIO ?', default=True):
                try:
                    _, lines = platform.write_hub_kubeconfig(self.r)
                except (CommandError, OSError, KeyError) as exc:
                    self.c.print(f'[yellow]Kubeconfig non généré : {exc}[/]')
                else:
                    for line in lines:
                        key, _, value = line.removeprefix('export ').partition('=')
                        text = _set_env(text, key, value)
                    self.c.print('[green]Kubeconfig et variables MinIO ajoutés à secrets.env.[/]')
        else:
            self.c.print('[yellow]Cluster non joignable : token seul configuré. '
                         'Relancez cette option après l\'option 4.[/]')
        with open(path, 'w') as fh:
            fh.write(text)
        os.chmod(path, 0o600)
        self.c.print('[green]secrets.env prêt (mode 600).[/]')

    def run_hub(self):
        if not self.r.ok(['kubectl', '-n', 'saiep-system', 'get', 'deploy', 'jupyterhub'],
                         echo=False):
            raise RuntimeError('Hub cluster absent : lancez d\'abord l\'option 4 ou 20.')
        self.c.print('[green]Hub cluster : http://127.0.0.1:8000 — '
                     'Ctrl-C pour arrêter le port-forward.[/]')
        self.c.print('[yellow]Si le port 8000 est déjà utilisé, arrêtez l\'ancien Hub local.[/]')
        self.r.run(['kubectl', '-n', 'saiep-system', 'port-forward',
                    'svc/jupyterhub', '8000:8000'], interactive=True, cwd=REPO_DIR)

    def _kind_cluster_exists(self):
        if not isinstance(self.provider, KindProvider) or not self.r.which('kind'):
            return False
        try:
            return self.provider.exists()
        except OSError:
            return False

    def _require_physical(self):
        if self._kind_cluster_exists():
            raise RuntimeError(
                'un cluster kind utilise des conteneurs Docker : les nœuds physiques '
                'ne peuvent pas le rejoindre. Utilisez un cluster kubeadm.'
            )

    def prepare_physical(self, gpu=False):
        self._require_physical()
        label = 'GPU' if gpu else 'CPU'
        self.c.print(f'[bold]Préparation d\'un nœud physique {label}[/]')
        self.c.print('[yellow]Cette opération modifie le système local '
                     '(swap, modules noyau, containerd, kubelet).[/]')
        if gpu:
            self.c.print('[yellow]Le pilote NVIDIA et nvidia-container-toolkit peuvent '
                         'nécessiter un redémarrage.[/]')
        if not Confirm.ask(f'Préparer cette machine comme nœud {label} ?', default=True):
            return
        minor = Prompt.ask('Version Kubernetes', default=KUBERNETES_MINOR)
        nodes.prepare_node(self.r, gpu=gpu, k8s_minor=minor, log=self.c.print)

    def prepare_physical_gpu(self):
        self.prepare_physical(gpu=True)

    def join_cluster(self):
        self._require_physical()
        profile = nodes.read_profile(self.r)
        if not profile:
            raise RuntimeError('profil absent : lancez d\'abord l\'option 9 ou 10 sur cette machine.')
        mode = Prompt.ask('Saisie de la jointure', choices=['commande', 'champs'],
                          default='commande')
        if mode == 'commande':
            command = Prompt.ask('Commande kubeadm join complète', password=True)
        else:
            endpoint = Prompt.ask('Endpoint control-plane (host:6443)')
            token = Prompt.ask('Token bootstrap', password=True)
            ca_hash = Prompt.ask('discovery-token-ca-cert-hash', password=True)
            command = f'kubeadm join {endpoint} --token {token} --discovery-token-ca-cert-hash {ca_hash}'
        nodes.join_worker(self.r, command, log=self.c.print)

    def stop_physical_node(self):
        self._require_physical()
        mode = Prompt.ask('Mode', choices=['control-plane', 'local'],
                          default='control-plane')
        if mode == 'local':
            self.c.print('[yellow]Le drain doit avoir été fait depuis le control plane.[/]')
            if not Confirm.ask('Arrêter kubelet et containerd sur cette machine ?', default=False):
                return
            poweroff = Confirm.ask('Éteindre la machine ensuite ?', default=False)
            nodes.stop_local_services(self.r, poweroff=poweroff, log=self.c.print)
            return
        node = self.pick('Nœud à arrêter', nodes.worker_nodes(self.r))
        if node and Confirm.ask(f'Drainer {node} ? Les notebooks et jobs en cours seront interrompus.',
                                default=False):
            nodes.drain_node(self.r, node, log=self.c.print)

    def storage_node(self):
        self._require_physical()
        mode = Prompt.ask('Mode', choices=['join', 'deploy'], default='join')
        if mode == 'deploy':
            node = self.pick('Nœud de stockage', nodes.worker_nodes(self.r))
            if not node:
                return
            mount = Prompt.ask('Point de montage du disque de sauvegarde',
                               default='/srv/saiep-storage')
            storage_only = Confirm.ask(
                'Mode stockage exclusif (taint, pas de workloads normaux) ?', default=False)
            self.r.run(['kubectl', 'label', 'node', node,
                        'saiep.estin.dz/storage=true', '--overwrite'])
            if storage_only:
                self.r.run(['kubectl', 'taint', 'node', node,
                            nodes.STORAGE_TAINT, '--overwrite'])
            if Confirm.ask('Déployer MinIO de sauvegarde + Velero + snapshots etcd ?',
                           default=True):
                backup.deploy_backup_stack(self.r, node, mount,
                                           storage_only=storage_only, log=self.c.print)
            return

        profile = nodes.read_profile(self.r)
        if not profile:
            raise RuntimeError('profil absent : lancez d\'abord l\'option 9 ou 10 sur cette machine.')
        command = Prompt.ask('Commande kubeadm join complète', password=True)
        self.c.print(nodes.discover_disks(self.r))
        device = Prompt.ask('Périphérique du disque de sauvegarde (ex: /dev/sdb)')
        mount = Prompt.ask('Point de montage', default='/srv/saiep-storage')
        storage_only = Confirm.ask('Mode stockage exclusif (taint, pas de workloads normaux) ?',
                                   default=False)
        if not Confirm.ask(f'Formater/monter {device} sur {mount} si nécessaire puis rejoindre ?',
                           default=False):
            return
        nodes.join_worker(self.r, command, storage=True, storage_only=storage_only,
                          log=self.c.print)
        nodes.prepare_data_disk(self.r, device, mount, log=self.c.print)
        self.c.print('[green]Nœud de stockage prêt.[/]')
        self.c.print('Sur le control plane, lancez l\'option 12 en mode deploy pour installer '
                     'MinIO de sauvegarde, Velero et le CronJob etcd.')

    def loop(self):
        self.header()
        while True:
            choice = self.menu()
            action = dict((k, a) for k, _, a in MENU)[choice]
            if action is None:
                return
            try:
                getattr(self, action)()
            except (CommandError, ValueError, OSError, RuntimeError) as e:
                self.c.print(f'[bold {RED}]Erreur :[/] {e}')
            except KeyboardInterrupt:
                self.c.print('\n[yellow]Annulé.[/]')


def _env_value(text, key):
    match = re.search(rf'^\s*export\s+{re.escape(key)}=(.*)$', text, re.M)
    return match.group(1).strip().strip('"').strip("'") if match else ''


def _set_env(text, key, value):
    """Update or append ``export KEY=value`` in a shell env file."""
    line = f'export {key}={shlex.quote(str(value))}'
    pattern = re.compile(rf'^\s*export\s+{re.escape(key)}=.*$', re.M)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    if text and not text.endswith('\n'):
        text += '\n'
    return text + line + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='saiep', description='Console SAIEP')
    parser.add_argument('--dry-run', action='store_true', help='afficher les commandes sans les exécuter')
    args = parser.parse_args(argv)
    try:
        Console_(dry_run=args.dry_run).loop()
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
