"""SAIEP console — interactive installer and cluster manager.

    python -m saiep_cli              # interactive menu
    python -m saiep_cli --dry-run    # print commands instead of running them
"""
import argparse
import sys

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from . import platform, projects, tools
from .providers import KubeadmProvider, get_provider
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
    ('1', 'Installation initiale (outils manquants)', 'fresh_install'),
    ('2', "Vérifier l'installation", 'verify'),
    ('3', 'Créer le cluster (+ pile SAIEP)', 'create_cluster'),
    ('4', 'Ajouter des nœuds au cluster', 'add_nodes'),
    ('5', 'Affecter des pods à un nœud (GitHub / local / terminal)', 'assign'),
    ('6', 'Démarrer tout le cluster', 'run_all'),
    ('7', 'Démarrer un nœud ou un projet', 'run_one'),
    ('8', 'Arrêter le cluster', 'stop_all'),
    ('9', 'Arrêter un nœud ou un projet', 'stop_one'),
    ('10', 'Désinstaller un projet (et ses dépendances)', 'uninstall'),
    ('11', 'Redéployer la pile SAIEP', 'deploy_stack'),
    ('12', 'Générer le kubeconfig du hub', 'hub_kubeconfig'),
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
        if isinstance(self.provider, KubeadmProvider) and Confirm.ask(
                'Préparer ce nœud pour kubeadm (swap, containerd, kubelet) ?', default=False):
            from . import KUBERNETES_MINOR
            tools.kubeadm_prepare_node(self.r, KUBERNETES_MINOR)

    def verify(self):
        table = Table(title='Vérification', border_style=NAVY)
        table.add_column('Contrôle')
        table.add_column('État')
        table.add_column('Détail', style='dim')
        for name, ok, detail in tools.verify(self.r):
            table.add_row(name, '[green]OK[/]' if ok else f'[{RED}]ÉCHEC[/]', detail)
        self.c.print(table)

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
        platform.deploy_stack(self.r, self.provider, log=self.c.print, with_falco=falco)
        self.c.print('[green]Pile SAIEP déployée.[/]')

    def hub_kubeconfig(self):
        path, env = platform.write_hub_kubeconfig(self.r)
        self.c.print(f'[green]kubeconfig écrit : {path}[/]\nAjoutez à secrets.env :')
        self.c.print('\n'.join(env))

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
