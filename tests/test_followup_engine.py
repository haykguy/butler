from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest

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
    next_morning_checkin,
    shift_out_of_quiet_hours,
    today_morning_checkin,
)
from butler.scheduler import Scheduler
from butler.settings import defaults
from butler.store import ResponsibilityStore


CHI = 'America/Chicago'
TZ_CHI = ZoneInfo(CHI)


def _utc(year, month, day, hour, minute=0, tz_name=CHI) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)


def test_profiles_and_describe_cadence():
    assert set(TONE_PROFILES) == {'Gentle', 'Supportive', 'Balanced', 'Direct', 'Strict'}
    assert set(PERSISTENCE_PROFILES) == {'Minimal', 'Occasional', 'Regular', 'Persistent'}

    for tone in TONE_PROFILES:
        for persistence in PERSISTENCE_PROFILES:
            desc = describe_cadence({'tone': tone, 'persistence': persistence})
            assert set(desc) == {
                'tone', 'tone_guide', 'persistence', 'summary',
                'non_negotiable_cadence', 'commitment_cadence',
            }
            assert desc['tone'] == tone
            assert desc['persistence'] == persistence
            assert tone in desc['summary'] and persistence in desc['summary']

    fallback = describe_cadence({'tone': 'Unknown', 'persistence': 'Invalid'})
    assert fallback['tone'] == 'Balanced'
    assert fallback['persistence'] == 'Occasional'


def test_quiet_hours_and_morning_checkin_helpers():
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

    # next_morning_checkin & today_morning_checkin
    before_830 = _utc(2026, 11, 11, 7, 15)
    after_830 = _utc(2026, 11, 11, 10, 0)
    assert next_morning_checkin(before_830, CHI) == _utc(2026, 11, 11, 8, 30)
    assert next_morning_checkin(after_830, CHI) == _utc(2026, 11, 12, 8, 30)

    assert today_morning_checkin(before_830, CHI) == _utc(2026, 11, 11, 8, 30)
    # Already past 08:30 local -> immediately due (`now`)
    assert today_morning_checkin(after_830, CHI, settings=cfg) == after_830

    # Quiet window ending at 09:15 shifts 08:30 morning check-in to 09:15
    late_quiet = dict(defaults(), quietEnabled=True, quietStart='23:00', quietEnd='09:15')
    assert next_morning_checkin(before_830, CHI, settings=late_quiet) == _utc(2026, 11, 11, 9, 15)


def test_compute_initial_schedule_across_kinds_and_persistence():
    base = dict(defaults(), quietEnabled=True, quietStart='22:00', quietEnd='07:00')

    # Non-negotiable before 09:00 local -> today 08:30 due, today 20:30 deadline
    t_early = _utc(2026, 11, 11, 7, 30)
    nn_early = compute_initial_schedule('non_negotiable', t_early, base, effective_timezone=CHI)
    assert nn_early['due_at'] == _utc(2026, 11, 11, 8, 30)
    assert nn_early['deadline_at'] == _utc(2026, 11, 11, 20, 30)

    # Non-negotiable midday -> +60m for Persistent/Regular, +2h for Occasional/Minimal
    t_midday = _utc(2026, 11, 11, 13, 0)
    nn_pers = compute_initial_schedule(
        'non_negotiable', t_midday, dict(base, persistence='Persistent'), effective_timezone=CHI,
    )
    nn_occ = compute_initial_schedule(
        'non_negotiable', t_midday, dict(base, persistence='Occasional'), effective_timezone=CHI,
    )
    assert nn_pers['due_at'] == t_midday + timedelta(minutes=60)
    assert nn_occ['due_at'] == t_midday + timedelta(hours=2)

    # Non-negotiable at 21:00 local (past 20:30) -> tomorrow 08:30 due, tomorrow 20:30 deadline
    t_night = _utc(2026, 11, 11, 21, 0)
    nn_night = compute_initial_schedule('non_negotiable', t_night, base, effective_timezone=CHI)
    assert nn_night['due_at'] == _utc(2026, 11, 12, 8, 30)
    assert nn_night['deadline_at'] == _utc(2026, 11, 12, 20, 30)

    # Commitment cadences
    c_pers = compute_initial_schedule('commitment', t_midday, dict(base, persistence='Persistent'), effective_timezone=CHI)
    c_reg = compute_initial_schedule('open_loop', t_midday, dict(base, persistence='Regular'), effective_timezone=CHI)
    c_occ_day = compute_initial_schedule('check_in', t_midday, dict(base, persistence='Occasional'), effective_timezone=CHI)
    c_occ_eve = compute_initial_schedule('commitment', _utc(2026, 11, 11, 18, 30), dict(base, persistence='Occasional'), effective_timezone=CHI)
    c_min_am = compute_initial_schedule('commitment', _utc(2026, 11, 11, 10, 0), dict(base, persistence='Minimal'), effective_timezone=CHI)
    c_min_pm = compute_initial_schedule('commitment', t_midday, dict(base, persistence='Minimal'), effective_timezone=CHI)

    assert c_pers['due_at'] == t_midday + timedelta(minutes=75)
    assert c_reg['due_at'] == t_midday + timedelta(hours=3)
    assert c_occ_day['due_at'] == t_midday + timedelta(hours=4)
    assert c_occ_eve['due_at'] == _utc(2026, 11, 12, 9, 30)
    assert c_min_am['due_at'] == _utc(2026, 11, 11, 16, 0)
    assert c_min_pm['due_at'] == _utc(2026, 11, 12, 9, 30)

    # Auto-chosen due_at landing in quiet hours shifts out of quiet hours
    c_quiet = compute_initial_schedule(
        'commitment', _utc(2026, 11, 11, 21, 15), dict(base, persistence='Persistent'), effective_timezone=CHI,
    )
    assert c_quiet['due_at'] == _utc(2026, 11, 12, 7, 0)

    # Watch default
    watch_sched = compute_initial_schedule('watch', t_midday, base, effective_timezone=CHI)
    assert watch_sched['due_at'] == t_midday + timedelta(minutes=15)
    assert watch_sched['interval_seconds'] == 900


