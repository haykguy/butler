from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from overseer.settings import defaults, save
from overseer.policy import Policy


def test_opt_in_pause_and_daily_reservations(tmp_path):
    now = datetime(2026, 1, 1, 18, tzinfo=timezone.utc)
    policy = Policy(tmp_path)
    assert not policy.capture_allowed()
    assert not policy.reserve_wake(now)
    settings = dict(defaults(), activityEnabled=True, notificationsEnabled=True,
                    timezone='America/Chicago', dailyLimit=1)
    save(tmp_path, settings)
    assert policy.capture_allowed()
    assert policy.reserve_wake(now)
    assert not policy.reserve_wake(now)
    assert policy.reserve_wake(datetime(2026, 1, 2, 6, tzinfo=timezone.utc))
    save(tmp_path, dict(settings, paused=True))
    assert not policy.capture_allowed()
    assert not policy.reserve_wake(datetime(2026, 1, 3, 18, tzinfo=timezone.utc))


def test_quiet_hours_and_invalid_configuration_fail_closed(tmp_path):
    policy = Policy(tmp_path)
    data = dict(defaults(), notificationsEnabled=True, timezone='America/Chicago',
                quietEnabled=True, quietStart='22:00', quietEnd='07:00')
    save(tmp_path, data)
    assert not policy.reserve_wake(datetime(2026, 1, 2, 4, tzinfo=timezone.utc))
    assert policy.reserve_wake(datetime(2026, 1, 2, 13, tzinfo=timezone.utc))
    (tmp_path / 'settings.json').write_text('{')
    assert not policy.capture_allowed()
    assert not policy.reserve_wake(datetime(2026, 1, 2, 18, tzinfo=timezone.utc))


def test_concurrent_reservations_obey_limit(tmp_path):
    save(tmp_path, dict(defaults(), notificationsEnabled=True, timezone='UTC', dailyLimit=3))
    policy = Policy(tmp_path)
    now = datetime(2026, 1, 1, 18, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: policy.reserve_wake(now), range(30)))
    assert sum(results) == 3


def test_timezone_required_for_quiet_or_limited_wakes(tmp_path):
    save(tmp_path, dict(defaults(), notificationsEnabled=True, dailyLimit=1))
    assert not Policy(tmp_path).reserve_wake(datetime(2026, 1, 1, tzinfo=timezone.utc))
