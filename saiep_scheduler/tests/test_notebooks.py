from saiep_scheduler import notebooks


def test_notebook_session_start_finish_merge(tmp_path, monkeypatch):
    path = tmp_path / 'notebook-sessions.jsonl'
    monkeypatch.setenv('SAIEP_NOTEBOOK_LOG', str(path))
    notebooks.start(user='alice', namespace='saiep-u-alice', pod='saiep-nb-alice',
                    cpu='1', memory='1Gi', duration=3600, image='saiep/notebook:dev')
    notebooks.finish(user='alice', namespace='saiep-u-alice', pod='saiep-nb-alice',
                     phase='Succeeded', message='stop')
    sessions = notebooks.sessions()
    assert len(sessions) == 1
    row = sessions[0]
    assert row['job'] == 'notebook-alice'
    assert row['phase'] == 'Succeeded'
    assert row['cpu'] == '1'
    assert row['log_key'] == 'saiep-u-alice/notebook/saiep-nb-alice/main.log'


def test_notebook_sessions_are_scoped_by_filter(tmp_path, monkeypatch):
    path = tmp_path / 'notebook-sessions.jsonl'
    monkeypatch.setenv('SAIEP_NOTEBOOK_LOG', str(path))
    notebooks.start(user='alice', namespace='saiep-u-alice', pod='saiep-nb-alice',
                    cpu='1', memory='1Gi', duration=3600, image='img')
    notebooks.start(user='bob', namespace='saiep-u-bob', pod='saiep-nb-bob',
                    cpu='2', memory='2Gi', duration=3600, image='img')
    assert {row['namespace'] for row in notebooks.sessions()} == {'saiep-u-alice', 'saiep-u-bob'}
    assert notebooks.find('saiep-nb-alice')['user'] == 'alice'


def test_restarted_notebook_ignores_previous_stop(tmp_path, monkeypatch):
    path = tmp_path / 'notebook-sessions.jsonl'
    monkeypatch.setenv('SAIEP_NOTEBOOK_LOG', str(path))
    notebooks.start(user='alice', namespace='saiep-u-alice', pod='saiep-nb-alice',
                    cpu='1', memory='1Gi', duration=3600, image='img')
    notebooks.finish(user='alice', namespace='saiep-u-alice', pod='saiep-nb-alice',
                     phase='Succeeded')
    notebooks.start(user='alice', namespace='saiep-u-alice', pod='saiep-nb-alice',
                    cpu='2', memory='2Gi', duration=7200, image='img2')
    row = notebooks.sessions(live={'saiep-nb-alice': 'Running'})[0]
    assert row['phase'] == 'Running'
    assert row['cpu'] == '2'
    assert row['image'] == 'img2'


def test_stale_running_session_is_reconciled_with_pod_phase(tmp_path, monkeypatch):
    path = tmp_path / 'notebook-sessions.jsonl'
    monkeypatch.setenv('SAIEP_NOTEBOOK_LOG', str(path))
    notebooks.start(user='alice', namespace='saiep-u-alice', pod='saiep-nb-alice',
                    cpu='1', memory='1Gi', duration=3600, image='img')
    assert notebooks.sessions(live={})[0]['phase'] == 'Succeeded'
    assert notebooks.sessions(live={'saiep-nb-alice': 'Failed'})[0]['phase'] == 'Failed'
    assert notebooks.sessions(live={'saiep-nb-alice': 'Running'})[0]['phase'] == 'Running'


def test_archive_key_is_inside_tenant_prefix(tmp_path):
    assert notebooks.archive_key('saiep-u-admin', 'saiep-nb-admin') == \
        'saiep-u-admin/notebook/saiep-nb-admin/main.log'
