"""Escalation state machine, auto-cadence, anti-burst coalescing, and action-first wake prompts.

Pure policy functions operating on timezone-aware datetimes and Butler profile settings.
Nothing in this module touches SQLite or dispatches network/channel I/O directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TONE_PROFILES: dict[str, dict] = {
    'Gentle': {
        'tone': 'Gentle',
        'guide': 'Warm, calm, zero-guilt, and invitational; lower starting friction without pressure.',
        'voice_rules': (
            'Use a soft, warm, zero-guilt voice. Never scold or dramatize delays; '
            'make starting feel light, doable, and completely pressure-free.'
        ),
    },
    'Supportive': {
        'tone': 'Supportive',
        'guide': 'Encouraging teammate; upbeat, collaborative, and focused on momentum and practical help.',
        'voice_rules': (
            'Sound like an encouraging teammate in their corner. Offer practical help '
            'and celebrate progress while nudging the next concrete step.'
        ),
    },
    'Balanced': {
        'tone': 'Balanced',
        'guide': 'Clear, attentive human assistant; grounded, concise, and matter-of-factly helpful.',
        'voice_rules': (
            'Write like a clear, attentive human assistant—concise, grounded, '
            'warm without fluff, and focused on making the next step easy.'
        ),
    },
    'Direct': {
        'tone': 'Direct',
        'guide': 'Crisp, high-signal, no fluff; straight to status, prep, and the next concrete action.',
        'voice_rules': (
            'Be crisp and direct with zero filler or pleasantries. Lead with what you '
            'prepared or checked and state the exact next action.'
        ),
    },
    'Strict': {
        'tone': 'Strict',
        'guide': (
            'Firm accountability coach; call out procrastination directly and demand '
            'action or an explicit snooze time, without personal insults.'
        ),
        'voice_rules': (
            'Speak as a firm, no-nonsense accountability coach. Call out procrastination '
            'or drift directly and ask for immediate action or an explicit snooze time. '
            'Attack the excuse or delay, never personal worth, health, or appearance; '
            'if the user is unwell or in distress, drop strictness immediately.'
        ),
    },
}

PERSISTENCE_PROFILES: dict[str, dict] = {
    'Minimal': {
        'persistence': 'Minimal',
        'summary': 'Single light touch per item (plus an optional 15m pre-deadline heads-up).',
        'non_negotiable_cadence': '1 daily morning/initial check-in (+1 heads-up 15m before deadline if set), then next morning.',
        'commitment_cadence': '1 check-in when due (+1 final reminder 15m before deadline if set), then marks submitted.',
    },
    'Occasional': {
        'persistence': 'Occasional',
        'summary': 'Check in when due and follow up once 4h later before stepping back.',
        'non_negotiable_cadence': 'Initial check-in, 1 follow-up after 4h, then rolls to next morning 08:30.',
        'commitment_cadence': 'Initial check-in, 1 follow-up after 4h, then marks submitted.',
    },
    'Regular': {
        'persistence': 'Regular',
        'summary': 'Steady daytime follow-ups every 3h (tightening to 2h later) with pre-deadline heads-up.',
        'non_negotiable_cadence': 'Every 3h (tightening to 2h after stage >= 2) up to 4 follow-ups/day + 15m deadline heads-up, then next morning.',
        'commitment_cadence': 'Every 3h (tightening to 2h after stage >= 2) up to 4 follow-ups, continuing until deadline passes.',
    },
    'Persistent': {
        'persistence': 'Persistent',
        'summary': 'Keep following up with tightening intervals (75m -> 45m -> 30m) until completed, paused, or snoozed.',
        'non_negotiable_cadence': 'Tightens 75m -> 45m -> 30m through the afternoon/evening, pauses for sleep/quiet hours, resumes next morning.',
        'commitment_cadence': 'Tightens 75m -> 45m -> 30m continuously (respecting quiet hours) until completed, paused, or snoozed.',
    },
}


def _zone(effective_timezone: str) -> ZoneInfo:
    candidate = (effective_timezone or 'UTC').strip()
    if candidate.startswith(':'):
        candidate = candidate[1:].strip()
    if not candidate:
        return ZoneInfo('UTC')
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo('UTC')


def _require_aware_utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        parsed = datetime.fromisoformat(dt.strip().replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('datetime must be timezone-aware')
        return parsed.astimezone(timezone.utc)
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('datetime must be timezone-aware')
    return dt.astimezone(timezone.utc)


def _coerce_optional_utc(dt: datetime | str | None) -> datetime | None:
    if dt is None or dt == '':
        return None
    return _require_aware_utc(dt)


def _parse_quiet_window(settings: dict | None) -> tuple[dtime, dtime] | None:
    cfg = settings or {}
    if not cfg.get('quietEnabled'):
        return None
    raw_start = cfg.get('quietStart')
    raw_end = cfg.get('quietEnd')
    if not isinstance(raw_start, str) or not isinstance(raw_end, str) or not raw_start or not raw_end:
        return None
    try:
        start = dtime.fromisoformat(raw_start.strip())
        end = dtime.fromisoformat(raw_end.strip())
    except ValueError:
        return None
    if start == end:
        return None
    return start, end


def describe_cadence(settings: dict) -> dict:
    """Summarize effective tone and persistence cadence in plain language."""
    cfg = settings or {}
    tone_key = cfg.get('tone') if cfg.get('tone') in TONE_PROFILES else 'Balanced'
    pers_key = cfg.get('persistence') if cfg.get('persistence') in PERSISTENCE_PROFILES else 'Occasional'
    t_prof = TONE_PROFILES[tone_key]
    p_prof = PERSISTENCE_PROFILES[pers_key]
    return {
        'tone': tone_key,
        'tone_guide': t_prof['guide'],
        'persistence': pers_key,
        'summary': f"{tone_key} tone · {pers_key} cadence: {p_prof['summary']}",
        'non_negotiable_cadence': p_prof['non_negotiable_cadence'],
        'commitment_cadence': p_prof['commitment_cadence'],
    }


def shift_out_of_quiet_hours(
    candidate_utc: datetime,
    settings: dict,
    effective_timezone: str = 'UTC',
) -> datetime:
    """Advance `candidate_utc` to the end of quiet hours if it lands inside an active quiet window."""
    cand = _require_aware_utc(candidate_utc)
    window = _parse_quiet_window(settings)
    if window is None:
        return cand
    start, end = window
    tz = _zone(effective_timezone)
    local_dt = cand.astimezone(tz)
    current_t = local_dt.time().replace(tzinfo=None)
    inside = (start <= current_t < end) if start < end else (current_t >= start or current_t < end)
    if not inside:
        return cand

    target_date = local_dt.date()
    if start > end and current_t >= start:
        target_date = target_date + timedelta(days=1)
    end_local = datetime(
        target_date.year, target_date.month, target_date.day,
        end.hour, end.minute, end.second, end.microsecond,
        tzinfo=tz,
    )
    end_utc = end_local.astimezone(timezone.utc)
    if end_utc <= cand:
        # Repeated wall time after a DST fall-back: the second occurrence of the
        # end time is still ahead; do not skip a whole day.
        later = end_local.replace(fold=1).astimezone(timezone.utc)
        if later > cand:
            return later
    if end_utc <= cand:
        next_day = target_date + timedelta(days=1)
        end_utc = datetime(
            next_day.year, next_day.month, next_day.day,
            end.hour, end.minute, end.second, end.microsecond,
            tzinfo=tz,
        ).astimezone(timezone.utc)
    return end_utc


def next_morning_checkin(
    now: datetime,
    effective_timezone: str = 'UTC',
    *,
    hour: int = 8,
    minute: int = 30,
    settings: dict | None = None,
) -> datetime:
    """Return aware UTC datetime for the next upcoming `hour:minute` in `effective_timezone`."""
    now_utc = _require_aware_utc(now)
    tz = _zone(effective_timezone)
    local_now = now_utc.astimezone(tz)
    target_local = datetime(
        local_now.year, local_now.month, local_now.day,
        hour, minute, 0, 0, tzinfo=tz,
    )
    if local_now >= target_local:
        tmr = local_now.date() + timedelta(days=1)
        target_local = datetime(tmr.year, tmr.month, tmr.day, hour, minute, 0, 0, tzinfo=tz)
    result = target_local.astimezone(timezone.utc)
    if settings is not None:
        result = shift_out_of_quiet_hours(result, settings, effective_timezone)
    return result


def today_morning_checkin(
    now: datetime,
    effective_timezone: str = 'UTC',
    *,
    hour: int = 8,
    minute: int = 30,
    settings: dict | None = None,
) -> datetime:
    """Return aware UTC datetime for today's morning check-in (or `now` if already past `hour:minute` local)."""
    now_utc = _require_aware_utc(now)
    tz = _zone(effective_timezone)
    local_now = now_utc.astimezone(tz)
    morning_local = datetime(
        local_now.year, local_now.month, local_now.day,
        hour, minute, 0, 0, tzinfo=tz,
    )
    result = morning_local.astimezone(timezone.utc) if local_now < morning_local else now_utc
    if settings is not None:
        result = shift_out_of_quiet_hours(result, settings, effective_timezone)
    return result


