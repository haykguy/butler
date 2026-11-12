from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest

from butler import followup
from butler.context import build_lifetime_awareness_context
from butler.followup import (
    PERSISTENCE_PROFILES,
    TONE_PROFILES,
    FollowupDecision,
    WakeBatch,
    build_action_first_wake_prompt,
    coalesce_due_batch,
    compute_initial_schedule,
    describe_cadence,
    next_followup_step,
    shift_out_of_quiet_hours,
)
from butler.scheduler import Scheduler
from butler.settings import defaults
from butler.store import ResponsibilityStore


CHI = 'America/Chicago'
TZ_CHI = ZoneInfo(CHI)


def _utc(year, month, day, hour, minute=0, tz_name=CHI) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)


def test_no_hardcoded_commitment_cadence_or_fixed_morning_times():
    # The model owns commitment timing; mechanical watch polling is a separate concern.
    for name in ('next_morning_checkin', 'today_morning_checkin', '_tomorrow_morning_checkin'):
        assert not hasattr(followup, name)
    for prof in PERSISTENCE_PROFILES.values():
        assert 'hint' in prof and 'cadence' not in ' '.join(prof)
        assert not any(ch.isdigit() for ch in prof['hint'] + prof['summary'])


def test_profiles_and_describe_cadence_are_preferences_only():
    assert set(PERSISTENCE_PROFILES) == {'Minimal', 'Occasional', 'Regular', 'Persistent'}
    for tone in TONE_PROFILES:
        for persistence in PERSISTENCE_PROFILES:
            desc = describe_cadence({'tone': tone, 'persistence': persistence})
            assert desc['tone'] == tone and desc['persistence'] == persistence
            assert 'Model-driven' in desc['commitment_cadence']
    fallback = describe_cadence({'tone': 'Unknown', 'persistence': 'Invalid'})
    assert (fallback['tone'], fallback['persistence']) == ('Balanced', 'Occasional')


@pytest.mark.parametrize('persistence', list(PERSISTENCE_PROFILES))
def test_initial_schedule_is_identical_for_every_persistence_level(persistence):
    now = _utc(2026, 11, 11, 13, 0)
    cfg = dict(defaults(), persistence=persistence)
    for kind in ('commitment', 'open_loop', 'check_in', 'non_negotiable'):
        sched = compute_initial_schedule(kind, now, cfg, effective_timezone=CHI)
        assert sched['due_at'] == now  # due now: the model is woken and chooses the time
        assert sched['schedule_reason'] == 'due_now_model_decides'
        assert sched['deadline_at'] is None  # no invented 20:30 deadline
    explicit = _utc(2026, 11, 12, 15, 0)
    assert compute_initial_schedule('commitment', now, cfg, due_at=explicit)['due_at'] == explicit


def test_followup_step_only_decides_whether_to_wake_never_the_next_time():
    now = _utc(2026, 11, 11, 13, 0)
    for persistence in PERSISTENCE_PROFILES:
        cfg = dict(defaults(), persistence=persistence)
        for kind in ('commitment', 'non_negotiable'):
            item = {'id': 'x', 'kind': kind, 'stage': 3, 'due_at': now - timedelta(minutes=1),
                    'deadline_at': now + timedelta(minutes=10)}
            dec = next_followup_step(item, now, cfg, effective_timezone=CHI)
            assert dec.should_wake and dec.next_due_at is None
            assert dec.next_status == 'submitted' and dec.reason == 'awaiting_model_reschedule'
    not_due = next_followup_step({'id': 'x', 'due_at': now + timedelta(hours=1)}, now, defaults(), effective_timezone=CHI)
    assert not not_due.should_wake


def test_model_chosen_interval_recurs_and_quiet_hours_still_gate():
    now = _utc(2026, 11, 11, 13, 0)
    rec = {'id': 'w', 'kind': 'watch', 'due_at': now, 'interval_seconds': 600}
    dec = next_followup_step(rec, now, defaults(), effective_timezone=CHI)
    assert dec.should_wake and dec.next_status == 'active' and dec.next_due_at == now + timedelta(minutes=10)
    cfg = dict(defaults(), quietEnabled=True, quietStart='22:00', quietEnd='07:00')
    night = _utc(2026, 11, 11, 23, 30)
    q = next_followup_step({'id': 'q', 'due_at': night - timedelta(hours=1)}, night, cfg, effective_timezone=CHI)
    assert not q.should_wake and q.next_due_at == _utc(2026, 11, 12, 7, 0)
    recurring = {'id': 'watch', 'kind': 'watch', 'due_at': night - timedelta(hours=1), 'interval_seconds': 3600}
    held = next_followup_step(recurring, night, cfg, effective_timezone=CHI)
    assert not held.should_wake and held.next_due_at == _utc(2026, 11, 12, 7, 0)
    resumed = next_followup_step(recurring, held.next_due_at, cfg, effective_timezone=CHI)
    assert resumed.should_wake and resumed.next_status == 'active' and resumed.next_due_at == _utc(2026, 11, 12, 7, 30)
    bypass = {'id': 'q', 'due_at': night - timedelta(hours=1), 'metadata': {'bypass_quiet_hours': True}}
    assert next_followup_step(bypass, night, cfg, effective_timezone=CHI).should_wake


