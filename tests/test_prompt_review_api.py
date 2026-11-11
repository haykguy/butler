from types import ModuleType, SimpleNamespace
import sys

from test_dashboard_api import _load_api_module


def _profile(monkeypatch, home):
    constants = ModuleType('hermes_constants')
    constants.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)


def test_review_uses_active_profile_and_does_not_write(tmp_path, monkeypatch):
    env = _load_api_module(tmp_path, monkeypatch)
    _profile(monkeypatch, env['hermes_home'])
    env['api'].plugin_data_dir = lambda name: (_ for _ in ()).throw(AssertionError('GET must not create storage'))
    seen = []
    env['api']._prompt_review = SimpleNamespace(review=lambda home: seen.append(home) or {'items': []})
    response = env['client'].get('/api/plugins/butler/prompt-review')
    assert response.status_code == 200
    assert response.json() == {'items': []}
    assert seen == [env['hermes_home']]
    assert not env['hermes_home'].exists()


def test_apply_requires_explicit_valid_selection(tmp_path, monkeypatch):
    env = _load_api_module(tmp_path, monkeypatch)
    _profile(monkeypatch, env['hermes_home'])
    calls = []
    env['api']._prompt_review = SimpleNamespace(apply=lambda home, items: calls.append((home, items)) or {'applied': ['one']})
    url = '/api/plugins/butler/prompt-review/apply'
    for body in ({}, {'items': []}, {'items': [{'id': '../file', 'sha256': 'x', 'proposed': 'a'}]},
                 {'items': [{'id': 'one', 'sha256': 'a' * 64, 'proposed': 'a', 'path': '/tmp/x'}]}):
        assert env['client'].post(url, json=body).status_code == 422
    assert not calls
    items = [{'id': 'one', 'sha256': 'a' * 64, 'proposed': 'new'}]
    response = env['client'].post(url, json={'items': items})
    assert response.status_code == 200
    assert calls == [(env['hermes_home'], items)]


def test_real_backend_preview_apply_and_stale_conflict(tmp_path, monkeypatch):
    env = _load_api_module(tmp_path, monkeypatch)
    _profile(monkeypatch, env['hermes_home'])
    home = env['hermes_home']
    home.mkdir(parents=True)
    soul = home / 'SOUL.md'
    soul.write_text('Existing voice\n')
    client = env['client']
    url = '/api/plugins/butler/prompt-review'
    response = client.get(url)
    assert response.status_code == 200
    item = next(item for item in response.json()['items'] if item['id'] == 'soul')
    assert soul.read_text() == 'Existing voice\n'
    selection = {key: item[key] for key in ('id', 'sha256', 'proposed')}
    soul.write_text('Changed after preview\n')
    response = client.post(url + '/apply', json={'items': [selection]})
    assert response.status_code == 409
    assert 'conflict' in response.json()['detail'].lower()
    assert soul.read_text() == 'Changed after preview\n'
    item = next(item for item in client.get(url).json()['items'] if item['id'] == 'soul')
    selection = {key: item[key] for key in ('id', 'sha256', 'proposed')}
    response = client.post(url + '/apply', json={'items': [selection]})
    assert response.status_code == 200
    assert soul.read_text() == item['proposed']
    assert not (home / 'skills').exists()