def test_recurring_interval_and_anti_burst_after_sleep():
    cfg = dict(defaults(), persistence='Regular', quietEnabled=True, quietStart='22:00', quietEnd='07:00')
    due = _utc(2026, 11, 11, 9, 0)
    now = due + timedelta(hours=4, minutes=7)  # 4h+ sleep -> many missed 15m intervals
    item = {'id': 'w1', 'title': 'Check CI', 'due_at': due, 'interval_seconds': 900, 'stage': 0, 'kind': 'watch'}

    dec = next_followup_step(item, now, cfg, effective_timezone=CHI)
    assert dec.should_wake is True
    assert dec.actions == ['check_in']
    assert dec.next_stage == 1
    assert dec.next_due_at == due + timedelta(minutes=15 * 17)  # 13:15 local > 13:07 local
    assert dec.next_due_at > now

    # Non-interval item overdue by 4 hours schedules strictly forward from `now`, never <= now
    overdue_comm = {'id': 'c1', 'title': 'Send invoice', 'due_at': due, 'stage': -1, 'kind': 'commitment'}
    dec_comm = next_followup_step(overdue_comm, now, cfg, effective_timezone=CHI)
    assert dec_comm.should_wake is True
    assert dec_comm.next_due_at == now + timedelta(hours=3)


def test_minimal_and_occasional_state_machines():
    morning = _utc(2026, 11, 11, 8, 30)
    deadline = _utc(2026, 11, 11, 20, 30)

    # Minimal non-negotiable with deadline > 30m away -> 1 morning briefing + 15m pre-deadline heads-up
    min_cfg = dict(defaults(), persistence='Minimal')
    nn = {'id': 'nn1', 'title': 'Workout', 'due_at': morning, 'deadline_at': deadline, 'kind': 'non_negotiable', 'stage': -1}
    step1 = next_followup_step(nn, morning, min_cfg, effective_timezone=CHI)
    assert step1.actions == ['morning_briefing']
    assert step1.next_stage == 0
    assert step1.next_due_at == deadline - timedelta(minutes=15)
    assert step1.next_status == 'active'

    # Heads-up fires at 20:15 -> rolls to tomorrow 08:30
    nn_s0 = dict(nn, stage=0, due_at=step1.next_due_at)
    step2 = next_followup_step(nn_s0, step1.next_due_at, min_cfg, effective_timezone=CHI)
    assert step2.actions == ['heads_up']
    assert step2.heads_up is True
    assert step2.next_due_at == _utc(2026, 11, 12, 8, 30)
    assert step2.next_status == 'active'

    # Minimal commitment without deadline -> single wake then submitted
    comm = {'id': 'c1', 'title': 'Ping Alex', 'due_at': morning, 'kind': 'commitment', 'stage': -1}
    c_step = next_followup_step(comm, morning, min_cfg, effective_timezone=CHI)
    assert c_step.should_wake is True
    assert c_step.next_status == 'submitted'
    assert c_step.next_due_at is None

    # Occasional: stage -1 -> +4h; stage 0 -> rolls (non_negotiable) or submits (commitment)
    occ_cfg = dict(defaults(), persistence='Occasional')
    o1 = next_followup_step(comm, morning, occ_cfg, effective_timezone=CHI)
    assert o1.next_stage == 0
    assert o1.next_due_at == morning + timedelta(hours=4)
    assert o1.next_status == 'active'

    o2 = next_followup_step(dict(comm, stage=0, due_at=o1.next_due_at), o1.next_due_at, occ_cfg, effective_timezone=CHI)
    assert o2.next_stage == 1
    assert o2.next_status == 'submitted'
    assert o2.next_due_at is None

    nn_o2 = next_followup_step(dict(nn, stage=0, due_at=o1.next_due_at), o1.next_due_at, occ_cfg, effective_timezone=CHI)
    assert nn_o2.next_status == 'active'
    assert nn_o2.next_due_at == _utc(2026, 11, 12, 8, 30)