def test_snooze_by_model_revives_submitted_item_and_context_flags_it():
    now = _utc(2026, 11, 11, 13, 0)
    with TemporaryDirectory(dir=Path(__file__).parent) as directory:
        store = ResponsibilityStore(Path(directory) / 'm.sqlite')
        try:
            item = store.create('Call dentist', now - timedelta(minutes=1))
            claimed = Scheduler(store, batch_size=10, lease_seconds=120).claim(now)
            dec = next_followup_step(claimed[0], now, defaults(), effective_timezone=CHI)
            assert store.advance_followup(item['id'], claimed[0]['claim_token'], now,
                                          next_due_at=dec.next_due_at, stage=dec.next_stage,
                                          next_status=dec.next_status)
            parked = store.get(item['id'])
            assert parked['status'] == 'submitted'
            out = build_lifetime_awareness_context(defaults(), now=now, active_responsibilities=[parked])
            assert 'AWAITING YOUR DECISION' in out
            assert Scheduler(store, batch_size=10, lease_seconds=120).claim(now + timedelta(days=3)) == []
            later = now + timedelta(hours=26)
            assert store.snooze(item['id'], later)
            revived = store.get(item['id'])
            assert revived['status'] == 'active'
            assert Scheduler(store, batch_size=10, lease_seconds=120).claim(later)[0]['id'] == item['id']
        finally:
            store.close()


def test_quiet_hours_safety_gate():
    cfg = dict(defaults(), quietEnabled=True, quietStart='22:00', quietEnd='07:00')

    # Inside overnight quiet hours (before midnight local -> tomorrow 07:00 local)
    late_evening = _utc(2026, 11, 11, 23, 15)
    shifted_1 = shift_out_of_quiet_hours(late_evening, cfg, CHI)
    assert shifted_1 == _utc(2026, 11, 12, 7, 0)

    # Inside overnight quiet hours (after midnight local -> same day 07:00 local)
    early_am = _utc(2026, 11, 12, 2, 45)
    shifted_2 = shift_out_of_quiet_hours(early_am, cfg, CHI)
    assert shifted_2 == _utc(2026, 11, 12, 7, 0)

    # Half-open end boundary: 07:00 local is NOT inside quiet hours
    at_end = _utc(2026, 11, 12, 7, 0)
    assert shift_out_of_quiet_hours(at_end, cfg, CHI) == at_end

    # Disabled quiet hours preserves timestamp
    assert shift_out_of_quiet_hours(late_evening, dict(cfg, quietEnabled=False), CHI) == late_evening

    # Naive datetime rejected
    with pytest.raises(ValueError):
        shift_out_of_quiet_hours(datetime(2026, 11, 11, 23, 0), cfg, CHI)



