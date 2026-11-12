"""Tests for Workstream 4: Dashboard REST API and Desktop plugin surface."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import types

import pytest

from butler.routes import RouteRegistry
from butler.settings import defaults
from butler.store import ResponsibilityStore

UTC = timezone.utc
BUTLER_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = BUTLER_ROOT.parent


def _load_api_module(tmp_path: Path, monkeypatch, *, now_dt: datetime | None = None):
    fastapi = pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient

    plugins_pkg = types.ModuleType('plugins')
    storage_mod = types.ModuleType('plugins.plugin_storage')
    profile_dir = tmp_path / 'hermes-home' / 'plugin-data' / 'butler'
    storage_mod.plugin_data_dir = lambda name: profile_dir
    monkeypatch.setitem(sys.modules, 'plugins', plugins_pkg)
    monkeypatch.setitem(sys.modules, 'plugins.plugin_storage', storage_mod)

    mod_name = f'_test_butler_plugin_api_{id(tmp_path)}'
    spec = importlib.util.spec_from_file_location(
        mod_name, BUTLER_ROOT / 'dashboard' / 'plugin_api.py',
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

    hermes_cfg = {
        'display': {'tool_progress': 'all', 'interim_assistant_messages': True},
        'platforms': {'telegram': {'enabled': True, 'gateway_restart_notification': True}},
        'plugins': {'entries': {'butler': {'enabled': True, 'allow_gateway_injection': False}}},
    }

    def _deep_merge(dst: dict, patch: dict):
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                _deep_merge(dst[k], v)
            else:
                dst[k] = v

    api._config_loader = lambda: dict(hermes_cfg)
    api._config_saver = lambda patch, **kw: _deep_merge(hermes_cfg, patch)
    api._sys_tz_fn = lambda: 'America/Chicago'
    api._ip_tz_fn = lambda: 'America/New_York'
    clock = [now_dt or datetime(2026, 11, 11, 15, 0, tzinfo=UTC)]  # 09:00 CST
    api._now_fn = lambda: clock[0]

    app = fastapi.FastAPI()
    app.include_router(api.router, prefix='/api/plugins/butler')
    # Also mount unprefixed to verify hidden /api/plugins/butler/* alias routes
    raw_app = fastapi.FastAPI()
    raw_app.include_router(api.router)

    return {
        'api': api,
        'client': TestClient(app),
        'raw_client': TestClient(raw_app),
        'profile_dir': profile_dir,
        'hermes_home': profile_dir.parent.parent,
        'hermes_cfg': hermes_cfg,
        'clock': clock,
    }


def _seed_state_db_sessions(state_db_path: Path, rows: list[dict]):
    state_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state_db_path) as db:
        db.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                session_key TEXT,
                ended_at REAL,
                last_activity_at REAL,
                started_at REAL
            )
            """
        )
        for r in rows:
            db.execute(
                'INSERT INTO sessions (id, source, session_key, ended_at, last_activity_at, started_at) VALUES (?, ?, ?, ?, ?, ?)',
                (
                    r['id'],
                    r.get('source', 'desktop'),
                    r.get('session_key'),
                    r.get('ended_at'),
                    r.get('last_activity_at', 100.0),
                    r.get('started_at', 90.0),
                ),
            )


