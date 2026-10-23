"""KubeSpawner subclass used by the SAIEP Hub.

The notebook Pod is deleted by KubeSpawner.stop(), so archive its live log and
close the notebook session record *before* delegating to the parent stop.
"""
from kubespawner import KubeSpawner

from . import notebooks


class SAIEPSpawner(KubeSpawner):
    async def stop(self, now=False):
        if not getattr(self, 'pod_name', None) or not getattr(self, 'namespace', None):
            return await super().stop(now)
        record = notebooks.find(self.pod_name)
        if record is None:
            pod_config = self.extra_pod_config if isinstance(self.extra_pod_config, dict) else {}
            notebooks.start(
                user=self.user.name,
                namespace=self.namespace,
                pod=self.pod_name,
                cpu=str(self.cpu_guarantee or ''),
                memory=str(self.mem_guarantee or ''),
                duration=int(pod_config.get('activeDeadlineSeconds') or 0),
                image=self.image,
            )
        log_key = notebooks.archive(self)
        notebooks.finish(
            user=self.user.name,
            namespace=self.namespace,
            pod=self.pod_name,
            phase='Succeeded' if not now else 'Stopped',
            message='arrêt du serveur notebook',
            log_key=log_key,
        )
        return await super().stop(now)
