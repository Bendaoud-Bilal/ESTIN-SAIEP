"""Job intake: validate a user's execution request and turn it into an Argo
Workflow that references a SAIEP ClusterWorkflowTemplate
(scheduler/argo/workflows.yaml). Every request enters through Argo — no
bare Pods are created by the hub.
"""
import re
from dataclasses import asdict, dataclass

from . import LABEL_DOMAIN

PRIORITY_CLASSES = {'batch': 'saiep-batch', 'background': 'saiep-background'}
CPU_CHOICES = ('0.5', '1', '2', '4')
MEMORY_CHOICES = ('512Mi', '1Gi', '2Gi', '4Gi', '8Gi')
GPU_CHOICES = (0, 1, 2)
ISOLATION_TIERS = ('standard', 'sandboxed')
MAX_TIMEOUT_SECONDS = 7 * 24 * 3600
MAX_RETRIES = 3
_NAME_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$')


@dataclass
class JobRequest:
    name: str
    image: str
    command: str
    cpu: str = '1'
    memory: str = '1Gi'
    gpu: int = 0
    tier: str = 'batch'
    isolation: str = 'standard'
    timeout_seconds: int = 3600
    retries: int = 1

    @classmethod
    def from_form(cls, data):
        """Build from untrusted form/JSON input; raises ValueError on bad input."""
        try:
            req = cls(
                name=str(data.get('name', '')).strip().lower(),
                image=str(data.get('image', '')).strip(),
                command=str(data.get('command', '')).strip()[:4000],
                cpu=str(data.get('cpu', '1')),
                memory=str(data.get('memory', '1Gi')),
                gpu=int(data.get('gpu', 0)),
                tier=str(data.get('tier', 'batch')),
                isolation=str(data.get('isolation', 'standard')),
                timeout_seconds=int(data.get('timeout_seconds', 3600)),
                retries=int(data.get('retries', 1)),
            )
        except (TypeError, ValueError) as e:
            raise ValueError(f'requête invalide: {e}') from e
        req.validate()
        return req

    def validate(self):
        if not _NAME_RE.match(self.name):
            raise ValueError('nom invalide (a-z, 0-9, -, 40 caractères max)')
        if not self.image or len(self.image) > 255 or any(c.isspace() for c in self.image):
            raise ValueError('image invalide')
        if not self.command:
            raise ValueError('commande requise')
        if self.cpu not in CPU_CHOICES:
            raise ValueError(f'cpu doit être parmi {CPU_CHOICES}')
        if self.memory not in MEMORY_CHOICES:
            raise ValueError(f'mémoire doit être parmi {MEMORY_CHOICES}')
        if self.gpu not in GPU_CHOICES:
            raise ValueError(f'gpu doit être parmi {GPU_CHOICES}')
        if self.tier not in PRIORITY_CLASSES:
            raise ValueError(f'priorité doit être parmi {tuple(PRIORITY_CLASSES)}')
        if self.isolation not in ISOLATION_TIERS:
            raise ValueError(f'isolation doit être parmi {ISOLATION_TIERS}')
        if not 60 <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError('durée max hors limites (60 s à 7 jours)')
        if not 0 <= self.retries <= MAX_RETRIES:
            raise ValueError(f'tentatives entre 0 et {MAX_RETRIES}')

    def as_dict(self):
        return asdict(self)


def build_workflow(namespace, username, req):
    """Argo Workflow manifest for a validated JobRequest."""
    template = 'saiep-gpu-job' if req.gpu else 'saiep-cpu-job'
    params = {
        'image': req.image,
        'command': req.command,
        'cpu': req.cpu,
        'memory': req.memory,
        'retries': str(req.retries),
    }
    if req.gpu:
        params['gpu'] = str(req.gpu)
    labels = {
        f'{LABEL_DOMAIN}/user': namespace.removeprefix('saiep-u-'),
        f'{LABEL_DOMAIN}/job': req.name,
        f'{LABEL_DOMAIN}/tier': req.tier,
    }
    return {
        'apiVersion': 'argoproj.io/v1alpha1',
        'kind': 'Workflow',
        'metadata': {
            'generateName': f'{req.name}-',
            'namespace': namespace,
            'labels': labels,
            'annotations': {f'{LABEL_DOMAIN}/submitted-by': username},
        },
        'spec': {
            'workflowTemplateRef': {'name': template, 'clusterScope': True},
            'arguments': {
                'parameters': [{'name': k, 'value': v} for k, v in params.items()]
            },
            'podPriorityClassName': PRIORITY_CLASSES[req.tier],
            'activeDeadlineSeconds': req.timeout_seconds,
            'serviceAccountName': 'saiep-runner',
            'podMetadata': {
                'labels': {
                    **labels,
                    f'{LABEL_DOMAIN}/isolation-tier': req.isolation,
                }
            },
        },
    }
