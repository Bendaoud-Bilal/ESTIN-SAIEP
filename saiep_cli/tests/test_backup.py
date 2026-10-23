import yaml

from saiep_cli import backup


def test_backup_credentials_are_private_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, 'STATE_DIR', str(tmp_path))
    first = backup.credentials()
    assert backup.credentials() == first
    assert (tmp_path / 'backup-minio.json').stat().st_mode & 0o077 == 0


def test_backup_stack_renders_without_placeholders(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, 'STATE_DIR', str(tmp_path))
    text = backup.render_stack('worker-gpu-1', '/srv/saiep-storage')
    assert '${' not in text
    docs = [d for d in yaml.safe_load_all(text) if d]
    kinds = [d['kind'] for d in docs]
    assert 'PersistentVolume' in kinds
    assert 'Deployment' in kinds
    assert 'CronJob' in kinds
    deployment = next(d for d in docs if d['kind'] == 'Deployment')
    assert deployment['spec']['template']['spec']['nodeSelector'] == {
        'saiep.estin.dz/storage': 'true'
    }
    cron = next(d for d in docs if d['kind'] == 'CronJob')
    assert cron['spec']['schedule'] == '0 */6 * * *'


def test_velero_values_have_credentials_and_no_template_placeholders(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, 'STATE_DIR', str(tmp_path))
    text = backup.render_velero_values()
    assert '${' not in text
    assert 'saiep-backup-minio.saiep-backup.svc.cluster.local:9000' in text
    assert 'aws_access_key_id=' in text
