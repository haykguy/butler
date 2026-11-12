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


def test_hook_injects_followup_directive_even_without_any_items(tmp_path):
    hooks = AwarenessHooks(directory=lambda: tmp_path, load_state=lambda: {}, now_fn=lambda: NOW,
                           system_tz_fn=lambda: 'America/Chicago')
    result = hooks.pre_llm_call(user_message='Hey, quick question', conversation_history=[], is_first_turn=True)
    assert result and 'After EVERY reply' in result['context']
    assert 'should I come back' in result['context']


def test_runner_injects_only_human_marker_and_pre_hook_adds_hidden_prompt(tmp_path, monkeypatch):
    from butler.settings import save
    from tests.test_runner_wakes import FakeHostContext, _make_harness
    import butler.awareness as awareness_module

    save(tmp_path, dict(defaults(), notificationsEnabled=True, timezone='America/Chicago', minWakeGapMinutes=0))
    store_factory, reg_factory, routes, _ = _make_harness(tmp_path)
    route = {'route_id': 'private-route', 'kind': 'session_key', 'session_key': 'imessage:hayk', 'origin': None, 'platform': 'imessage'}
    routes.record_route(route)
    db = store_factory()
    db.create('Send confidential invoice', NOW - timedelta(minutes=1), route_id='private-route')
    db.create('Another route private task', NOW - timedelta(minutes=1), route_id='other-route')
    db.close()
    host = FakeHostContext(accepted=True)
    runner = Runner(host, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: NOW)
    assert runner.tick()['injected'] == 1
    assert host.calls[0][0] == 'Butler check-in.'

    monkeypatch.setattr(awareness_module, 'capture_bound_turn_route', lambda: route)
    hooks = AwarenessHooks(directory=lambda: tmp_path, load_state=lambda: {}, now_fn=lambda: NOW,
                           system_tz_fn=lambda: 'America/Chicago', store_factory=store_factory)
    result = hooks.pre_llm_call(user_message='Butler check-in.', session_id='s1', conversation_history=[])
    hidden = result['context']
    assert 'YOU OWN THE FOLLOW-UP' in hidden and 'Send confidential invoice' in hidden
    assert 'Another route private task' not in hidden
    assert 'Send confidential invoice' not in host.calls[0][0]
    hooks.post_llm_call(user_message='Butler check-in.', session_id='s1', assistant_response='I found the invoice; can you send it?')
    db = store_factory()
    assert db.get_engagement()['unanswered_wakes'] == 1
    db.close()


def test_only_visible_assistant_response_to_wake_counts_as_unanswered(tmp_path, monkeypatch):
    store = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
    store.create('Send invoice', NOW)
    store.close()
    hooks = AwarenessHooks(directory=lambda: tmp_path, load_state=lambda: {}, now_fn=lambda: NOW,
                           system_tz_fn=lambda: 'America/Chicago')
    route = {'route_id': 'route-a', 'kind': 'session_key', 'session_key': 'imessage:hayk', 'origin': None, 'platform': 'imessage'}
    import butler.awareness as awareness_module
    monkeypatch.setattr(awareness_module, 'capture_bound_turn_route', lambda: route)
    marker = 'Butler check-in.'
    for response, expected in (('[SILENT]', 0), ('Did you get the invoice sent?', 1)):
        store = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
        wake_id = store.enqueue_pending_wake('route-a', f'{WAKE_MARKER}: invoice follow-up', NOW)
        store.close()
        hooks.pre_llm_call(user_message=marker, session_id='wake-session', conversation_history=[])
        hooks.post_llm_call(user_message=marker, session_id='wake-session', assistant_response=response)
        store = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
        assert store.get_engagement()['unanswered_wakes'] == expected
        store.close()
    hooks.pre_llm_call(user_message='Hey, are you around?', session_id='wake-session', conversation_history=[], is_first_turn=False)
    store = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
    assert store.get_engagement()['unanswered_wakes'] == 0
    store.close()
    store = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
    assert store.get_engagement()['unanswered_wakes'] == 0
    store.close()



