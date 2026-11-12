import pytest
from butler.settings import defaults, load, save


def test_desktop_explains_capture_gates_and_limits():
    from pathlib import Path
    ui = (Path(__file__).resolve().parents[1] / 'desktop' / 'plugin.js').read_text()
    assert 'collect_event_history' in ui
    assert 'plaintext' in ui
    assert 'not automatically deleted' in ui
    assert 'history replay remains disabled' in ui
    assert 'not connected to the scheduler, event collection' not in ui


def test_settings_are_profile_scoped_and_default_off(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    data = defaults()
    assert data['activityEnabled'] is False
    assert data['notificationsEnabled'] is False
    data['timezone'] = 'America/Chicago'
    save(a, data)
    assert load(a)['timezone'] == 'America/Chicago'
    assert load(b) == defaults()


def test_reject_invalid_settings_without_overwriting(tmp_path):
    good = defaults()
    save(tmp_path, good)
    bad = dict(good, dailyLimit=-1)
    with pytest.raises(ValueError):
        save(tmp_path, bad)
    assert load(tmp_path) == good


@pytest.mark.parametrize('patch', [
    {'timezone': '../bad'}, {'tone': 'invented'}, {'persistence': 'invented'},
    {'dailyLimit': True}, {'dailyLimit': 1001}, {'quietEnabled': 'true'},
    {'quietEnabled': True}, {'quietStart': '25:00'}, {'notificationsEnabled': 1},
    {'toolProgressMode': 'loud'}, {'backgroundProcessNotifications': 'loud'},
    {'memoryNotifications': 'loud'}, {'lifetimeAwarenessEnabled': 'yes'},
    {'unknown': 'x'},
])
def test_invalid_values_are_rejected(tmp_path, patch):
    with pytest.raises(ValueError):
        save(tmp_path, dict(defaults(), **patch))


def test_dashboard_http_roundtrip_and_read_only_responsibilities(tmp_path, monkeypatch):
    fastapi = pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    import importlib.util
    import sys
    import types
    from datetime import datetime, timezone
    from pathlib import Path
    from fastapi.testclient import TestClient
    from butler.store import ResponsibilityStore

    plugins = types.ModuleType('plugins')
    storage = types.ModuleType('plugins.plugin_storage')
    active = [tmp_path / 'a']
    storage.plugin_data_dir = lambda name: active[0]
    monkeypatch.setitem(sys.modules, 'plugins', plugins)
    monkeypatch.setitem(sys.modules, 'plugins.plugin_storage', storage)
    spec = importlib.util.spec_from_file_location('butler_test_api', Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    fake_hermes_cfg = {'display': {}, 'platforms': {'photon': {'enabled': True, 'gateway_restart_notification': True}}}
    api._config_loader = lambda: dict(fake_hermes_cfg)
    api._config_saver = lambda patch, **kw: fake_hermes_cfg.update(patch)
    api._sys_tz_fn = lambda: 'America/Chicago'
    api._ip_tz_fn = lambda: 'America/New_York'
    app = fastapi.FastAPI()
    app.include_router(api.router, prefix='/api/plugins/butler')
    client = TestClient(app)
    url = '/api/plugins/butler'
    assert client.get(url + '/state').json()['responsibilities'] == []
    assert not (active[0] / 'responsibilities.sqlite3').exists()
    active[0].mkdir()
    store = ResponsibilityStore(active[0] / 'responsibilities.sqlite3')
    item = store.create('Actual task', datetime(2026, 1, 1, tzinfo=timezone.utc), metadata={'session_key': 'private'})
    store.close()
    state = client.get(url + '/state').json()
    assert state['responsibilities'][0]['id'] == item['id']
    assert 'metadata' not in state['responsibilities'][0]
    assert state['settings_enforced'] is False
    assert state['lifetime_awareness_active'] is True
    assert state['detected_system_timezone'] == 'America/Chicago'
    det = client.post(url + '/detect-timezone', json={'use_ip': True}).json()
    assert det['detected_ip_timezone'] == 'America/New_York'
    assert det['suggested_timezone'] == 'America/New_York'
    preset_res = client.post(url + '/preset', json={'preset': 'quiet_human'})
    assert preset_res.status_code == 200
    assert preset_res.json()['settings']['applySystemControls'] is True
    assert preset_res.json()['hermes_sync']['applied'] is True
    assert client.post(url + '/preset', json={'preset': 'bad'}).status_code == 422
    data = dict(defaults(), displayName='  Hayk  ', timezone='America/Chicago')
    put_res = client.put(url + '/settings', json=data)
    assert put_res.status_code == 200
    assert put_res.json()['displayName'] == 'Hayk'
    assert client.put(url + '/settings', json=dict(data, displayName='x' * 81)).status_code == 422
    assert client.put(url + '/settings', json=dict(data, displayName='bad\nname')).status_code == 422
    assert client.put(url + '/settings', json=dict(data, dailyLimit=-1)).status_code == 422
    assert client.get(url + '/state').json()['settings'] == dict(data, displayName='Hayk')
    active[0] = tmp_path / 'b'
    assert client.get(url + '/state').json()['settings'] == defaults()
    assert client.get(url + '/state').json()['responsibilities'] == []
