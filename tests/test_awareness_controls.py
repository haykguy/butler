import io
import json
from datetime import datetime, timezone

import pytest

from butler.awareness import AwarenessHooks, register_awareness_and_controls
from butler.context import (
    build_lifetime_awareness_context,
    detect_ip_timezone,
    detect_system_timezone,
    format_duration,
    resolve_effective_timezone,
    summarize_session_lifetime,
)
from butler.notifications import (
    apply_system_controls_to_hermes,
    build_hermes_config_patch,
    summarize_hermes_config,
)
from butler.settings import defaults, load, quiet_human_preset, save


class FakeContext:
    def __init__(self):
        self.tools = {}
        self.hooks = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)


def test_timezone_detection_and_precedence(tmp_path):
    tz_file = tmp_path / 'timezone'
    tz_file.write_text('America/Chicago\n', encoding='utf-8')
    assert detect_system_timezone(env={}, localtime_path=tmp_path / 'missing', timezone_file=tz_file) == 'America/Chicago'
    assert detect_system_timezone(env={'TZ': ':America/New_York'}, localtime_path=tmp_path / 'missing', timezone_file=tz_file) == 'America/New_York'
    assert detect_system_timezone(env={'TZ': '../bad'}, localtime_path=tmp_path / 'missing', timezone_file=tmp_path / 'missing2') == ''

    calls = []

    def fake_urlopen(req, timeout=3.0):
        calls.append(req.full_url)
        if 'ipapi.co' in req.full_url:
            raise OSError('offline primary')
        return io.BytesIO(b'{"timezone": "America/Los_Angeles"}')

    assert detect_ip_timezone(urlopen=fake_urlopen) == 'America/Los_Angeles'
    assert len(calls) == 2

    s = defaults()
    assert resolve_effective_timezone(s, config_timezone='', system_timezone='') == ('UTC', 'utc_fallback')
    assert resolve_effective_timezone(s, config_timezone='', system_timezone='America/Chicago') == ('America/Chicago', 'system_detected')
    assert resolve_effective_timezone(s, config_timezone='Europe/London', system_timezone='America/Chicago') == ('Europe/London', 'hermes_config')
    s['timezone'] = 'Asia/Tokyo'
    assert resolve_effective_timezone(s, config_timezone='Europe/London', system_timezone='America/Chicago') == ('Asia/Tokyo', 'profile')


def test_session_lifetime_summary_and_context_injection(tmp_path):
    now = datetime(2026, 11, 11, 14, 30, 0, tzinfo=timezone.utc)  # 08:30 CST
    now_ts = now.timestamp()

    first = summarize_session_lifetime([{'role': 'user', 'content': 'hi', 'timestamp': now_ts}], now, is_first_turn=True)
    assert first['is_first_turn'] is True
    assert first['user_turns'] == 1

    history = [
        {'role': 'system', 'content': 'sys', 'timestamp': now_ts - 999999},
        {'role': 'user', 'content': 'first', 'timestamp': now_ts - 7200},
        {'role': 'assistant', 'content': '', 'timestamp': now_ts - 7190},
        {'role': 'tool', 'content': 'ok', 'timestamp': now_ts - 7185},
        {'role': 'assistant', 'content': 'done', 'timestamp': now_ts - 3660},
        {'role': 'user', 'content': 'back now', 'timestamp': now_ts - 2},
    ]
    life = summarize_session_lifetime(history, now, is_first_turn=False)
    assert life['is_first_turn'] is False
    assert life['user_turns'] == 2
    assert life['session_age_human'] == '2h'
    assert life['since_previous_user_human'] == '2h'
    assert life['since_last_assistant_human'] == '1h 1m'
    assert format_duration(45) == '45s'
    assert format_duration(90000) == '1d 1h'

    cfg = dict(defaults(), timezone='America/Chicago', quietEnabled=True, quietStart='22:00', quietEnd='07:00')
    save(tmp_path, cfg)
    hooks = AwarenessHooks(
        directory=lambda: tmp_path,
        load_state=lambda: {'available': True, 'timezone': ''},
        now_fn=lambda: now,
        system_tz_fn=lambda: 'America/Chicago',
    )
    injected = hooks.pre_llm_call(
        conversation_history=history,
        is_first_turn=False,
        platform='photon',
        parent_session_id='',
    )
    assert injected is not None
    ctx_text = injected['context']
    assert '[Butler Live Time & Session Lifetime on photon]' in ctx_text
    assert '2026-11-11 08:30:00' in ctx_text
    assert 'America/Chicago' in ctx_text
    assert 'session age 2h' in ctx_text
    assert "elapsed since user's previous message 2h" in ctx_text
    assert 'elapsed since your last reply 1h 1m' in ctx_text
    assert 'quiet_hours=inactive (window 22:00–07:00)' in ctx_text
    assert 'warnings_muted=True' in ctx_text

    # Subagent turns must be skipped cleanly
    assert hooks.pre_llm_call(conversation_history=history, parent_session_id='parent-123') is None

    # Pause or disabling lifetimeAwarenessEnabled stops injection
    save(tmp_path, dict(cfg, paused=True))
    assert hooks.pre_llm_call(conversation_history=history) is None
    save(tmp_path, dict(cfg, paused=False, lifetimeAwarenessEnabled=False))
    assert hooks.pre_llm_call(conversation_history=history) is None
    assert build_lifetime_awareness_context(dict(cfg, paused=True), now=now) is None