def _tomorrow_morning_checkin(
    now_utc: datetime,
    effective_timezone: str,
    settings: dict | None = None,
    *,
    hour: int = 8,
    minute: int = 30,
) -> datetime:
    tz = _zone(effective_timezone)
    local_now = now_utc.astimezone(tz)
    tmr = local_now.date() + timedelta(days=1)
    target_utc = datetime(tmr.year, tmr.month, tmr.day, hour, minute, 0, 0, tzinfo=tz).astimezone(timezone.utc)
    if settings is not None:
        target_utc = shift_out_of_quiet_hours(target_utc, settings, effective_timezone)
    return target_utc


def compute_initial_schedule(
    kind: str,
    now: datetime,
    settings: dict,
    *,
    effective_timezone: str = 'UTC',
    due_at: datetime | None = None,
    deadline_at: datetime | None = None,
    interval_seconds: int | None = None,
) -> dict:
    """Compute smart initial `due_at` and `deadline_at` when omitted, or normalize when supplied."""
    now_utc = _require_aware_utc(now)
    cfg = settings or {}
    persistence = cfg.get('persistence') if cfg.get('persistence') in PERSISTENCE_PROFILES else 'Occasional'
    tz = _zone(effective_timezone)
    local_now = now_utc.astimezone(tz)
    norm_kind = (kind or 'commitment').strip() or 'commitment'

    deadline_dt = _coerce_optional_utc(deadline_at)
    if deadline_dt is None and norm_kind == 'non_negotiable':
        dl_local = datetime(
            local_now.year, local_now.month, local_now.day,
            20, 30, 0, 0, tzinfo=tz,
        )
        if local_now >= dl_local:
            tmr = local_now.date() + timedelta(days=1)
            dl_local = datetime(tmr.year, tmr.month, tmr.day, 20, 30, 0, 0, tzinfo=tz)
        deadline_dt = dl_local.astimezone(timezone.utc)

    if norm_kind == 'watch' and not interval_seconds:
        interval_seconds = 900

    if due_at is not None:
        due_dt = _require_aware_utc(due_at)
        return {
            'due_at': due_dt,
            'deadline_at': deadline_dt,
            'interval_seconds': interval_seconds,
            'schedule_reason': 'explicit_due_at',
        }

    if norm_kind == 'non_negotiable':
        if local_now.hour < 9:
            due_dt = datetime(
                local_now.year, local_now.month, local_now.day,
                8, 30, 0, 0, tzinfo=tz,
            ).astimezone(timezone.utc)
            reason = 'non_negotiable_morning_0830'
        elif local_now.hour < 20:
            delta = timedelta(minutes=60) if persistence in ('Persistent', 'Regular') else timedelta(hours=2)
            due_dt = now_utc + delta
            reason = f'non_negotiable_same_day_{persistence.lower()}'
        else:
            due_dt = _tomorrow_morning_checkin(now_utc, effective_timezone, hour=8, minute=30)
            reason = 'non_negotiable_next_morning_0830'
    elif norm_kind == 'watch':
        due_dt = now_utc + timedelta(minutes=15)
        reason = 'watch_15m_interval'
    else:
        # 'commitment', 'open_loop', 'check_in', and any custom one-off responsibility kind
        if persistence == 'Persistent':
            due_dt = now_utc + timedelta(minutes=75)
            reason = 'commitment_persistent_75m'
        elif persistence == 'Regular':
            due_dt = now_utc + timedelta(hours=3)
            reason = 'commitment_regular_3h'
        elif persistence == 'Occasional':
            if local_now.hour >= 18:
                due_dt = next_morning_checkin(now_utc, effective_timezone, hour=9, minute=30)
                reason = 'commitment_occasional_next_morning_0930'
            else:
                due_dt = now_utc + timedelta(hours=4)
                reason = 'commitment_occasional_4h'
        else:  # 'Minimal'
            if local_now.hour < 12:
                due_dt = now_utc + timedelta(hours=6)
                reason = 'commitment_minimal_6h'
            else:
                due_dt = next_morning_checkin(now_utc, effective_timezone, hour=9, minute=30)
                reason = 'commitment_minimal_next_morning_0930'

    shifted_due = shift_out_of_quiet_hours(due_dt, cfg, effective_timezone)
    if shifted_due != due_dt:
        reason = f'{reason}_quiet_shifted'
    return {
        'due_at': shifted_due,
        'deadline_at': deadline_dt,
        'interval_seconds': interval_seconds,
        'schedule_reason': reason,
    }