def test_same_session_wake_context_is_bound_to_turn_id(tmp_path, monkeypatch):
    import butler.awareness as awareness_module
    route = {'route_id': 'route-a', 'kind': 'session_key', 'session_key': 'imessage:hayk', 'origin': None, 'platform': 'imessage'}
    monkeypatch.setattr(awareness_module, 'capture_bound_turn_route', lambda: route)
    store = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
    store.enqueue_pending_wake('route-a', 'wake payload one', NOW)
    store.enqueue_pending_wake('route-a', 'wake payload two', NOW + timedelta(seconds=1))
    store.close()
    hooks = AwarenessHooks(directory=lambda: tmp_path, load_state=lambda: {}, now_fn=lambda: NOW,
                           system_tz_fn=lambda: 'America/Chicago')
    first = hooks.pre_llm_call(user_message='Butler check-in.', session_id='same-session', turn_id='turn-1', conversation_history=[])
    second = hooks.pre_llm_call(user_message='Butler check-in.', session_id='same-session', turn_id='turn-2', conversation_history=[])
    assert 'wake payload one' in first['context'] and 'wake payload two' not in first['context']
    assert 'wake payload two' in second['context'] and 'wake payload one' not in second['context']
    hooks.post_llm_call(user_message='Butler check-in.', session_id='same-session', turn_id='turn-1', assistant_response='first reply')
    hooks.post_llm_call(user_message='Butler check-in.', session_id='same-session', turn_id='turn-2', assistant_response='second reply')
    store = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
    assert store.get_engagement()['unanswered_wakes'] == 2
    assert store.db.execute('SELECT COUNT(*) FROM pending_wakes').fetchone()[0] == 0
    store.close()


def test_hook_surfaces_submitted_items_awaiting_model_decision(tmp_path):
    store = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
    item = store.create('Send invoice', NOW, kind='commitment')
    store.db.execute("UPDATE responsibilities SET status='submitted' WHERE id=?", (item['id'],))
    store.close()
    hooks = AwarenessHooks(directory=lambda: tmp_path, load_state=lambda: {}, now_fn=lambda: NOW,
                           system_tz_fn=lambda: 'America/Chicago')
    result = hooks.pre_llm_call(user_message='Hello', conversation_history=[], is_first_turn=False)
    assert 'Send invoice' in result['context']
    assert 'AWAITING YOUR DECISION' in result['context']


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

    hooks.pre_llm_call(user_message='Butler check-in.', conversation_history=[], is_first_turn=False)
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
    for phrase in ('availability', 'Learn from reactions', 'word for word', 'go louder', 'Never invent a deadline', 'this week,” do not schedule a wake after that week ends', 'Do not create a second responsibility', 'Honor timing and outreach boundaries literally', 'Make at most one check after the ETA', 'unanswered proactive nudges since their last real message: 3'):
        assert phrase in prompt
    assert WAKE_MARKER in prompt


def test_wake_prompt_respects_user_stated_time_boundaries_without_fixed_cadence():
    prompt = build_action_first_wake_prompt(
        [{'id': 'a', 'title': 'Send invoice'}], {}, defaults(), now=NOW,
        effective_timezone='America/Chicago',
    )
    assert 'Honor timing and outreach boundaries literally' in prompt
    assert 'Never wake before a user-stated time they plan to act' in prompt
    assert 'schedule at most one later check-in that day' in prompt
    assert 'do not schedule a wake after that week ends' in prompt
    assert 'No per-level intervals, escalation ladder, or fixed follow-up cadence' in prompt
    assert 'a minute for something live' not in prompt


def test_context_tells_model_to_decide_followup_after_every_reply():
    out = build_lifetime_awareness_context(
        defaults(), now=NOW, active_responsibilities=[], engagement={'unanswered_wakes': 2},
    )
    assert 'After EVERY reply' in out and 'should I come back' in out
    assert 'bare question' in out
    assert 'Engagement: 2 unanswered' in out


