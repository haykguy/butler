import hashlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from butler.policy import Policy
from butler.routes import (
    RouteRegistry,
    capture_admitted_gateway_origin,
    capture_bound_turn_route,
)
from butler.settings import defaults, save


def _install_fake_session_context(monkeypatch, *, engaged=True, values=None):
    unset = object()
    data = dict(values or {})
    gw_pkg = types.ModuleType('gateway')
    gw_pkg.__path__ = []
    sc_mod = types.ModuleType('gateway.session_context')
    import os as _os

    def _get_session_env(name, default=''):
        # Mirrors gateway.session_context.get_session_env: a bound ContextVar wins,
        # else os.environ, else default. Nothing is bound unless engaged.
        # set_session_vars binds every session var (omitted ones to ''), so with a
        # bound session nothing falls through to the environment.
        if engaged and data:
            return data.get(name, '')
        return _os.getenv(name, default)

    sc_mod.get_session_env = _get_session_env
    monkeypatch.setitem(sys.modules, 'gateway', gw_pkg)
    monkeypatch.setitem(sys.modules, 'gateway.session_context', sc_mod)
    return unset


def test_capture_bound_turn_route_never_falls_back_to_env_or_plugin_session(monkeypatch):
    # Populate untrusted ambient fallbacks
    monkeypatch.setenv('HERMES_SESSION_KEY', 'agent:default:telegram:dm:spoofed')
    monkeypatch.setenv('HERMES_SESSION_PLATFORM', 'telegram')
    cli_pkg = types.ModuleType('hermes_cli')
    cli_pkg.__path__ = []
    ps_mod = types.ModuleType('hermes_cli.plugin_session')
    ps_mod.session_key = lambda: 'agent:default:telegram:dm:spoofed'
    monkeypatch.setitem(sys.modules, 'hermes_cli', cli_pkg)
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_session', ps_mod)

    # 1. Missing gateway.session_context -> fails closed
    monkeypatch.setitem(sys.modules, 'gateway.session_context', None)
    assert capture_bound_turn_route() is None

    # 2. session_context_engaged() is False -> fails closed even if key appears set
    _install_fake_session_context(
        monkeypatch,
        engaged=False,
        values={'HERMES_SESSION_KEY': 'agent:default:telegram:dm:123'},
    )
    assert capture_bound_turn_route() is None

    # 3. Engaged, but HERMES_SESSION_KEY is _UNSET -> fails closed (no os.environ fallback!)
    _install_fake_session_context(monkeypatch, engaged=True, values={})
    assert capture_bound_turn_route() is None

    # 4. Engaged, but HERMES_SESSION_KEY is whitespace -> fails closed
    _install_fake_session_context(
        monkeypatch,
        engaged=True,
        values={'HERMES_SESSION_KEY': '   '},
    )
    assert capture_bound_turn_route() is None

    # 5. Engaged with valid session_key, but HERMES_CRON_SESSION == '1' -> fails closed
    _install_fake_session_context(
        monkeypatch,
        engaged=True,
        values={
            'HERMES_SESSION_KEY': 'agent:default:telegram:dm:123',
            'HERMES_CRON_SESSION': '1',
            'HERMES_SESSION_PLATFORM': 'telegram',
        },
    )
    assert capture_bound_turn_route() is None

    # 6. Engaged non-cron bound session -> succeeds with deterministic sk: route_id
    sk = 'agent:default:telegram:dm:123'
    _install_fake_session_context(
        monkeypatch,
        engaged=True,
        values={
            'HERMES_SESSION_KEY': f'  {sk}  ',
            'HERMES_CRON_SESSION': '',
            'HERMES_SESSION_PLATFORM': 'telegram',
            'HERMES_SESSION_CHAT_ID': '123',
            'HERMES_SESSION_CHAT_TYPE': 'dm',
            'HERMES_SESSION_USER_ID': '456',
        },
    )
    route = capture_bound_turn_route()
    expected_id = 'sk:' + hashlib.sha256(sk.encode('utf-8')).hexdigest()[:16]
    assert route == {
        'route_id': expected_id,
        'kind': 'session_key',
        'session_key': sk,
        'origin': None,
        'platform': 'telegram',
    }

    # 6b. Bound value identical to the ambient env value is ambiguous -> fails closed
    _install_fake_session_context(
        monkeypatch,
        engaged=True,
        values={'HERMES_SESSION_KEY': 'agent:default:telegram:dm:spoofed'},
    )
    assert capture_bound_turn_route() is None

    # 7. Falls back from empty platform to source, then 'local'
    _install_fake_session_context(
        monkeypatch,
        engaged=True,
        values={'HERMES_SESSION_KEY': 'desk-1', 'HERMES_SESSION_SOURCE': 'desktop'},
    )
    assert capture_bound_turn_route()['platform'] == 'desktop'

    _install_fake_session_context(
        monkeypatch,
        engaged=True,
        values={'HERMES_SESSION_KEY': 'cli-1'},
    )
    assert capture_bound_turn_route()['platform'] == 'local'


