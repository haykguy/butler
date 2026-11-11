"""Offline synthetic replay; scripted policy inputs are NOT model outputs.

This evaluates fixture plumbing and existing validation, not model quality or
actual cross-session reconciliation. No gateway, network, or live data is used.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from overseer.context import ConversationContext, ObservedEvent
from overseer.interim import interim_message
from overseer.notifications import Notification, NotificationPreferences, should_notify


@dataclass(frozen=True)
class Case:
    id: str
    text: str
    expected_send: bool  # synthetic human label, not a model prediction
    baseline_send: bool  # scripted control decision, not a model response
    judge_send: bool  # scripted control decision, not a model response


CASES = (
    Case('useful', 'Synthetic: task finished', True, True, True),
    Case('noise', 'Synthetic: still thinking', False, True, False),
    Case('miss', 'Synthetic: approval needed', True, True, False),
)


def replay(cases, judged):
    """Pass identical candidate texts through scripted control decisions."""
    decisions = []
    for case in cases:
        send = case.baseline_send
        if judged and send:
            # Explicit test controls exercise the real fail-closed parser only.
            payload = json.dumps({'send': case.judge_send, 'message': case.text})
            send = interim_message(payload) is not None
        decisions.append((case.id, send, case.expected_send))
    return tuple(decisions)


def confusion(decisions):
    return {
        'tp': sum(actual and label for _, actual, label in decisions),
        'fp': sum(actual and not label for _, actual, label in decisions),
        'fn': sum(not actual and label for _, actual, label in decisions),
        'tn': sum(not actual and not label for _, actual, label in decisions),
    }


def visible_events(events, session, seen):
    """Reference ledger only: explicit delivery, per-session event-ID dedupe.

    Queued or accepted events do not imply display. Same text under distinct
    IDs is not collapsed. Caller owns the ledger and authorization boundary.
    """
    result = []
    for event_id, target, state, text in events:
        key = (session, event_id)
        if target == session and state == 'visible' and key not in seen:
            seen.add(key)
            result.append(ObservedEvent('system_notification', text,
                          datetime(2026, 1, 1, tzinfo=timezone.utc)))
    return ConversationContext('UTC', tuple(result))


def test_same_fixture_twice_and_labeled_confusion():
    first = replay(CASES, judged=False)
    second = replay(CASES, judged=True)
    assert tuple(x[0] for x in first) == tuple(x[0] for x in second)
    assert first == replay(CASES, judged=False)
    assert second == replay(CASES, judged=True)
    assert confusion(first) == {'tp': 2, 'fp': 1, 'fn': 0, 'tn': 0}
    assert confusion(second) == {'tp': 1, 'fp': 0, 'fn': 1, 'tn': 1}


@pytest.mark.parametrize('payload', [None, '', '{', '{"send":true,"send":true,"message":"x"}',
                                    '{"send":1,"message":"x"}'])
def test_invalid_or_failed_judge_is_fail_closed(payload):
    assert interim_message(payload) is None


def test_cross_session_visibility_and_repeated_replay():
    events = [
        ('1', 'a', 'accepted', 'Synthetic: queued'),
        ('2', 'a', 'visible', 'Synthetic: displayed'),
        ('2', 'a', 'visible', 'Synthetic: displayed'),
        ('3', 'b', 'visible', 'Synthetic: displayed'),
        ('4', 'a', 'visible', 'Synthetic: displayed'),
    ]
    seen = set()
    a = visible_events(events, 'a', seen)
    b = visible_events(events, 'b', seen)
    assert [e.text for e in a.events] == ['Synthetic: displayed', 'Synthetic: displayed']
    assert [e.text for e in b.events] == ['Synthetic: displayed']
    assert visible_events(events, 'a', seen).events == ()
    assert visible_events([('1', 'a', 'visible', 'Synthetic: queued')], 'a', seen).events[0].text == 'Synthetic: queued'


def test_policy_suppressed_notification_is_not_visibility():
    allowed = should_notify(Notification('Synthetic: notice'), NotificationPreferences(enabled=False),
                            datetime(2026, 1, 1, tzinfo=timezone.utc), 'UTC')
    assert allowed is False
    assert visible_events([('1', 'a', 'suppressed', 'Synthetic: notice')], 'a', set()).events == ()