def test_state_enrichment_read_only_and_daily_reset(tmp_path, monkeypatch):
    env = _load_api_module(tmp_path, monkeypatch)
    client = env['client']
    profile_dir = env['profile_dir']
    clock = env['clock']
    base = '/api/plugins/butler'

    # 1. Pure read-only when DB does not exist
    res = client.get(f'{base}/state')
    assert res.status_code == 200
    body = res.json()
    assert not profile_dir.exists()
    assert body['responsibilities'] == []
    assert body['non_negotiables'] == []
    assert body['commitments'] == []
    assert body['recent_activity'] == []
    assert body['routes'] == []
    assert body['allow_gateway_injection'] is False
    assert body['detected_system_timezone'] == 'America/Chicago'
    assert body['effective_timezone'] == 'America/Chicago'
    assert 'summary' in body['cadence_info']
    assert body['quota']['used_today'] == 0

    # Also test direct alias on unprefixed router
    alias_res = env['raw_client'].get('/api/plugins/butler/state')
    assert alias_res.status_code == 200
    assert alias_res.json()['effective_timezone'] == 'America/Chicago'

    # 2. Seed a non-negotiable completed yesterday and verify GET /state resets it for today
    profile_dir.mkdir(parents=True, exist_ok=True)
    db_path = profile_dir / 'responsibilities.sqlite3'
    with ResponsibilityStore(db_path) as st:
        nn = st.create(
            '30m deep work',
            clock[0] - timedelta(days=1),
            timezone_name='America/Chicago',
            kind='non_negotiable',
            metadata={'notes': 'No Slack', 'prep_hint': 'Open spec', 'session_key': 'secret'},
        )
        st.mark_non_negotiable_done(nn['id'], '2026-11-10')
        cm = st.create(
            'Send investor update',
            clock[0] + timedelta(hours=2),
            timezone_name='America/Chicago',
            kind='commitment',
        )

    with RouteRegistry(db_path) as reg:
        reg.record_route({
            'route_id': 'sk:abc123',
            'kind': 'session_key',
            'session_key': 'desktop:priv-session',
            'origin': None,
            'platform': 'desktop',
        })

    refreshed = client.get(f'{base}/state').json()
    assert len(refreshed['non_negotiables']) == 1
    assert len(refreshed['commitments']) == 1
    nn_item = refreshed['non_negotiables'][0]
    assert nn_item['id'] == nn['id']
    assert nn_item['status'] == 'active'
    assert nn_item['done_today'] is False
    assert nn_item['completed_day'] is None
    assert nn_item['notes'] == 'No Slack'
    assert nn_item['prep_hint'] == 'Open spec'
    assert 'metadata' not in nn_item
    assert 'session_key' not in nn_item['details']

    # Sanitized route omits raw session_key
    assert len(refreshed['routes']) == 1
    assert refreshed['routes'][0]['platform'] == 'desktop'
    assert 'session_key' not in refreshed['routes'][0]
    assert any(a['action'] == 'daily_reset' for a in refreshed['recent_activity'])
    assert refreshed['commitments'][0]['id'] == cm['id']