def test_capture_bound_turn_route_with_isolated_hermes_source_if_present(monkeypatch):
    hermes_root = Path(__file__).resolve().parents[2] / 'isolated' / 'hermes-home' / 'hermes-agent'
    if not (hermes_root / 'gateway' / 'session_context.py').is_file():
        pytest.skip('isolated hermes-agent source not present')

    monkeypatch.syspath_prepend(str(hermes_root))
    import gateway.session_context as sc

    prev_engaged = sc._session_context_engaged
    try:
        sc.reset_session_vars()
        sc._session_context_engaged = False
        monkeypatch.setenv('HERMES_SESSION_KEY', 'env-leak-attempt')
        assert capture_bound_turn_route() is None

        tokens = sc.set_session_vars(
            platform='discord',
            chat_id='999',
            chat_type='dm',
            user_id='888',
            session_key='agent:default:discord:dm:999',
        )
        try:
            route = capture_bound_turn_route()
            assert route is not None
            assert route['session_key'] == 'agent:default:discord:dm:999'
            assert route['platform'] == 'discord'
            assert route['origin'] is None
        finally:
            sc.clear_session_vars(tokens)

        # After clear_session_vars, even with HERMES_SESSION_KEY in os.environ, returns None
        assert capture_bound_turn_route() is None
    finally:
        sc.reset_session_vars()
        sc._session_context_engaged = prev_engaged


def test_capture_admitted_gateway_origin_validation_and_shapes():
    valid_source = {
        'platform': 'telegram',
        'chat_id': '100200',
        'chat_type': 'dm',
        'user_id': '300400',
        'thread_id': 'topic-7',
    }

    # With session_key
    sk = 'agent:default:telegram:dm:100200'
    r_sk = capture_admitted_gateway_origin(platform='telegram', source=valid_source, session_key=sk)
    assert r_sk == {
        'route_id': 'sk:' + hashlib.sha256(sk.encode('utf-8')).hexdigest()[:16],
        'kind': 'session_key',
        'session_key': sk,
        'origin': {
            'platform': 'telegram',
            'chat_id': '100200',
            'chat_type': 'dm',
            'user_id': '300400',
            'thread_id': 'topic-7',
        },
        'platform': 'telegram',
    }

    # Without session_key -> kind='origin'
    r_orig = capture_admitted_gateway_origin(
        platform='telegram',
        source={**valid_source, 'thread_id': None},
        session_key='',
    )
    expected_origin = {
        'platform': 'telegram',
        'chat_id': '100200',
        'chat_type': 'dm',
        'user_id': '300400',
        'thread_id': '',
    }
    assert r_orig == {
        'route_id': 'orig:' + hashlib.sha256(json.dumps(expected_origin, sort_keys=True).encode('utf-8')).hexdigest()[:16],
        'kind': 'origin',
        'session_key': None,
        'origin': expected_origin,
        'platform': 'telegram',
    }

    # Reject invalid / unauthorized shapes
    assert capture_admitted_gateway_origin(platform='telegram', source=None) is None
    assert capture_admitted_gateway_origin(platform='', source=valid_source) is None
    assert capture_admitted_gateway_origin(platform='discord', source=valid_source) is None
    assert capture_admitted_gateway_origin(platform='telegram', source={**valid_source, 'chat_type': 'group'}) is None
    assert capture_admitted_gateway_origin(platform='telegram', source={**valid_source, 'shared_session': True}) is None
    assert capture_admitted_gateway_origin(platform='telegram', source={**valid_source, 'chat_id': '  '}) is None
    assert capture_admitted_gateway_origin(platform='telegram', source={**valid_source, 'user_id': None}) is None
    assert capture_admitted_gateway_origin(platform='telegram', source={**valid_source, 'chat_id': True}) is None


