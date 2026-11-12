from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from butler.awareness import AwarenessHooks
from butler.context import build_lifetime_awareness_context
from butler.followup import JUDGMENT_RULES, WAKE_MARKER, build_action_first_wake_prompt
from butler.runner import Runner
from butler.settings import defaults, validate
from butler.store import ResponsibilityStore

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)


def test_engagement_counts_wakes_and_real_replies_reset(tmp_path):
    store = ResponsibilityStore(tmp_path / 'e.sqlite')
    assert store.get_engagement()['unanswered_wakes'] == 0
    store.note_wake_sent(NOW)
    store.note_wake_sent(NOW + timedelta(hours=1))
    assert store.get_engagement()['unanswered_wakes'] == 2
    store.note_user_reply(NOW + timedelta(hours=2))
    eng = store.get_engagement()
    assert eng['unanswered_wakes'] == 0 and eng['last_user_at'] == NOW + timedelta(hours=2)
    assert eng['last_wake_at'] == NOW + timedelta(hours=1)


def test_hook_resets_on_real_message_but_not_on_butler_own_wake(tmp_path):
    ResponsibilityStore(tmp_path / 'responsibilities.sqlite3').note_wake_sent(NOW)
    hooks = AwarenessHooks(directory=lambda: tmp_path, load_state=lambda: {}, now_fn=lambda: NOW,
                           system_tz_fn=lambda: 'America/Chicago')

    def unanswered():
        s = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
        try:
            return s.get_engagement()['unanswered_wakes']
        finally:
            s.close()

    hooks.pre_llm_call(user_message=f'{WAKE_MARKER} (Wednesday...)', conversation_history=[], is_first_turn=False)
    assert unanswered() == 1  # its own wake prompt is not a reply
    hooks.pre_llm_call(user_message='yeah on it', conversation_history=[], is_first_turn=False)
    assert unanswered() == 0


def test_gates_are_owner_set_and_zero_disables(tmp_path):
    store = ResponsibilityStore(tmp_path / 'g.sqlite')
    gate = Runner._engagement_gate
    cfg = dict(defaults(), maxUnanswered=2, minWakeGapMinutes=10)
    assert gate(store, cfg, NOW) is None
    store.note_wake_sent(NOW)
    assert gate(store, cfg, NOW + timedelta(minutes=5)) == 'min_wake_gap'
    assert gate(store, cfg, NOW + timedelta(minutes=11)) is None
    store.note_wake_sent(NOW + timedelta(minutes=11))
    assert gate(store, cfg, NOW + timedelta(hours=3)) == 'max_unanswered'
    store.note_user_reply(NOW + timedelta(hours=4))
    assert gate(store, cfg, NOW + timedelta(hours=5)) is None
    assert gate(store, dict(cfg, maxUnanswered=0, minWakeGapMinutes=0), NOW) is None


def test_gate_settings_validate_and_model_cannot_change_them():
    with pytest.raises(ValueError):
        validate(dict(defaults(), maxUnanswered=-1))
    with pytest.raises(ValueError):
        validate(dict(defaults(), minWakeGapMinutes=True))
    from butler.awareness import _MODEL_MUTABLE_KEYS
    assert 'maxUnanswered' not in _MODEL_MUTABLE_KEYS and 'minWakeGapMinutes' not in _MODEL_MUTABLE_KEYS


def test_wake_prompt_carries_judgment_and_engagement():
    item = {'id': 'a', 'title': 'Send invoice', 'kind': 'commitment', 'nudge_count': 2}
    prompt = build_action_first_wake_prompt(
        [item], {}, defaults(), now=NOW, effective_timezone='America/Chicago',
        engagement={'unanswered_wakes': 3, 'last_user_at': NOW - timedelta(hours=9)},
    )
    assert JUDGMENT_RULES in prompt
    for phrase in ('availability', 'Learn from reactions', 'word for word', 'go louder', 'unanswered proactive nudges since their last real message: 3'):
        assert phrase in prompt
    assert WAKE_MARKER in prompt


def test_context_tells_model_to_decide_followup_after_every_reply():
    out = build_lifetime_awareness_context(
        defaults(), now=NOW, active_responsibilities=[], engagement={'unanswered_wakes': 2},
    )
    assert 'After EVERY reply' in out and 'should I come back' in out
    assert 'bare question' in out
    assert 'Engagement: 2 unanswered' in out


def test_tick_end_to_end_wake_counts_gates_and_resets(tmp_path):
    """Real Runner.tick(): wake goes out with judgment prompt, counter rises, gate holds, real reply reopens it."""
    from butler.settings import save
    from tests.test_runner_wakes import FakeHostContext, _make_harness
    clock = [NOW]
    save(tmp_path, dict(defaults(), notificationsEnabled=True, timezone='America/Chicago',
                        maxUnanswered=1, minWakeGapMinutes=0))
    store_factory, reg_factory, routes, _ = _make_harness(tmp_path)
    routes.record_route({'route_id': 'r', 'kind': 'session_key', 'session_key': 'tui:x', 'origin': None, 'platform': 'tui'})
    db = store_factory()
    a = db.create('Send deck', NOW - timedelta(minutes=1), route_id='r')
    b = db.create('Call dentist', NOW - timedelta(minutes=1), route_id='r')
    db.close()
    ctx = FakeHostContext(accepted=True)
    runner = Runner(ctx, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: clock[0])

    assert runner.tick()['injected'] == 1
    prompt = ctx.calls[0][0]
    assert 'YOU OWN THE FOLLOW-UP' in prompt and 'JUDGMENT' in prompt and 'ENGAGEMENT' in prompt

    db = store_factory()
    assert db.get(a['id'])['status'] == 'submitted' and db.get_engagement()['unanswered_wakes'] == 1
    assert db.snooze(a['id'], NOW + timedelta(hours=1))  # model reschedules
    db.close()
    clock[0] = NOW + timedelta(hours=2)
    held = runner.tick()
    assert held['injected'] == 0 and held['policy_reason'] == 'max_unanswered'
    assert len(ctx.calls) == 1
    db = store_factory(); db.note_user_reply(clock[0]); db.close()  # user answers
    assert runner.tick()['injected'] == 1