def test_create_and_lifecycle_actions_with_state_db_route_discovery(tmp_path, monkeypatch):
    env = _load_api_module(tmp_path, monkeypatch)
    client = env['client']
    base = '/api/plugins/butler'

    # Seed active profile state.db with a cron row (ignored) and active desktop session
    _seed_state_db_sessions(env['hermes_home'] / 'state.db', [
        {'id': 'cron-1', 'source': 'cron', 'session_key': 'cron:1', 'last_activity_at': 500.0},
        {'id': 'desk-live-42', 'source': 'desktop', 'session_key': '', 'last_activity_at': 300.0},
    ])

    # Create non-negotiable without explicit due_at -> smart schedule + auto-bound route
    create_nn = client.post(f'{base}/responsibilities', json={
        'title': 'Write 500 words',
        'kind': 'non_negotiable',
        'prep_hint': 'Open draft.md',
    })
    assert create_nn.status_code == 200
    nn_item = create_nn.json()['item']
    assert nn_item['kind'] == 'non_negotiable'
    assert nn_item['done_today'] is False
    assert nn_item['route_id'] is not None
    assert nn_item['route_id'].startswith('sk:')
    assert nn_item['prep_hint'] == 'Open draft.md'

    # Complete non-negotiable -> done_today=True
    comp = client.post(f"{base}/responsibilities/{nn_item['id']}/action", json={'action': 'complete'})
    assert comp.status_code == 200
    assert comp.json()['item']['done_today'] is True
    assert comp.json()['item']['completed_day'] == '2026-11-11'

    # Uncheck / reopen -> back to active & done_today=False
    reop = client.post(f"{base}/responsibilities/{nn_item['id']}/action", json={'action': 'reopen'})
    assert reop.status_code == 200
    assert reop.json()['item']['status'] == 'active'
    assert reop.json()['item']['done_today'] is False

    # Snooze +30m, +2h, and tomorrow_morning
    snz30 = client.post(f"{base}/responsibilities/{nn_item['id']}/action", json={
        'action': 'snooze',
        'snooze_minutes': 30,
    })
    assert snz30.status_code == 200
    due_30 = datetime.fromisoformat(snz30.json()['item']['due_at'])
    assert due_30 == env['clock'][0] + timedelta(minutes=30)

    snz_tmr = client.post(f"{base}/responsibilities/{nn_item['id']}/action", json={
        'action': 'snooze',
        'until': 'tomorrow_morning',
    })
    assert snz_tmr.status_code == 200
    due_tmr = datetime.fromisoformat(snz_tmr.json()['item']['due_at'])
    assert due_tmr > env['clock'][0] + timedelta(hours=12)

    # Pause, Resume, Update, Stop, Delete on a commitment
    create_cm = client.post(f'{base}/responsibilities', json={
        'title': 'Review PR #18',
        'kind': 'commitment',
        'due_at': '2026-11-11T14:30',
        'deadline_at': '2026-11-11T17:00',
        'notes': 'Check migrations',
    })
    assert create_cm.status_code == 200
    cm_id = create_cm.json()['item']['id']

    assert client.post(f'{base}/responsibilities/{cm_id}/action', json={'action': 'pause'}).json()['item']['status'] == 'paused'
    assert client.post(f'{base}/responsibilities/{cm_id}/action', json={'action': 'resume'}).json()['item']['status'] == 'active'

    upd = client.post(f'{base}/responsibilities/{cm_id}/action', json={
        'action': 'update',
        'title': 'Review & merge PR #18',
        'notes': 'Updated note',
        'prep_hint': 'Run pytest first',
    })
    assert upd.status_code == 200
    assert upd.json()['item']['title'] == 'Review & merge PR #18'
    assert upd.json()['item']['notes'] == 'Updated note'
    assert upd.json()['item']['prep_hint'] == 'Run pytest first'

    assert client.post(f'{base}/responsibilities/{cm_id}/action', json={'action': 'stop'}).json()['item']['status'] == 'stopped'
    dele = client.post(f'{base}/responsibilities/{cm_id}/action', json={'action': 'delete'})
    assert dele.status_code == 200
    assert dele.json()['deleted'] is True
    assert client.post(f'{base}/responsibilities/{cm_id}/action', json={'action': 'complete'}).status_code == 404
    assert client.post(f'{base}/responsibilities', json={'title': '   '}).status_code == 422


def test_enable_proactive_quick_setup_and_check_in_now(tmp_path, monkeypatch):
    env = _load_api_module(tmp_path, monkeypatch)
    client = env['client']
    base = '/api/plugins/butler'

    _seed_state_db_sessions(env['hermes_home'] / 'state.db', [
        {'id': 'desk-main', 'source': 'desktop', 'session_key': 'desktop:main', 'last_activity_at': 999.0},
    ])

    # Empty check-in before setup does not create DB
    empty_ci = client.post(f'{base}/check-in-now', json={})
    assert empty_ci.status_code == 200
    assert empty_ci.json()['ok'] is False
    assert not (env['profile_dir'] / 'responsibilities.sqlite3').exists()

    # Run 1-click Quick Setup via /enable-proactive
    setup_res = client.post(f'{base}/enable-proactive', json={
        'tone': 'Direct',
        'persistence': 'Persistent',
        'timezone': '',  # auto-detects America/Chicago
        'applyQuietPreset': True,
        'quietEnabled': True,
        'quietStart': '22:00',
        'quietEnd': '07:30',
        'dailyLimit': 15,
        'nonNegotiables': [
            '45m deep work',
            'Inbox zero',
            '30m workout',
            'inbox zero',  # duplicate ignored
        ],
    })
    assert setup_res.status_code == 200
    data = setup_res.json()
    assert data['ok'] is True
    assert data['seeded_count'] == 3
    assert data['settings']['notificationsEnabled'] is True
    assert data['settings']['paused'] is False
    assert data['settings']['tone'] == 'Direct'
    assert data['settings']['persistence'] == 'Persistent'
    assert data['settings']['timezone'] == 'America/Chicago'
    assert data['settings']['applySystemControls'] is True
    assert data['allow_gateway_injection'] is True
    assert env['hermes_cfg']['plugins']['entries']['butler']['enabled'] is True
    assert env['hermes_cfg']['plugins']['entries']['butler']['allow_gateway_injection'] is True
    assert len(data['routes']) == 1

    # Re-running with overlapping nonNegotiables does not duplicate existing ones
    setup_again = client.post(f'{base}/enable-proactive', json={
        'nonNegotiables': ['45m deep work', 'Evening review'],
    }).json()
    assert setup_again['seeded_count'] == 1
    assert len(setup_again['non_negotiables']) == 4

    # Check-in now fallback mode (queues due_at=now when no live in-process runner)
    queued_ci = client.post(f'{base}/check-in-now', json={})
    assert queued_ci.status_code == 200
    assert queued_ci.json()['ok'] is True
    assert queued_ci.json()['mode'] == 'queued_due_now'
    assert queued_ci.json()['queued_count'] == 4

    # Check-in now live runner mode when active runner is registered
    calls = []

    class FakeRunner:
        directory = env['profile_dir']

        def trigger_now(self, item_id=None):
            calls.append(item_id)
            return {'triggered': True, 'woken_items': 2, 'injected_batches': 1, 'reason': 'ok'}

    env['api']._runner.set_active_runner(FakeRunner())
    try:
        live_ci = client.post(f'{base}/check-in-now', json={'item_id': 'nn-custom'})
        assert live_ci.status_code == 200
        assert live_ci.json()['ok'] is True
        assert live_ci.json()['mode'] == 'live_runner'
        assert calls == ['nn-custom']
    finally:
        env['api']._runner.set_active_runner(None)


