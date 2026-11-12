"""Model-driven follow-ups: safety gates, batching, and wake prompts.

There is NO fixed cadence here. The model decides when (and whether) to follow up: after every
wake it must reschedule the item (butler_manage snooze/update), complete it, or stop it. Code only
keeps safety gates (quiet hours) and batching. Tone/persistence are preference hints in the prompt.
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

# Preference HINTS shown to the model. No intervals, counts, or times: the model decides timing.
PERSISTENCE_PROFILES: dict[str, dict] = {
    'Minimal': {
        'persistence': 'Minimal',
        'summary': 'Owner prefers a light touch: follow up rarely, only when it clearly helps.',
        'hint': 'The owner wants you to follow up sparingly. Prefer a single well-timed nudge; let go early.',
    },
    'Occasional': {
        'persistence': 'Occasional',
        'summary': 'Owner prefers occasional follow-ups when something is genuinely still open.',
        'hint': 'Follow up when something is genuinely open, and step back if they are busy or silent.',
    },
    'Regular': {
        'persistence': 'Regular',
        'summary': 'Owner prefers steady follow-through on open items.',
        'hint': 'Keep steady follow-through on open items; adjust spacing to how they respond and what is at stake.',
    },
    'Persistent': {
        'persistence': 'Persistent',
        'summary': 'Owner wants you to keep pursuing open items until they are done, paused, or snoozed.',
        'hint': 'Keep pursuing open items until done, paused, or snoozed; tighten and go louder as stakes or deadlines rise, but stop if it would annoy rather than help.',
    },
}


WAKE_MARKER = 'Proactive Follow-Up Turn'
VISIBLE_WAKE_MARKER = 'Butler check-in.'


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
    """Describe the owner's tone/persistence PREFERENCES. Timing is chosen by the model, not by code."""
    cfg = settings or {}
    tone_key = cfg.get('tone') if cfg.get('tone') in TONE_PROFILES else 'Balanced'
    pers_key = cfg.get('persistence') if cfg.get('persistence') in PERSISTENCE_PROFILES else 'Occasional'
    t_prof = TONE_PROFILES[tone_key]
    p_prof = PERSISTENCE_PROFILES[pers_key]
    note = 'Model-driven: Butler decides when and whether to follow up; this is a preference, not a schedule.'
    return {
        'tone': tone_key,
        'tone_guide': t_prof['guide'],
        'persistence': pers_key,
        'summary': f"{tone_key} tone · {pers_key} persistence: {p_prof['summary']}",
        'non_negotiable_cadence': note,
        'commitment_cadence': note,
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
    """Normalize a schedule. No auto-cadence: an omitted due_at means 'due now' so the model is
    woken and chooses the real follow-up time itself. Only explicit values are ever stored."""
    now_utc = _require_aware_utc(now)
    norm_kind = (kind or 'commitment').strip() or 'commitment'
    deadline_dt = _coerce_optional_utc(deadline_at)
    if norm_kind == 'watch' and not interval_seconds:
        interval_seconds = 900  # mechanical polling interval for a change-watcher; override via interval_seconds
    if due_at is not None:
        return {
            'due_at': _require_aware_utc(due_at),
            'deadline_at': deadline_dt,
            'interval_seconds': interval_seconds,
            'schedule_reason': 'explicit_due_at',
        }
    if norm_kind == 'watch':
        due_dt, reason = now_utc + timedelta(seconds=interval_seconds), 'watch_poll_interval'
    else:
        due_dt, reason = now_utc, 'due_now_model_decides'
    shifted = shift_out_of_quiet_hours(due_dt, settings or {}, effective_timezone)
    if shifted != due_dt:
        reason += '_quiet_shifted'
    return {
        'due_at': shifted,
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
    """Decide ONLY whether to wake now. Code never picks the next follow-up time.

    After a wake the item moves to 'submitted' (awaiting the model). The model must reschedule it
    via butler_manage (snooze/update), or complete/stop it. Unrescheduled items are surfaced in the
    model's context every turn. Recurring items with a model-chosen interval_seconds just recur.
    """
    now_utc = _require_aware_utc(now)
    cfg = settings or {}
    tz_name = effective_timezone or item.get('timezone') or 'UTC'
    due_at = _coerce_optional_utc(item.get('due_at')) or now_utc
    raw_stage = item.get('stage', item.get('followup_stage', -1))
    stage = int(-1 if raw_stage is None else raw_stage)
    metadata = item.get('metadata') if isinstance(item.get('metadata'), dict) else {}

    if now_utc < due_at:
        return FollowupDecision(False, [], stage, due_at, 'active', 'low', reason='not_yet_due')

    quiet_resume = shift_out_of_quiet_hours(now_utc, cfg, tz_name)  # safety gate: quiet hours
    if quiet_resume > now_utc and not metadata.get('bypass_quiet_hours'):
        return FollowupDecision(False, [], stage, quiet_resume, 'active', 'low', reason='quiet_hours_deferred')

    interval_raw = item.get('interval_seconds')
    if interval_raw is not None and int(interval_raw) > 0:
        interval = timedelta(seconds=int(interval_raw))  # interval was chosen by the model/user
        steps = max(1, (now_utc - due_at) // interval + 1)
        next_due = shift_out_of_quiet_hours(due_at + steps * interval, cfg, tz_name)
        if next_due <= now_utc:
            next_due = now_utc + interval
        return FollowupDecision(True, ['check_in'], stage + 1, next_due, 'active', 'normal',
                                reason='model_chosen_interval')

    return FollowupDecision(True, ['check_in'], stage + 1, None, 'submitted', 'normal',
                            reason='awaiting_model_reschedule')


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


JUDGMENT_RULES = (
    'JUDGMENT (how a good chief of staff follows up):\n'
    '1. Read their availability before nudging: recent chat, calendar/time of day, quiet hours, anything they said '
    '("not now", "busy", "later"). If busy or overloaded, back off or batch; do not stop caring about the item.\n'
    '2. Learn from reactions: check recent chat for how they responded to earlier nudges like this one. If they '
    'ignored or brushed off this kind of message, do less of it; if they engaged, keep doing it. Save durable '
    'preferences ("don\'t remind me about X") to memory immediately.\n'
    '3. Urgent or blocking with a real time window (deadline, money, something that expires): come back sooner and '
    'more often, and go louder (voice note where available) only if it truly matters. Casual items: later and lighter.\n'
    '4. Never repeat a message word for word. Each nudge must add something: a new fact, your best guess, a prepared '
    'draft, or a smaller next step. No guilt-tripping, no machinery, ids or tool talk.\n'
    '5. If you are blocked on them, say exactly what you need. If the unanswered count below is rising, space out '
    'further or stop; continuing when it would annoy rather than help is the wrong call.\n'
    '6. Never invent a deadline, date, business closing time, or urgency. Preserve only time boundaries the user stated or you verified with a tool. Treat a stated window as a real boundary: if they say “this week,” do not schedule a wake after that week ends unless they extend the window; if it remains unresolved, pause it or ask at the next real exchange instead of silently rolling it into next week. Clarify an ambiguous cutoff before the window closes.\n'
    '7. Do not create a second responsibility for the same open task. Reuse and reschedule the existing item; after a reply, choose one next checkpoint rather than stacking overlapping reminders.\n'
    '8. A promise to check back counts only once the item shows a scheduled next time (butler_manage list).\n'
    '9. Honor timing and outreach boundaries literally. Never wake before a user-stated time they plan to act. If they say “ask me later, but make it the last nudge today,” schedule at most one later check-in that day; do not add any more that day. If they say only “last nudge today” without asking for another, do not schedule another that day.\n'
    '10. After an explicit ETA, do not wake before it. Make at most one check after the ETA; if they do not respond, do not schedule another countdown until they reply or materially new information changes the situation. This applies even when the ETA falls after an external deadline.\n'
    '11. No per-level intervals, escalation ladder, or fixed follow-up cadence. Choose every next time from this person’s context and what they said.'
)


def _engagement_line(engagement: dict | None, now_utc: datetime, tz: ZoneInfo) -> str:
    if not engagement:
        return 'ENGAGEMENT: no history recorded yet.'
    last_user = engagement.get('last_user_at')
    unanswered = int(engagement.get('unanswered_wakes') or 0)
    when = _format_local_stamp(last_user, tz) if last_user else 'never seen'
    return (
        f'ENGAGEMENT: unanswered proactive nudges since their last real message: {unanswered}; '
        f'their last message: {when}. Use this to decide whether to nudge, wait longer, or let it go.'
    )


def build_action_first_wake_prompt(
    items: list[dict],
    decisions: dict[str, FollowupDecision] | list[FollowupDecision],
    settings: dict,
    *,
    now: datetime,
    effective_timezone: str = 'UTC',
    engagement: dict | None = None,
) -> str:
    """Wake prompt: hands the model the situation and ownership of what happens next."""
    now_utc = _require_aware_utc(now)
    cfg = settings or {}
    tz = _zone(effective_timezone)
    local_now = now_utc.astimezone(tz)
    cadence_info = describe_cadence(cfg)
    tone = cadence_info['tone']
    persistence = cadence_info['persistence']
    tone_rules = TONE_PROFILES[tone]['voice_rules']

    item_lines: list[str] = []
    recurring = False
    for idx, item in enumerate(items, start=1):
        item_id = str(item.get('id') or f'item-{idx}')
        title = str(item.get('title') or 'Untitled responsibility').strip()
        kind = str(item.get('kind') or 'commitment')
        nudge_number = int(item.get('nudge_count') or 0) + 1
        if item.get('interval_seconds'):
            recurring = True
        last = _format_local_stamp(item.get('last_nudged_at'), tz)
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
            f"last_nudged={last} | deadline={deadline_str}{extra_suffix}"
        )

    sections = [
        f"{WAKE_MARKER} ({local_now.strftime('%A %Y-%m-%d %H:%M %Z')}, timezone={effective_timezone}):",
        'Open responsibilities that came due:',
        *item_lines,
        '',
        (
            'YOU OWN THE FOLLOW-UP. No schedule decides this for you. Before writing, look at what changed: '
            'recent chat, how they responded to earlier nudges, calendar/time of day, what is at stake. '
            'Then decide: nudge now (adding something new, not repeating yourself), stay silent if they are '
            'busy or it is not worth it, or let it go because it is done or they said not now.'
        ),
        (
            'ACTION-FIRST: do not parrot the title. If a ~30-second prep step helps (check a file/status, draft '
            'the message, summarize the blocker, shrink it to a 2-minute first step), do it before messaging.'
        ),
        (
            'BEFORE YOU END THIS TURN, close the loop for every item above using butler_manage: '
            'snooze with `until` set to the next touch YOU choose, or complete/stop if finished or dropped, or pause. '
            + ('(Items with a recurring interval will recur on their own interval; you may still change it.) ' if recurring else '')
            + 'If you do nothing the item stays "awaiting your decision" and will NOT wake again by itself.'
        ),
        _engagement_line(engagement, now_utc, tz),
        JUDGMENT_RULES,
        (
            f'OWNER PREFERENCES (hints, not a schedule): tone={tone}: {tone_rules} '
            f'persistence={persistence}: {PERSISTENCE_PROFILES[persistence]["hint"]}'
        ),
        (
            'OUTPUT RULE: Write directly to the user like a thoughtful human assistant. Never emit bracketed '
            'system banners, raw ids, or tool talk. If you decide not to message, end with just [SILENT].'
        ),
    ]
    return '\n'.join(sections)