def test_hermes_load_controls_patch_and_tool_privacy_boundary(tmp_path):
    live_store = {
        'display': {
            'tool_progress': 'all',
            'platforms': {
                'photon': {'tool_progress': 'all', 'suppress_warning_notifications': False},
            },
        },
        'platforms': {
            'photon': {'enabled': True, 'token': 'secret-do-not-leak', 'gateway_restart_notification': True},
            'telegram': {'enabled': True, 'gateway_restart_notification': True},
        },
    }
    saved_calls = []

    def fake_loader():
        return json.loads(json.dumps(live_store))

    def fake_saver(patch, *, preserve_keys=None, merge_existing=False):
        saved_calls.append((patch, preserve_keys, merge_existing))
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(live_store.get(k), dict):
                for sk, sv in v.items():
                    if isinstance(sv, dict) and isinstance(live_store[k].get(sk), dict):
                        live_store[k][sk].update(sv)
                    else:
                        live_store[k][sk] = sv
            else:
                live_store[k] = v

    ctx = FakeContext()
    register_awareness_and_controls(
        ctx,
        directory=lambda: tmp_path,
        config_loader=fake_loader,
        config_saver=fake_saver,
        ip_tz_fn=lambda: 'America/Chicago',
        system_tz_fn=lambda: 'America/Chicago',
    )
    handler = ctx.tools['butler_controls']['handler']

    # Timezone detection onboarding (OS only vs IP)
    det_local = json.loads(handler({'action': 'detect_timezone'}))
    assert det_local['ok'] is True
    assert det_local['detected_system_timezone'] == 'America/Chicago'
    assert det_local['detected_ip_timezone'] == ''

    det_ip = json.loads(handler({'action': 'detect_timezone', 'use_ip': True}))
    assert det_ip['detected_ip_timezone'] == 'America/Chicago'

    # Privacy gate: model cannot enable activityEnabled or notificationsEnabled
    denied = json.loads(handler({'action': 'update', 'settings': {'activityEnabled': True}}))
    assert denied['ok'] is False
    assert 'activityEnabled' in denied['error']
    assert load(tmp_path)['activityEnabled'] is False

    # Apply quiet_human preset -> writes profile AND syncs Hermes config
    applied = json.loads(handler({'action': 'apply_preset', 'preset': 'quiet_human'}))
    assert applied['ok'] is True
    assert applied['hermes_sync']['applied'] is True
    assert len(saved_calls) == 1
    _, preserve, merge_existing = saved_calls[0]
    assert merge_existing is True
    assert ('display', 'suppress_warning_notifications') in preserve
    assert ('skills', 'creation_nudge_interval') in preserve
    assert ('platforms', 'photon', 'gateway_restart_notification') in preserve

    summary = summarize_hermes_config(live_store)
    assert 'secret-do-not-leak' not in json.dumps(summary)
    assert summary['suppress_warning_notifications'] is True
    assert summary['credits_notices'] is False
    assert summary['tool_progress'] == 'off'
    assert summary['interim_assistant_messages'] is False
    assert summary['skill_creation_nudge_interval'] == 0
    assert summary['gateway_message_timestamps'] is True
    assert summary['gateway_restart_notification_by_platform'] == {'photon': False, 'telegram': False}
    assert live_store['display']['platforms']['photon']['suppress_warning_notifications'] is True

    # Status reflects synced Hermes config
    st = json.loads(handler({'action': 'status'}))
    assert st['ok'] is True
    assert st['effective_timezone'] == 'America/Chicago'
    assert st['hermes_live_config']['suppress_warning_notifications'] is True

    # Legacy 9-key profile JSON still loads cleanly via migration
    legacy_dir = tmp_path / 'legacy'
    legacy_dir.mkdir()
    legacy_payload = {
        k: defaults()[k] for k in (
            'timezone', 'tone', 'persistence', 'dailyLimit',
            'quietEnabled', 'quietStart', 'quietEnd',
            'activityEnabled', 'notificationsEnabled',
        )
    }
    (legacy_dir / 'settings.json').write_text(json.dumps(legacy_payload), encoding='utf-8')
    migrated = load(legacy_dir)
    assert migrated['lifetimeAwarenessEnabled'] is True
    assert migrated['suppressWarningNotifications'] is True

    # Direct patch & apply helper check
    patch, pkeys = build_hermes_config_patch(quiet_human_preset(), live_store)
    assert patch['skills']['creation_nudge_interval'] == 0
    assert ('gateway', 'message_timestamps', 'enabled') in pkeys
    res = apply_system_controls_to_hermes(quiet_human_preset(), config_loader=fake_loader, config_saver=fake_saver)
    assert res['applied'] is True