def test_batch_coalescing_scheduler_and_action_first_prompt():
    now = _utc(2026, 11, 11, 8, 30)
    cfg = dict(defaults(), tone='Strict', persistence='Persistent')

    items = [
        {'id': 'a1', 'title': 'Hydrate & stretch', 'kind': 'non_negotiable', 'nudge_count': 2, 'stage': 2,
         'due_at': now, 'deadline_at': _utc(2026, 11, 11, 20, 30), 'route_id': 'route-tg',
         'metadata': {'prep_hint': 'Check yesterday mobility notes'}},
        {'id': 'a2', 'title': 'Ship PR review', 'kind': 'commitment', 'nudge_count': 0, 'stage': -1,
         'due_at': now, 'route_id': 'route-tg', 'metadata': {'notes': 'Repo: butler'}},
        {'id': 'b1', 'title': 'Quiet item', 'kind': 'commitment', 'nudge_count': 0, 'stage': -1,
         'due_at': now, 'route_id': 'route-desk'},
    ]
    d1 = next_followup_step(items[0], now, cfg, effective_timezone=CHI)
    d2 = next_followup_step(items[1], now, cfg, effective_timezone=CHI)
    d_skip = FollowupDecision(
        should_wake=False, actions=[], next_stage=-1, next_due_at=now + timedelta(hours=1),
        next_status='active', urgency='low', reason='quiet_hours_deferred',
    )
    batches = coalesce_due_batch([(items[0], d1), (items[2], d_skip), (items[1], d2)])
    assert len(batches) == 1
    assert isinstance(batches[0], WakeBatch)
    assert batches[0].route_id == 'route-tg'
    assert [i['id'] for i in batches[0].items] == ['a1', 'a2']
    assert isinstance(batches[0].decisions, dict)
    assert batches[0].decisions['a1'] == d1
    assert batches[0].decisions.get('a2') == d2
    assert list(zip(batches[0].items, batches[0].decisions)) == [(items[0], d1), (items[1], d2)]

    prompt = build_action_first_wake_prompt(
        batches[0].items, batches[0].decisions, cfg, now=now, effective_timezone=CHI,
    )
    assert 'Hydrate & stretch' in prompt
    assert 'Ship PR review' in prompt
    assert 'check_in=#3' in prompt
    assert 'prep_hint: Check yesterday mobility notes' in prompt
    assert 'notes: Repo: butler' in prompt
    assert 'ACTION-FIRST' in prompt
    assert '30-second prep step' in prompt
    assert 'Strict' in prompt
    assert 'YOU OWN THE FOLLOW-UP' in prompt
    assert 'butler_manage' in prompt
    assert 'snooze' in prompt
    assert '[BUTLER WAKE]' not in prompt

    # Scheduler preserves constructor validation & claim(), and supports claim_and_plan()
    with TemporaryDirectory(dir=Path(__file__).parent) as directory:
        store = ResponsibilityStore(Path(directory) / 'sched.sqlite')
        try:
            with pytest.raises(ValueError):
                Scheduler(store, batch_size=0)
            with pytest.raises(ValueError):
                Scheduler(store, lease_seconds=False)
            store.create('Morning item 1', now - timedelta(minutes=2))
            store.create('Morning item 2', now - timedelta(minutes=1))
            sched = Scheduler(store, batch_size=10, lease_seconds=120)
            planned, planned_batches = sched.claim_and_plan(cfg, now=now, effective_timezone=CHI)
            assert len(planned) == 2
            assert len(planned_batches) == 1
            assert len(planned_batches[0].items) == 2
        finally:
            store.close()


def test_context_open_loop_awareness_and_backward_compatibility():
    now = _utc(2026, 11, 11, 9, 15)
    cfg = dict(defaults(), timezone=CHI, persistence='Persistent')

    # Default active_responsibilities=None must match legacy output byte-for-byte
    legacy_out = build_lifetime_awareness_context(cfg, now=now)
    explicit_none = build_lifetime_awareness_context(cfg, now=now, active_responsibilities=None)
    assert legacy_out == explicit_none
    assert 'Daily Non-Negotiables:' not in legacy_out
    assert 'butler_create' not in legacy_out

    # Empty list [] renders responsibility block + 2-sentence proactive directive
    empty_out = build_lifetime_awareness_context(cfg, now=now, active_responsibilities=[])
    assert empty_out.startswith(legacy_out + '\n')
    assert 'Daily Non-Negotiables: none tracked yet' in empty_out
    assert 'Open commitments (0): none currently open.' in empty_out
    assert 'butler_create' in empty_out
    assert 'persistence=Persistent' in empty_out
    assert 'never a schedule' in empty_out
    assert 'You own follow-up timing' in empty_out
    assert 'butler_manage' in empty_out

    # Populated non-negotiables (done today vs open today) + >8 open commitments capped to 8
    responsibilities = [
        {
            'id': 'nn-done', 'title': 'Morning meditation', 'kind': 'non_negotiable',
            'status': 'active', 'completed_day': '2026-11-11',
        },
        {
            'id': 'nn-open', 'title': '10k steps', 'kind': 'non_negotiable',
            'status': 'active', 'completed_day': '2026-11-10',
            'deadline_at': _utc(2026, 11, 11, 20, 30),
        },
    ]
    for i in range(10):
        responsibilities.append({
            'id': f'c-{i}',
            'title': f'Task {i}',
            'kind': 'commitment',
            'status': 'active',
            'due_at': now + timedelta(minutes=15 * i),
        })
    rich_out = build_lifetime_awareness_context(cfg, now=now, active_responsibilities=responsibilities)
    assert 'still open today (1): 10k steps (by 20:30) [id=nn-open]' in rich_out
    assert 'done today (1): Morning meditation [id=nn-done]' in rich_out
    assert 'Open commitments (10):' in rich_out
    assert 'Task 0' in rich_out and 'Task 7' in rich_out
    assert 'Task 8' not in rich_out
    assert '(+2 more)' in rich_out