def test_desktop_plugin_js_passes_admission_lint_and_sync():
    desktop_js = BUTLER_ROOT / 'desktop' / 'plugin.js'
    preview_js = WORKSPACE_ROOT / 'desktop-preview' / 'proactive-assistant' / 'plugin.js'
    isolated_js = WORKSPACE_ROOT / 'isolated' / 'hermes-home' / 'desktop-plugins' / 'proactive-assistant' / 'plugin.js'

    src = desktop_js.read_text()
    # Local dev workspace copies (absent in a clean checkout) must stay in sync.
    for copy in (preview_js, isolated_js):
        if copy.exists():
            assert copy.read_text() == src

    # Validate against Hermes Desktop static admission rules when a Hermes checkout is available.
    candidates = [
        WORKSPACE_ROOT / 'isolated' / 'hermes-home' / 'hermes-agent' / 'hermes_cli' / 'plugin_validate_desktop.py',
        Path.home() / '.hermes' / 'hermes-agent' / 'hermes_cli' / 'plugin_validate_desktop.py',
    ]
    validate_path = next((c for c in candidates if c.exists()), None)
    if validate_path is not None:
        spec = importlib.util.spec_from_file_location('_test_pvd', validate_path)
        pvd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pvd)
        assert pvd.desktop_surface_findings(src) == []
        assert pvd.desktop_review_findings(src) == []

    # Verify clean relative ctx.rest paths (no double-prefixed /api/plugins/butler)
    assert '/api/plugins/butler' not in src
    # Real SDK: ctx.rest is a function (path, {method, body}); .get/.post/.put do not exist.
    assert 'ctx.rest.' not in src
    assert 'rest(path, body === undefined ? { method } : { method, body })' in src
    for expected in (
        "restCall(ctx, 'GET', '/state')",
        "restCall(ctx, 'PUT', '/settings'",
        "restCall(ctx, 'POST', '/detect-timezone'",
        "restCall(ctx, 'POST', '/preset'",
        "restCall(ctx, 'POST', '/responsibilities'",
        "restCall(ctx, 'POST', '/enable-proactive'",
        "restCall(ctx, 'POST', '/check-in-now'",
        'ctx.setInterval',
        'Non-Negotiables & Commitments',
        'Quick Setup',
        'Persistent · model-chosen timing',
        'awaiting next touch decision',
        'Phone Calling & Voice Escalation',
        '/phone/plans/',
        '/phone/calls/',
        '/phone/kill-switch',
    ):
        assert expected in src, f'Missing expected UI contract token: {expected}'

    # Verify dashboard/manifest.json hides empty Web Dashboard tab
    import json as _json
    manifest_data = _json.loads((BUTLER_ROOT / 'dashboard' / 'manifest.json').read_text())
    assert manifest_data.get('tab', {}).get('hidden') is True

    # Verify valid ESM syntax via node --check
    proc = subprocess.run(['node', '--check', str(desktop_js)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_display_name_paused_check_in_and_real_runner_trigger(tmp_path, monkeypatch):
    from butler.runner import Runner
    from butler.store import ResponsibilityStore

    env = _load_api_module(tmp_path, monkeypatch)
    client = env['client']
    base = '/api/plugins/butler'

    _seed_state_db_sessions(env['hermes_home'] / 'state.db', [
        {'id': 'desk-main', 'source': 'desktop', 'session_key': 'desktop:main', 'last_activity_at': 999.0},
    ])

    setup = client.post(f'{base}/enable-proactive', json={
        'displayName': '  Hayk  ',
        'timezone': 'America/Chicago',
        'nonNegotiables': ['Morning walk'],
    }).json()
    assert setup['settings']['displayName'] == 'Hayk'
    nn_id = setup['non_negotiables'][0]['id']

    # Pause blocks check-in-now
    paused_settings = dict(setup['settings'], paused=True)
    assert client.put(f'{base}/settings', json=paused_settings).status_code == 200
    paused_ci = client.post(f'{base}/check-in-now', json={}).json()
    assert paused_ci['ok'] is False
    assert paused_ci['mode'] == 'paused'
    assert paused_ci['reason'] == 'policy_blocked'

    # Resume and wire a real Runner instance
    assert client.put(f'{base}/settings', json=dict(setup['settings'], paused=False)).status_code == 200

    injected = []

    class DummyCtx:
        def inject_message(self, *args, **kwargs):
            injected.append((args, kwargs))
            return True

    real_runner = Runner(
        DummyCtx(),
        lambda: ResponsibilityStore(env['profile_dir'] / 'responsibilities.sqlite3'),
        directory=env['profile_dir'],
        now_fn=env['api']._now_fn,
    )
    env['api']._runner.set_active_runner(real_runner)
    try:
        # Missing item -> 404
        nf = client.post(f'{base}/check-in-now', json={'item_id': 'nonexistent'})
        assert nf.status_code == 404

        # Real runner trigger_now fires and check_in_now reports live_runner ok=True
        ok_ci = client.post(f'{base}/check-in-now', json={}).json()
        assert ok_ci['ok'] is True
        assert ok_ci['triggered'] is True
        assert ok_ci['mode'] == 'live_runner'
        assert len(injected) == 1

        # Force item into submitted status and verify manual check-in-now reopens & fires
        with ResponsibilityStore(env['profile_dir'] / 'responsibilities.sqlite3') as st:
            cur_row = st.get(nn_id)
            assert cur_row is not None
            # Model-driven follow-ups may already leave the item submitted
            # after the first live wake; only force it when still active.
            if cur_row['status'] == 'active':
                due = cur_row['due_at']
                claimed = st.claim_due(due, lease_seconds=60)[0]
                st.advance_followup(nn_id, claimed['claim_token'], due, next_due_at=None, stage=3, next_status='submitted')
            sub_row = st.get(nn_id)
            assert sub_row is not None and sub_row['status'] == 'submitted'

        sub_ci = client.post(f'{base}/check-in-now', json={'item_id': nn_id}).json()
        assert sub_ci['ok'] is True
        assert sub_ci['mode'] == 'live_runner'
        assert len(injected) == 2
    finally:
        env['api']._runner.set_active_runner(None)


def test_phone_dashboard_endpoints_masking_and_double_gate(tmp_path, monkeypatch):
    from butler.phone import PhoneStore
    from butler.settings import load as load_settings

    env = _load_api_module(tmp_path, monkeypatch)
    client = env['client']
    prof = env['profile_dir']
    now_dt = env['clock'][0]

    raw_owner = '+13125550199'
    raw_third_party = '+14155550142'

    # Save phone preferences via PUT /settings; response must mask ownerPhone
    s = defaults()
    s.update({
        'phoneEnabled': True,
        'ownerRingEnabled': True,
        'ownerPhone': raw_owner,
        'maxRingsPerHour': 4,
        'minRingGapSeconds': 180,
    })
    r_put = client.put('/api/plugins/butler/settings', json=s)
    assert r_put.status_code == 200
    put_body = r_put.json()
    assert raw_owner not in str(put_body)
    assert put_body['ownerPhone'].endswith('0199')
    assert '*' in put_body['ownerPhone']
    assert load_settings(prof)['ownerPhone'] == raw_owner

    # Saving back the masked ownerPhone preserves raw E.164 on disk
    put_body['maxRingsPerHour'] = 5
    r_put2 = client.put('/api/plugins/butler/settings', json=put_body)
    assert r_put2.status_code == 200
    assert load_settings(prof)['ownerPhone'] == raw_owner
    assert load_settings(prof)['maxRingsPerHour'] == 5

    # Draft two outbound plans directly in PhoneStore
    ps = PhoneStore(prof)
    p1 = ps.draft_plan(
        to_number=raw_third_party,
        recipient_label='Clinic Desk',
        objective='Confirm Friday checkup time',
        ai_disclosure_opening='Hello, this is an automated AI assistant calling from Butler on behalf of the patient.',
        now=now_dt,
    )
    p2 = ps.draft_plan(
        to_number=raw_third_party,
        recipient_label='Pharmacy',
        objective='Check refill readiness',
        ai_disclosure_opening='Hello, this is an automated AI assistant calling from Butler.',
        now=now_dt,
    )

    # /state returns masked phone state with zero raw phone numbers
    r_state = client.get('/api/plugins/butler/state')
    assert r_state.status_code == 200
    state_json = r_state.json()
    assert raw_owner not in str(state_json)
    assert raw_third_party not in str(state_json)
    phone = state_json['phone']
    assert phone['phone_enabled'] is True
    assert len(phone['pending_plans']) == 2

    # Approve with wrong SHA-256 -> 409 conflict
    r_bad_sha = client.post(
        f"/api/plugins/butler/phone/plans/{p1['plan_id']}/approve",
        json={'sha256': '0' * 64},
    )
    assert r_bad_sha.status_code == 409
    assert r_bad_sha.json()['detail']['code'] == 'script_sha256_mismatch'

    # Approve with exact SHA-256 -> 200 approved
    r_approve = client.post(
        f"/api/plugins/butler/phone/plans/{p1['plan_id']}/approve",
        json={'sha256': p1['script_sha256']},
    )
    assert r_approve.status_code == 200
    assert r_approve.json()['plan']['status'] == 'approved'
    assert raw_third_party not in str(r_approve.json())

    # Reject second plan -> 200 rejected
    r_reject = client.post(
        f"/api/plugins/butler/phone/plans/{p2['plan_id']}/reject",
        json={'reason': 'No longer needed'},
    )
    assert r_reject.status_code == 200
    assert r_reject.json()['plan']['status'] == 'rejected'

    # Place p1 with fake dispatch fn, then cancel active call via API
    placed = ps.place_call(
        p1['plan_id'],
        now=now_dt,
        env_override={
            'LIVEKIT_URL': 'wss://example.livekit.cloud',
            'LIVEKIT_API_KEY': 'test_key',
            'LIVEKIT_API_SECRET': 'test_secret_0123456789abcdef',
            'LIVEKIT_SIP_TRUNK_ID': 'ST_test123',
        },
        dispatch_fn=lambda **kw: {'dispatch_id': 'AD_test1'},
    )
    r_cancel = client.post(
        f"/api/plugins/butler/phone/calls/{placed['call_id']}/cancel",
        json={'reason': 'User cancelled'},
    )
    assert r_cancel.status_code == 200
    assert r_cancel.json()['call']['status'] == 'cancelled'

    # Engage and clear master phone kill-switch
    r_stop = client.post(
        '/api/plugins/butler/phone/kill-switch',
        json={'action': 'stop', 'reason': 'Testing emergency stop'},
    )
    assert r_stop.status_code == 200
    assert r_stop.json()['phone']['phone_stopped'] is True

    r_resume = client.post(
        '/api/plugins/butler/phone/kill-switch',
        json={'action': 'resume'},
    )
    assert r_resume.status_code == 200
    assert r_resume.json()['phone']['phone_stopped'] is False
