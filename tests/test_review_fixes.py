from datetime import datetime, timezone
import sqlite3
from zoneinfo import ZoneInfo

from butler.followup import shift_out_of_quiet_hours
from tests.test_dashboard_api import _load_api_module, _seed_state_db_sessions


def test_quiet_end_on_dst_fall_back_does_not_skip_a_day():
    tz = ZoneInfo('America/Chicago')
    cand = datetime(2026, 11, 1, 1, 15, tzinfo=tz, fold=1).astimezone(timezone.utc)
    cfg = {'quietEnabled': True, 'quietStart': '00:00', 'quietEnd': '01:30'}
    out = shift_out_of_quiet_hours(cand, cfg, 'America/Chicago')
    assert out == datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)


def test_enable_proactive_rejects_bad_daily_limit_with_422(tmp_path, monkeypatch):
    env = _load_api_module(tmp_path, monkeypatch)
    res = env['client'].post('/api/plugins/butler/enable-proactive', json={'dailyLimit': 'lots'})
    assert res.status_code == 422
    res = env['client'].post('/api/plugins/butler/enable-proactive', json={'dailyLimit': True})
    assert res.status_code == 422


def test_state_db_discovery_skips_group_sessions(tmp_path, monkeypatch):
    env = _load_api_module(tmp_path, monkeypatch)
    _seed_state_db_sessions(env['hermes_home'] / 'state.db', [
        {'id': 'g1', 'source': 'telegram', 'session_key': 'agent:main:telegram:group:-100', 'last_activity_at': 900.0},
        {'id': 'd1', 'source': 'telegram', 'session_key': 'agent:main:telegram:dm:42', 'last_activity_at': 100.0},
    ])
    route = env['api']._discover_route_from_state_db(env['hermes_home'] / 'state.db')
    assert route['session_key'] == 'agent:main:telegram:dm:42'