def test_regular_and_persistent_escalation_and_sleep_boundaries():
    reg_cfg = dict(defaults(), persistence='Regular')
    t0 = _utc(2026, 11, 11, 8, 30)
    nn = {'id': 'nn2', 'title': 'Read 20 pages', 'due_at': t0, 'kind': 'non_negotiable', 'stage': -1}

    r0 = next_followup_step(nn, t0, reg_cfg, effective_timezone=CHI)
    assert r0.next_due_at == t0 + timedelta(hours=3)
    assert r0.next_stage == 0

    # Stage >= 2 tightens gap from 3h to 2h
    t2 = _utc(2026, 11, 11, 14, 30)
    r2 = next_followup_step(dict(nn, stage=2, due_at=t2), t2, reg_cfg, effective_timezone=CHI)
    assert r2.next_due_at == t2 + timedelta(hours=2)
    assert r2.next_stage == 3

    # Stage >= 3 rolls daily non-negotiable to next morning
    t3 = _utc(2026, 11, 11, 16, 30)
    r3 = next_followup_step(dict(nn, stage=3, due_at=t3), t3, reg_cfg, effective_timezone=CHI)
    assert r3.next_due_at == _utc(2026, 11, 12, 8, 30)
    assert r3.next_status == 'active'

    # Persistent never stops; tightens 75m -> 45m -> 30m + voice_note + respects sleep
    pers_cfg = dict(defaults(), persistence='Persistent', quietEnabled=True, quietStart='22:00', quietEnd='07:00')
    p_item = {
        'id': 'nn_p', 'title': 'Write daily log', 'due_at': t0,
        'deadline_at': _utc(2026, 11, 11, 20, 30), 'kind': 'non_negotiable', 'stage': -1,
    }
    p0 = next_followup_step(p_item, t0, pers_cfg, effective_timezone=CHI)
    assert p0.actions == ['morning_briefing']
    assert p0.urgency == 'normal'
    assert p0.next_due_at == t0 + timedelta(minutes=75)
    assert p0.next_status == 'active'

    # Stage 1 midday -> 45m high urgency push
    t_mid = _utc(2026, 11, 11, 12, 0)
    p1 = next_followup_step(dict(p_item, stage=1, due_at=t_mid), t_mid, pers_cfg, effective_timezone=CHI)
    assert p1.actions == ['push']
    assert p1.urgency == 'high'
    assert p1.next_due_at == t_mid + timedelta(minutes=45)

    # Stage 3 or evening (>=17:00) with voice_note channel -> 30m critical evening_push + voice_note
    t_eve = _utc(2026, 11, 11, 18, 0)
    p_crit = next_followup_step(
        dict(p_item, stage=3, due_at=t_eve),
        t_eve,
        pers_cfg,
        effective_timezone=CHI,
        available_channels=('check_in', 'push', 'voice_note'),
    )
    assert p_crit.actions == ['evening_push', 'voice_note']
    assert p_crit.urgency == 'critical'
    assert p_crit.next_due_at == t_eve + timedelta(minutes=30)
    assert p_crit.next_status == 'active'

    # At 21:45 local, +30m would hit 22:15 (quiet hours) -> shifts to next morning check-in
    t_pre_sleep = _utc(2026, 11, 11, 21, 45)
    p_sleep = next_followup_step(dict(p_item, stage=5, due_at=t_pre_sleep), t_pre_sleep, pers_cfg, effective_timezone=CHI)
    assert p_sleep.should_wake is True
    assert p_sleep.next_due_at == _utc(2026, 11, 12, 8, 30)

    # Tick occurring INSIDE quiet hours defers without waking
    t_asleep = _utc(2026, 11, 11, 23, 30)
    p_deferred = next_followup_step(dict(p_item, stage=5, due_at=t_asleep), t_asleep, pers_cfg, effective_timezone=CHI)
    assert p_deferred.should_wake is False
    assert p_deferred.actions == []
    assert p_deferred.next_due_at == _utc(2026, 11, 12, 8, 30)


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
    assert 'ACTION-FIRST DIRECTIVE' in prompt
    assert '30-second prep step' in prompt
    assert 'Strict' in prompt
    assert 'explicit snooze time' in prompt
    assert 'butler_manage' in prompt
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