def test_route_registry_crud_preference_and_origin_preservation(tmp_path):
    db_path = tmp_path / 'routes.sqlite3'
    admitted = capture_admitted_gateway_origin(
        platform='telegram',
        source={'platform': 'telegram', 'chat_id': '11', 'chat_type': 'dm', 'user_id': '22'},
        session_key='agent:default:telegram:dm:11',
    )
    origin_only = capture_admitted_gateway_origin(
        platform='discord',
        source={'platform': 'discord', 'chat_id': '33', 'chat_type': 'dm', 'user_id': '44'},
    )

    with RouteRegistry(db_path) as reg:
        assert reg.preferred_route() is None
        assert reg.list_routes() == []

        rid1 = reg.record_route(admitted, preferred=True)
        assert rid1 == admitted['route_id']
        stored1 = reg.get_route(rid1)
        assert stored1['is_preferred'] is True
        assert stored1['origin']['chat_id'] == '11'

        # Recording a second route with preferred=False keeps first route preferred
        rid2 = reg.record_route(origin_only, preferred=False)
        assert reg.preferred_route()['route_id'] == rid1
        routes = reg.list_routes()
        assert [r['route_id'] for r in routes] == [rid1, rid2]

        # Refreshing rid1 from a bound turn (where origin is None) preserves existing origin_json
        turn_refresh = {
            'route_id': rid1,
            'kind': 'session_key',
            'session_key': 'agent:default:telegram:dm:11',
            'origin': None,
            'platform': 'telegram',
        }
        reg.record_route(turn_refresh, preferred=True)
        assert reg.get_route(rid1)['origin']['chat_id'] == '11'

        # Promoting rid2 switches preference
        reg.record_route(origin_only, preferred=True)
        assert reg.preferred_route()['route_id'] == rid2
        assert reg.get_route(rid1)['is_preferred'] is False

        # Removing preferred route falls back to newest updated_at route
        assert reg.remove_route(rid2) is True
        assert reg.remove_route(rid2) is False
        fallback = reg.preferred_route()
        assert fallback['route_id'] == rid1

    # Persists across reopen
    with RouteRegistry(db_path) as reg2:
        assert reg2.get_route(rid1)['session_key'] == 'agent:default:telegram:dm:11'


def test_policy_quiet_window_end_same_day_overnight_and_fallback_tz(tmp_path):
    policy = Policy(tmp_path)

    # Overnight window 22:00 -> 07:00 in America/Chicago (CST = UTC-6 in Jan)
    save(
        tmp_path,
        dict(
            defaults(),
            notificationsEnabled=True,
            timezone='America/Chicago',
            quietEnabled=True,
            quietStart='22:00',
            quietEnd='07:00',
        ),
    )
    # 2026-01-01 22:00 CST == 2026-01-02 04:00 UTC -> inside quiet hours, ends 2026-01-02 07:00 CST (13:00 UTC)
    at_start = datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc)
    expected_end = datetime(2026, 1, 2, 13, 0, tzinfo=timezone.utc)
    assert policy.quiet_window_end(at_start) == expected_end

    # After midnight local: 2026-01-02 02:30 CST == 2026-01-02 08:30 UTC -> still ends 2026-01-02 13:00 UTC
    after_midnight = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
    assert policy.quiet_window_end(after_midnight) == expected_end

    # Exact end boundary [quietStart, quietEnd): 07:00 CST (13:00 UTC) -> None
    assert policy.quiet_window_end(expected_end) is None

    # Same-day window 01:00 -> 06:00 with empty settings timezone + fallback_timezone
    save(
        tmp_path,
        dict(
            defaults(),
            notificationsEnabled=True,
            timezone='',
            quietEnabled=True,
            quietStart='01:00',
            quietEnd='06:00',
        ),
    )
    assert policy.quiet_window_end(datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc)) is None
    assert policy.quiet_window_end(
        datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc),
        fallback_timezone='UTC',
    ) == datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc)