def test_tick_end_to_end_wake_counts_gates_and_resets(tmp_path, monkeypatch):
    """Real Runner.tick(): wake goes out with judgment prompt, counter rises, gate holds, real reply reopens it."""
    from butler.settings import save
    from tests.test_runner_wakes import FakeHostContext, _make_harness
    clock = [NOW]
    save(tmp_path, dict(defaults(), notificationsEnabled=True, timezone='America/Chicago',
                        maxUnanswered=1, minWakeGapMinutes=0))
    store_factory, reg_factory, routes, _ = _make_harness(tmp_path)
    route = {'route_id': 'r', 'kind': 'session_key', 'session_key': 'tui:x', 'origin': None, 'platform': 'tui'}
    routes.record_route(route)
    db = store_factory()
    a = db.create('Send deck', NOW - timedelta(minutes=1), route_id='r')
    b = db.create('Call dentist', NOW - timedelta(minutes=1), route_id='r')
    db.close()
    ctx = FakeHostContext(accepted=True)
    runner = Runner(ctx, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: clock[0])

    assert runner.tick()['injected'] == 1
    marker = ctx.calls[0][0]
    assert marker == 'Butler check-in.'
    import butler.awareness as awareness_module
    monkeypatch.setattr(awareness_module, 'capture_bound_turn_route', lambda: route)
    hook = AwarenessHooks(directory=lambda: tmp_path, load_state=lambda: {}, now_fn=lambda: NOW,
                          system_tz_fn=lambda: 'America/Chicago', store_factory=store_factory)
    hidden = hook.pre_llm_call(user_message=marker, session_id='session-r', conversation_history=[])['context']
    assert 'YOU OWN THE FOLLOW-UP' in hidden and 'JUDGMENT' in hidden and 'ENGAGEMENT' in hidden
    hook.post_llm_call(user_message=marker, session_id='session-r', assistant_response='I checked in about the deck.')

    db = store_factory()
    assert db.get(a['id'])['status'] == 'submitted' and db.get_engagement()['unanswered_wakes'] == 1
    assert db.snooze(a['id'], NOW + timedelta(hours=1))  # model reschedules
    db.close()
    clock[0] = NOW + timedelta(hours=2)
    held = runner.tick()
    assert held['injected'] == 0 and held['policy_reason'] == 'max_unanswered'
    assert len(ctx.calls) == 1
    db = store_factory()
    assert db.get(a['id'])['status'] == 'submitted'  # no 30-second retry loop while waiting for a human
    db.note_user_reply(clock[0])  # user answers; its turn sees the parked item and decides next
    assert db.snooze(a['id'], clock[0])  # simulated model chooses to resume now
    db.close()
    assert runner.tick()['injected'] == 1


def test_model_can_close_pause_stop_and_store_followup_note_on_submitted_item(tmp_path):
    store = ResponsibilityStore(tmp_path / 'transitions.sqlite')
    item = store.create('Open task', NOW)
    store.db.execute("UPDATE responsibilities SET status='submitted' WHERE id=?", (item['id'],))
    assert store.snooze(item['id'], NOW + timedelta(hours=2), notes='User was in a meeting; ask after lunch.')
    got = store.get(item['id'])
    assert got['status'] == 'active' and got['metadata']['notes'] == 'User was in a meeting; ask after lunch.'
    store.db.execute("UPDATE responsibilities SET status='submitted' WHERE id=?", (item['id'],))
    assert store.pause(item['id'], notes='Parked pending user reply.')
    paused = store.get(item['id'])
    assert paused is not None
    assert paused['status'] == 'paused' and paused['metadata']['notes'] == 'Parked pending user reply.'
    hooks = AwarenessHooks(
        directory=lambda: tmp_path,
        load_state=lambda: {},
        now_fn=lambda: NOW,
        system_tz_fn=lambda: 'America/Chicago',
        store_factory=lambda: ResponsibilityStore(tmp_path / 'transitions.sqlite'),
    )
    injected = hooks.pre_llm_call(user_message='I will send it in 20 minutes', conversation_history=[], is_first_turn=False)
    assert injected is not None and 'Open task' in injected['context'] and 'PAUSED' in injected['context']
    assert store.resume(item['id'])
    store.db.execute("UPDATE responsibilities SET status='submitted' WHERE id=?", (item['id'],))
    assert store.stop(item['id'])
    assert store.get(item['id'])['status'] == 'stopped'
    item2 = store.create('Other task', NOW)
    store.db.execute("UPDATE responsibilities SET status='submitted' WHERE id=?", (item2['id'],))
    assert store.complete(item2['id'])
    assert store.get(item2['id'])['status'] == 'completed'
    assert not store.snooze(item2['id'], NOW + timedelta(hours=1), notes='must not persist on a closed item')
    assert store.get(item2['id'])['metadata'].get('notes') != 'must not persist on a closed item'
    store.close()


def test_public_repo_sanitized_no_private_hosts_or_ios_app():
    root = Path(__file__).resolve().parents[1]
    targets = [
        root / 'README.md',
        root / 'plugin.yaml',
        root / 'pyproject.toml',
        root / '__init__.py',
        *sorted((root / 'butler').glob('*.py')),
        *sorted((root / 'dashboard').glob('*.py')),
        *sorted((root / 'desktop').glob('*.js')),
        *sorted((root / 'skills').rglob('*.md')),
    ]
    forbidden = ('Hayk', 'haykguy/', 'Mecha', 'desklinux', '192.168.', 'HermesCall', '/Users/')
    for path in targets:
        text = path.read_text(encoding='utf-8')
        for token in forbidden:
            if token == 'haykguy/' and path.name in ('README.md', 'plugin.yaml', 'pyproject.toml'):
                continue
            assert token not in text, f'{token!r} leaked in {path.relative_to(root)}'
