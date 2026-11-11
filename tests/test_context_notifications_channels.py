from datetime import datetime, timezone

import pytest

from overseer.context import ConversationContext, ObservedEvent, local_time


def test_context_uses_only_supplied_observations_and_configured_zone():
    event = ObservedEvent('message', 'hello', datetime(2026, 1, 1, tzinfo=timezone.utc))
    context = ConversationContext(timezone='America/New_York', events=(event,))
    assert context.events == (event,)
    assert local_time(event.observed_at, context.timezone).hour == 19
    with pytest.raises(ValueError):
        local_time(datetime(2026, 1, 1), context.timezone)


def test_safety_notices_bypass_disabled_and_quiet_preferences():
    from datetime import time
    from overseer.notifications import Notification, NotificationPreferences, should_notify
    prefs = NotificationPreferences(enabled=False, quiet_start=time(22), quiet_end=time(8))
    now = datetime(2026, 1, 1, 23, tzinfo=timezone.utc)
    assert not should_notify(Notification('hello'), prefs, now, 'UTC')
    assert should_notify(Notification('Safety notice', safety=True), prefs, now, 'UTC')


def test_channel_operations_are_capability_checked_and_only_described():
    from overseer.channels import ChannelCapabilities, prepare_operation
    caps = ChannelCapabilities(reactions=True, threads=True)
    operation = prepare_operation(caps, 'reaction', 'message-1', '👍')
    assert operation.kind == 'reaction'
    assert operation.target == 'message-1'
    assert prepare_operation(caps, 'thread', 'message-1', 'reply').value == 'reply'
    with pytest.raises(ValueError):
        prepare_operation(caps, 'effect', 'message-1', 'confetti')
    with pytest.raises(ValueError):
        prepare_operation(caps, 'unknown', 'message-1', 'x')


def test_quiet_hours_use_user_zone_and_half_open_boundaries():
    from datetime import time
    from overseer.notifications import Notification, NotificationPreferences, should_notify
    prefs = NotificationPreferences(quiet_start=time(22), quiet_end=time(8), tone='warm', persistence='low')
    assert prefs.tone == 'warm' and prefs.persistence == 'low'
    assert not should_notify(Notification('hi'), prefs, datetime(2026, 1, 2, 3, tzinfo=timezone.utc), 'America/New_York')
    assert should_notify(Notification('hi'), prefs, datetime(2026, 1, 2, 13, tzinfo=timezone.utc), 'America/New_York')
    with pytest.raises(ValueError):
        NotificationPreferences(quiet_start=time(22))


def test_invalid_timezone_and_naive_observation_rejected():
    from zoneinfo import ZoneInfoNotFoundError
    with pytest.raises(ZoneInfoNotFoundError):
        ConversationContext(timezone='not/a-zone')
    with pytest.raises(ValueError):
        ObservedEvent('message', 'hello', datetime(2026, 1, 1))