def test_policy_evaluate_wake_and_quota_status_non_consuming(tmp_path):
    policy = Policy(tmp_path)
    now = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)  # 12:00 CST

    # 1. Invalid naive datetime
    bad = policy.evaluate_wake(datetime(2026, 1, 1, 18, 0))
    assert bad['allowed'] is False
    assert bad['reason'] == 'invalid_now'

    # 2. Default settings -> notifications_disabled
    ev_default = policy.evaluate_wake(now)
    assert ev_default['allowed'] is False
    assert ev_default['reason'] == 'notifications_disabled'

    # 3. Timezone required when dailyLimit or quietEnabled is set without timezone
    save(tmp_path, dict(defaults(), notificationsEnabled=True, dailyLimit=2))
    ev_tz = policy.evaluate_wake(now)
    assert ev_tz['allowed'] is False
    assert ev_tz['reason'] == 'timezone_required'
    assert ev_tz['defer_until'] is None

    # Fallback timezone satisfies evaluate_wake when explicitly supplied
    ev_fb = policy.evaluate_wake(now, fallback_timezone='America/Chicago')
    assert ev_fb['allowed'] is True
    assert ev_fb['reason'] == 'ok'

    # 4. Active quota + non-consuming evaluate_wake + daily_limit_reached -> next local day 08:00
    save(
        tmp_path,
        dict(
            defaults(),
            activityEnabled=True,
            notificationsEnabled=True,
            timezone='America/Chicago',
            dailyLimit=2,
        ),
    )
    for _ in range(5):
        ev_ok = policy.evaluate_wake(now)
        assert ev_ok == {
            'allowed': True,
            'reason': 'ok',
            'defer_until': None,
            'used_today': 0,
            'daily_limit': 2,
            'in_quiet_hours': False,
        }

    assert policy.reserve_wake(now) is True
    status_mid = policy.wake_quota_status(now)
    assert status_mid == {
        'used_today': 1,
        'daily_limit': 2,
        'remaining_today': 1,
        'in_quiet_hours': False,
        'quiet_until': None,
        'paused': False,
        'notifications_enabled': True,
        'allowed_now': True,
        'reason': 'ok',
    }

    assert policy.reserve_wake(now) is True
    ev_capped = policy.evaluate_wake(now)
    # Next local day (2026-01-02) at 08:00 CST == 14:00 UTC
    assert ev_capped == {
        'allowed': False,
        'reason': 'daily_limit_reached',
        'defer_until': datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc),
        'used_today': 2,
        'daily_limit': 2,
        'in_quiet_hours': False,
    }

    # 5. With quietEnabled=True, daily_limit_reached defers to next local day quietEnd (07:30 CST == 13:30 UTC)
    save(
        tmp_path,
        dict(
            defaults(),
            notificationsEnabled=True,
            timezone='America/Chicago',
            dailyLimit=2,
            quietEnabled=True,
            quietStart='22:00',
            quietEnd='07:30',
        ),
    )
    ev_capped_quiet = policy.evaluate_wake(now)
    assert ev_capped_quiet['allowed'] is False
    assert ev_capped_quiet['reason'] == 'daily_limit_reached'
    assert ev_capped_quiet['defer_until'] == datetime(2026, 1, 2, 13, 30, tzinfo=timezone.utc)

    # 6. During quiet hours, quiet_hours takes precedence and reports quiet_until in wake_quota_status
    quiet_now = datetime(2026, 1, 2, 4, 30, tzinfo=timezone.utc)  # 2026-01-01 22:30 CST
    ev_quiet = policy.evaluate_wake(quiet_now)
    expected_quiet_end = datetime(2026, 1, 2, 13, 30, tzinfo=timezone.utc)
    assert ev_quiet['allowed'] is False
    assert ev_quiet['reason'] == 'quiet_hours'
    assert ev_quiet['defer_until'] == expected_quiet_end
    assert ev_quiet['in_quiet_hours'] is True

    q_status = policy.wake_quota_status(quiet_now)
    assert q_status['in_quiet_hours'] is True
    assert q_status['quiet_until'] == expected_quiet_end.isoformat()
    assert q_status['remaining_today'] == 0
    assert q_status['allowed_now'] is False
    assert q_status['reason'] == 'quiet_hours'

    # 7. Paused state reports reason='paused' while preserving quota telemetry
    save(
        tmp_path,
        dict(
            defaults(),
            notificationsEnabled=True,
            paused=True,
            timezone='America/Chicago',
            dailyLimit=5,
        ),
    )
    p_status = policy.wake_quota_status(now)
    assert p_status['paused'] is True
    assert p_status['allowed_now'] is False
    assert p_status['reason'] == 'paused'
    assert p_status['used_today'] == 2
    assert p_status['remaining_today'] == 3