@dataclass
class FollowupDecision:
    should_wake: bool
    actions: list[str]
    next_stage: int
    next_due_at: datetime | None
    next_status: str
    urgency: str
    heads_up: bool = False
    reason: str = ''


def next_followup_step(
    item: dict,
    now: datetime,
    settings: dict,
    *,
    effective_timezone: str = 'UTC',
    available_channels: tuple[str, ...] = ('check_in', 'push'),
) -> FollowupDecision:
    """Pure escalation & cadence engine for a due (`due_at <= now`) active responsibility."""
    now_utc = _require_aware_utc(now)
    cfg = settings or {}
    tz_name = effective_timezone or item.get('timezone') or 'UTC'
    tz = _zone(tz_name)
    local_now = now_utc.astimezone(tz)
    local_hour = local_now.hour

    due_at = _coerce_optional_utc(item.get('due_at')) or now_utc
    deadline = _coerce_optional_utc(item.get('deadline_at'))
    raw_stage = item.get('stage', item.get('followup_stage', -1))
    stage = int(-1 if raw_stage is None else raw_stage)
    kind = str(item.get('kind') or 'commitment')
    metadata = item.get('metadata') if isinstance(item.get('metadata'), dict) else {}
    avail = set(available_channels or ('check_in', 'push'))

    interval_raw = item.get('interval_seconds')
    if interval_raw is not None and int(interval_raw) > 0:
        interval = timedelta(seconds=int(interval_raw))
        steps = max(1, (now_utc - due_at) // interval + 1)
        next_due = shift_out_of_quiet_hours(due_at + steps * interval, cfg, tz_name)
        if next_due <= now_utc:
            next_due = now_utc + interval
        return FollowupDecision(
            should_wake=True,
            actions=['check_in'],
            next_stage=stage + 1,
            next_due_at=next_due,
            next_status='active',
            urgency='normal',
            heads_up=False,
            reason='recurring_interval',
        )

    if now_utc < due_at:
        return FollowupDecision(
            should_wake=False,
            actions=[],
            next_stage=stage,
            next_due_at=due_at,
            next_status='active',
            urgency='low',
            heads_up=False,
            reason='not_yet_due',
        )

    quiet_resume = shift_out_of_quiet_hours(now_utc, cfg, tz_name)
    if quiet_resume > now_utc and not metadata.get('bypass_quiet_hours'):
        if kind == 'non_negotiable':
            quiet_resume = max(quiet_resume, next_morning_checkin(quiet_resume, tz_name, settings=cfg))
        return FollowupDecision(
            should_wake=False,
            actions=[],
            next_stage=stage,
            next_due_at=quiet_resume,
            next_status='active',
            urgency='low',
            heads_up=False,
            reason='quiet_hours_deferred',
        )

    persistence = cfg.get('persistence') if cfg.get('persistence') in PERSISTENCE_PROFILES else 'Occasional'
    next_stage = stage + 1
    initial_action = 'morning_briefing' if (kind == 'non_negotiable' and local_hour < 11 and stage < 0) else 'check_in'

    if persistence == 'Minimal':
        if stage < 0:
            can_heads_up = (
                deadline is not None
                and (deadline - now_utc) > timedelta(minutes=30)
                and not metadata.get('heads_up_sent')
            )
            if can_heads_up:
                heads_at = shift_out_of_quiet_hours(deadline - timedelta(minutes=15), cfg, tz_name)
                if heads_at <= now_utc:
                    heads_at = now_utc + timedelta(minutes=15)
                return FollowupDecision(
                    should_wake=True,
                    actions=[initial_action],
                    next_stage=0,
                    next_due_at=heads_at,
                    next_status='active',
                    urgency='low',
                    heads_up=False,
                    reason='minimal_initial_with_heads_up',
                )
            if kind == 'non_negotiable':
                nxt = _tomorrow_morning_checkin(now_utc, tz_name, settings=cfg)
                return FollowupDecision(
                    should_wake=True,
                    actions=[initial_action],
                    next_stage=0,
                    next_due_at=nxt,
                    next_status='active',
                    urgency='low',
                    heads_up=False,
                    reason='minimal_non_negotiable_daily',
                )
            return FollowupDecision(
                should_wake=True,
                actions=[initial_action],
                next_stage=0,
                next_due_at=None,
                next_status='submitted',
                urgency='low',
                heads_up=False,
                reason='minimal_single_wake',
            )

        # Pre-deadline heads-up / final reminder wake in Minimal mode
        if kind == 'non_negotiable':
            nxt = _tomorrow_morning_checkin(now_utc, tz_name, settings=cfg)
            return FollowupDecision(
                should_wake=True,
                actions=['heads_up'],
                next_stage=next_stage,
                next_due_at=nxt,
                next_status='active',
                urgency='normal',
                heads_up=True,
                reason='minimal_deadline_heads_up',
            )
        return FollowupDecision(
            should_wake=True,
            actions=['heads_up'],
            next_stage=next_stage,
            next_due_at=None,
            next_status='submitted',
            urgency='normal',
            heads_up=True,
            reason='minimal_final_heads_up',
        )

    if persistence == 'Occasional':
        if stage < 0:
            nxt = shift_out_of_quiet_hours(now_utc + timedelta(hours=4), cfg, tz_name)
            return FollowupDecision(
                should_wake=True,
                actions=[initial_action],
                next_stage=0,
                next_due_at=nxt,
                next_status='active',
                urgency='normal',
                heads_up=False,
                reason='occasional_first_checkin',
            )
        follow_action = 'push' if 'push' in avail else 'check_in'
        if kind == 'non_negotiable':
            nxt = _tomorrow_morning_checkin(now_utc, tz_name, settings=cfg)
            return FollowupDecision(
                should_wake=True,
                actions=[follow_action],
                next_stage=max(1, next_stage),
                next_due_at=nxt,
                next_status='active',
                urgency='normal',
                heads_up=False,
                reason='occasional_roll_next_morning',
            )
        return FollowupDecision(
            should_wake=True,
            actions=[follow_action],
            next_stage=max(1, next_stage),
            next_due_at=None,
            next_status='submitted',
            urgency='normal',
            heads_up=False,
            reason='occasional_final_checkin',
        )

    if persistence == 'Regular':
        is_near_deadline = (
            deadline is not None
            and timedelta(seconds=0) <= (deadline - now_utc) <= timedelta(minutes=20)
            and not metadata.get('heads_up_sent')
        )
        if is_near_deadline:
            actions = ['heads_up']
            urgency = 'high'
            heads_flag = True
        elif stage < 0:
            actions = [initial_action]
            urgency = 'normal'
            heads_flag = False
        elif stage >= 2:
            actions = ['push' if 'push' in avail else 'check_in']
            urgency = 'high'
            heads_flag = False
        else:
            actions = ['check_in']
            urgency = 'normal'
            heads_flag = False

        gap = timedelta(hours=2) if stage >= 2 else timedelta(hours=3)
        heads_up_candidate = (
            deadline - timedelta(minutes=15)
            if (deadline is not None and not metadata.get('heads_up_sent'))
            else None
        )

        # Up to 4 check-ins/day (stages -1, 0, 1, 2 -> rolling once stage >= 3)
        if stage >= 3:
            if kind == 'non_negotiable':
                if heads_up_candidate is not None and heads_up_candidate > now_utc + timedelta(minutes=2):
                    nxt = shift_out_of_quiet_hours(heads_up_candidate, cfg, tz_name)
                    reason = 'regular_pre_deadline_heads_up'
                else:
                    nxt = _tomorrow_morning_checkin(now_utc, tz_name, settings=cfg)
                    reason = 'regular_roll_next_morning'
                return FollowupDecision(
                    should_wake=True,
                    actions=actions,
                    next_stage=next_stage,
                    next_due_at=nxt,
                    next_status='active',
                    urgency=urgency,
                    heads_up=heads_flag,
                    reason=reason,
                )
            if deadline is not None and now_utc < deadline and not is_near_deadline:
                cand = now_utc + gap
                if heads_up_candidate is not None and now_utc < heads_up_candidate < cand:
                    cand = heads_up_candidate
                elif deadline < cand:
                    cand = deadline
                nxt = shift_out_of_quiet_hours(cand, cfg, tz_name)
                return FollowupDecision(
                    should_wake=True,
                    actions=actions,
                    next_stage=next_stage,
                    next_due_at=nxt,
                    next_status='active',
                    urgency='high',
                    heads_up=heads_flag,
                    reason='regular_until_deadline',
                )
            return FollowupDecision(
                should_wake=True,
                actions=actions,
                next_stage=next_stage,
                next_due_at=None,
                next_status='submitted',
                urgency=urgency,
                heads_up=heads_flag,
                reason='regular_cap_reached',
            )

        cand = now_utc + gap
        if heads_up_candidate is not None and now_utc < heads_up_candidate < cand:
            cand = heads_up_candidate
            reason = 'regular_pre_deadline_heads_up'
        else:
            reason = 'regular_tightened_2h' if stage >= 2 else 'regular_3h'
        nxt = shift_out_of_quiet_hours(cand, cfg, tz_name)
        return FollowupDecision(
            should_wake=True,
            actions=actions,
            next_stage=next_stage,
            next_due_at=nxt,
            next_status='active',
            urgency=urgency,
            heads_up=heads_flag,
            reason=reason,
        )

    # 'Persistent' — dynamic tightening cadence, never stops until completed/paused/snoozed
    deadline_within_3h = deadline is not None and (deadline - now_utc) <= timedelta(hours=3)
    if stage >= 3 or local_hour >= 17 or deadline_within_3h:
        gap = timedelta(minutes=30)
        urgency = 'critical'
        actions = ['evening_push']
        if 'voice_note' in avail or bool(metadata.get('allow_voice')):
            actions.append('voice_note')
        reason = 'persistent_critical_30m'
    elif stage in (1, 2) or 15 <= local_hour < 17:
        gap = timedelta(minutes=45)
        urgency = 'high'
        actions = ['push']
        reason = 'persistent_escalated_45m'
    else:
        gap = timedelta(minutes=75)
        urgency = 'normal'
        actions = ['morning_briefing' if (local_hour < 11 and stage < 0) else 'check_in']
        reason = 'persistent_initial_75m'

    cand = now_utc + gap
    cand_local = cand.astimezone(tz)
    shifted = shift_out_of_quiet_hours(cand, cfg, tz_name)
    past_evening_sleep_boundary = (
        kind == 'non_negotiable'
        and bool(cfg.get('quietEnabled'))
        and (cand_local.hour >= 22 or (cand_local.date() > local_now.date() and cand_local.hour < 8))
    )
    if shifted > cand or past_evening_sleep_boundary:
        if kind == 'non_negotiable':
            nxt = max(shifted, next_morning_checkin(now_utc, tz_name, settings=cfg))
        else:
            nxt = shifted
        reason = f'{reason}_sleep_shifted'
    else:
        nxt = cand

    if nxt <= now_utc:
        nxt = now_utc + gap

    heads_flag = (
        deadline is not None
        and timedelta(seconds=0) <= (deadline - now_utc) <= timedelta(minutes=30)
        and not metadata.get('heads_up_sent')
    )
    return FollowupDecision(
        should_wake=True,
        actions=actions,
        next_stage=next_stage,
        next_due_at=nxt,
        next_status='active',
        urgency=urgency,
        heads_up=heads_flag,
        reason=reason,
    )


class _DecisionMap(dict):
    """Dict mapping `item_id -> FollowupDecision` whose iteration yields decisions in batch order.

    Preserves full `dict[str, FollowupDecision]` indexing, `.get()`, `in`, `.keys()`, `.items()`,
    and equality semantics while also supporting `zip(batch.items, batch.decisions)` in callers.
    """

    def __iter__(self):
        return iter(self.values())


@dataclass
class WakeBatch:
    route_id: str
    items: list[dict]
    decisions: dict[str, FollowupDecision]


def coalesce_due_batch(
    items_and_decisions: list[tuple[dict, FollowupDecision]],
) -> list[WakeBatch]:
    """Filter to `should_wake` items and coalesce per `route_id` in due order."""
    batches_by_route: dict[str, WakeBatch] = {}
    ordered_routes: list[str] = []
    for item, decision in items_and_decisions:
        if not decision or not decision.should_wake:
            continue
        route_id = str(item.get('route_id') or '')
        if route_id not in batches_by_route:
            batches_by_route[route_id] = WakeBatch(route_id=route_id, items=[], decisions=_DecisionMap())
            ordered_routes.append(route_id)
        batch = batches_by_route[route_id]
        batch.items.append(item)
        item_id = str(item.get('id') or len(batch.items))
        batch.decisions[item_id] = decision
    return [batches_by_route[r] for r in ordered_routes]


def _format_local_stamp(dt_val: datetime | str | None, tz: ZoneInfo) -> str:
    parsed = _coerce_optional_utc(dt_val)
    if parsed is None:
        return 'none'
    loc = parsed.astimezone(tz)
    return loc.strftime('%Y-%m-%d %H:%M %Z')


def build_action_first_wake_prompt(
    items: list[dict],
    decisions: dict[str, FollowupDecision] | list[FollowupDecision],
    settings: dict,
    *,
    now: datetime,
    effective_timezone: str = 'UTC',
) -> str:
    """Build an action-first, tone-calibrated wake prompt for one coalesced route batch."""
    now_utc = _require_aware_utc(now)
    cfg = settings or {}
    tz = _zone(effective_timezone)
    local_now = now_utc.astimezone(tz)
    cadence_info = describe_cadence(cfg)
    tone = cadence_info['tone']
    persistence = cadence_info['persistence']
    tone_rules = TONE_PROFILES[tone]['voice_rules']

    item_lines: list[str] = []
    max_nudge_number = 1
    wants_voice = False
    decisions_is_mapping = isinstance(decisions, dict)
    decisions_seq = list(decisions) if not decisions_is_mapping and decisions is not None else []
    for idx, item in enumerate(items, start=1):
        item_id = str(item.get('id') or f'item-{idx}')
        title = str(item.get('title') or 'Untitled responsibility').strip()
        kind = str(item.get('kind') or 'commitment')
        nudge_number = int(item.get('nudge_count') or 0) + 1
        max_nudge_number = max(max_nudge_number, nudge_number)
        if decisions_is_mapping:
            decision = decisions.get(item_id)
        else:
            decision = decisions_seq[idx - 1] if (idx - 1) < len(decisions_seq) else None
        urgency = getattr(decision, 'urgency', None) or 'normal'
        dec_actions = getattr(decision, 'actions', None) or ()
        actions = ', '.join(dec_actions) if dec_actions else 'check_in'
        if 'voice_note' in dec_actions:
            wants_voice = True
        deadline_str = _format_local_stamp(item.get('deadline_at'), tz)
        meta = item.get('metadata') if isinstance(item.get('metadata'), dict) else {}
        extras: list[str] = []
        if meta.get('notes'):
            extras.append(f"notes: {meta['notes']}")
        if meta.get('prep_hint'):
            extras.append(f"prep_hint: {meta['prep_hint']}")
        watch_change = item.get('watch_change') or meta.get('watch_change')
        if watch_change:
            extras.append(f"watch_change: {watch_change}")
        extra_suffix = f" | {' ; '.join(extras)}" if extras else ''
        item_lines.append(
            f"- [{idx}] id={item_id} | title=\"{title}\" | kind={kind} | check_in=#{nudge_number} | "
            f"urgency={urgency} | mode={actions} | deadline={deadline_str}{extra_suffix}"
        )

    persistence_guidance = f'Persistence={persistence} ({cadence_info["summary"]}).'
    if persistence == 'Persistent' and max_nudge_number >= 3:
        persistence_guidance += (
            ' You are following up again (check-in #3+) because this is on their active/non-negotiables list—'
            'acknowledge that directly and ask for either concrete action right now or an explicit snooze time.'
        )
    elif len(items) > 1:
        persistence_guidance += (
            f' Combine all {len(items)} due items into one cohesive, natural check-in instead of separate alerts.'
        )

    voice_guidance = (
        ' Voice escalation is active for this urgent follow-up: if text_to_speech is appropriate on this channel, '
        'you may pair your concise message with a short spoken check-in.'
        if wants_voice else ''
    )

    sections = [
        f"Proactive Follow-Up Turn ({local_now.strftime('%A %Y-%m-%d %H:%M %Z')}, timezone={effective_timezone}):",
        'Due responsibilities to address in this turn:',
        *item_lines,
        '',
        (
            'ACTION-FIRST DIRECTIVE: Do NOT act like a dumb alarm clock that merely parrots task titles. '
            'Before writing to the user, check whether there is a quick ~30-second prep step you can execute '
            'right now with your tools—such as checking a relevant file/note/status, drafting the message or email '
            'they need to send, summarizing what is blocking progress, or laying out an effortless 2-minute first '
            'micro-step so starting feels frictionless.'
        ),
        (
            f'TONE & PERSISTENCE POSTURE (tone={tone}, persistence={persistence}): {tone_rules} '
            f'{persistence_guidance}{voice_guidance}'
        ),
        (
            'OUTPUT RULE: Write directly to the user like a thoughtful human assistant—never emit bracketed '
            'system wake banners, robotic headers, or raw internal JSON. If an item was already completed, '
            'rescheduled, or needs to be paused based on what you find, call butler_manage (complete, snooze, pause) '
            'so state stays accurate.'
        ),
    ]
    return '\n'.join(sections)
