"""Local time conversion, timezone resolution, and session lifetime awareness.

ConversationContext preserves explicit caller-supplied event formatting.
Turn-level lifetime awareness inspects only numeric message timestamps from the
current turn's in-memory transcript plus validated profile settings.
"""
from dataclasses import dataclass
from datetime import datetime, time, timezone as dt_timezone
import json
import os
from pathlib import Path
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ObservedEvent:
    kind: str
    text: str
    observed_at: datetime

    def __post_init__(self):
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError('observed_at must be timezone-aware')


@dataclass(frozen=True)
class ConversationContext:
    timezone: str
    events: tuple[ObservedEvent, ...] = ()

    def __post_init__(self):
        ZoneInfo(self.timezone)
        object.__setattr__(self, 'events', tuple(self.events))


def _valid_iana_zone(candidate: str) -> str:
    if not isinstance(candidate, str):
        return ''
    zone = candidate.strip()
    if zone.startswith(':'):
        zone = zone[1:].strip()
    if not zone or len(zone) > 128 or zone.startswith('/') or '..' in zone:
        return ''
    try:
        ZoneInfo(zone)
        return zone
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ''


def local_time(instant: datetime, timezone: str) -> datetime:
    """Convert an aware instant using the explicitly configured IANA zone."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('instant must be timezone-aware')
    return instant.astimezone(ZoneInfo(timezone))


def detect_system_timezone(*, env=None, localtime_path='/etc/localtime',
                           timezone_file='/etc/timezone') -> str:
    """Detect the host's IANA timezone locally without network requests."""
    environ = os.environ if env is None else env
    tz_env = _valid_iana_zone(environ.get('TZ', ''))
    if tz_env:
        return tz_env
    try:
        lt = Path(localtime_path)
        if lt.exists() or lt.is_symlink():
            resolved = str(lt.resolve())
            for marker in ('/zoneinfo/', '/zoneinfo.default/'):
                if marker in resolved:
                    zone = _valid_iana_zone(resolved.split(marker, 1)[1])
                    if zone:
                        return zone
    except OSError:
        pass
    try:
        tz_path = Path(timezone_file)
        if tz_path.is_file():
            zone = _valid_iana_zone(tz_path.read_text(encoding='utf-8', errors='replace').splitlines()[0])
            if zone:
                return zone
    except (OSError, IndexError):
        pass
    return ''


def detect_ip_timezone(*, urlopen=urllib.request.urlopen, timeout: float = 3.0) -> str:
    """Opt-in IP-based IANA timezone lookup for onboarding verification only."""
    endpoints = (
        ('https://ipapi.co/timezone', 'text'),
        ('https://worldtimeapi.org/api/ip', 'json'),
    )
    for url, mode in endpoints:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'hermes-overseer/0.2'})
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read(4096).decode('utf-8', errors='replace').strip()
            if mode == 'json':
                payload = json.loads(body)
                raw = payload.get('timezone', '') if isinstance(payload, dict) else ''
            else:
                raw = body.splitlines()[0] if body else ''
            zone = _valid_iana_zone(raw)
            if zone:
                return zone
        except Exception:
            continue
    return ''


def resolve_effective_timezone(settings: dict, *, config_timezone: str = '',
                               system_timezone: str | None = None) -> tuple[str, str]:
    """Return (iana_timezone, source) from profile, Hermes config, OS, or UTC."""
    prof = _valid_iana_zone((settings or {}).get('timezone', ''))
    if prof:
        return prof, 'profile'
    cfg = _valid_iana_zone(config_timezone)
    if cfg:
        return cfg, 'hermes_config'
    sys_tz = _valid_iana_zone(detect_system_timezone() if system_timezone is None else system_timezone)
    if sys_tz:
        return sys_tz, 'system_detected'
    return 'UTC', 'utc_fallback'


def format_duration(seconds: float) -> str:
    """Format non-negative elapsed seconds concisely (e.g. '45s', '12m 5s', '3h 14m', '2d 5h')."""
    total = max(0, int(round(seconds)))
    if total < 60:
        return f'{total}s'
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f'{minutes}m {secs}s' if secs and minutes < 10 else f'{minutes}m'
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f'{hours}h {mins}m' if mins else f'{hours}h'
    days, hrs = divmod(hours, 24)
    return f'{days}d {hrs}h' if hrs else f'{days}d'


def _day_part(hour: int) -> str:
    if hour < 5:
        return 'late night'
    if hour < 8:
        return 'early morning'
    if hour < 12:
        return 'morning'
    if hour < 17:
        return 'afternoon'
    if hour < 21:
        return 'evening'
    return 'night'


def _in_quiet_hours(local_dt: datetime, settings: dict) -> bool:
    if not settings.get('quietEnabled') or not settings.get('quietStart') or not settings.get('quietEnd'):
        return False
    try:
        start = time.fromisoformat(settings['quietStart'])
        end = time.fromisoformat(settings['quietEnd'])
    except ValueError:
        return False
    current = local_dt.time().replace(tzinfo=None)
    return start <= current < end if start < end else current >= start or current < end


def _coerce_epoch(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            num = float(raw)
            if num > 0:
                return num
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def summarize_session_lifetime(conversation_history, now: datetime,
                               *, is_first_turn: bool = False) -> dict:
    """Compute session age and inter-message gaps using only message roles & timestamps."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    now_ts = now.timestamp()
    items = list(conversation_history) if isinstance(conversation_history, (list, tuple)) else []

    # Exclude the trailing user entry when it represents the just-staged current turn.
    prior_items = items[:-1] if items and isinstance(items[-1], dict) and items[-1].get('role') == 'user' else items

    all_ts: list[float] = []
    prior_user_ts: list[float] = []
    assistant_ts: list[float] = []
    user_turns = 0
    assistant_turns = 0

    for msg in items:
        if not isinstance(msg, dict):
            continue
        role = msg.get('role')
        if role == 'user':
            user_turns += 1
        elif role == 'assistant':
            assistant_turns += 1
        if role != 'system':
            ts = _coerce_epoch(msg.get('timestamp'))
            if ts is not None and ts <= now_ts + 5:
                all_ts.append(ts)

    for msg in prior_items:
        if not isinstance(msg, dict):
            continue
        role = msg.get('role')
        ts = _coerce_epoch(msg.get('timestamp'))
        if ts is None or ts > now_ts + 5:
            continue
        if role == 'user':
            prior_user_ts.append(ts)
        elif role == 'assistant':
            assistant_ts.append(ts)

    if is_first_turn and user_turns == 0:
        user_turns = 1

    session_age = max(0.0, now_ts - min(all_ts)) if all_ts else None
    since_prev_user = max(0.0, now_ts - prior_user_ts[-1]) if prior_user_ts else None
    since_last_assistant = max(0.0, now_ts - assistant_ts[-1]) if assistant_ts else None

    return {
        'is_first_turn': bool(is_first_turn or user_turns <= 1 and assistant_turns == 0),
        'user_turns': max(1, user_turns),
        'assistant_turns': assistant_turns,
        'session_age_seconds': session_age,
        'session_age_human': format_duration(session_age) if session_age is not None else None,
        'since_previous_user_seconds': since_prev_user,
        'since_previous_user_human': format_duration(since_prev_user) if since_prev_user is not None else None,
        'since_last_assistant_seconds': since_last_assistant,
        'since_last_assistant_human': format_duration(since_last_assistant) if since_last_assistant is not None else None,
    }


def build_lifetime_awareness_context(
    settings: dict,
    *,
    now: datetime | None = None,
    conversation_history=None,
    is_first_turn: bool = False,
    platform: str = '',
    config_timezone: str = '',
    system_timezone: str | None = None,
) -> str | None:
    """Render per-turn live local time, session lifetime, and notification/style posture."""
    if not isinstance(settings, dict):
        return None
    if settings.get('paused') or not settings.get('lifetimeAwarenessEnabled', True):
        return None

    current = now if now is not None else datetime.now(dt_timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=dt_timezone.utc)

    zone, source = resolve_effective_timezone(
        settings, config_timezone=config_timezone, system_timezone=system_timezone,
    )
    local_dt = local_time(current, zone)
    offset_raw = local_dt.strftime('%z')
    offset = f'UTC{offset_raw[:3]}:{offset_raw[3:]}' if len(offset_raw) == 5 else 'UTC'
    tz_abbr = local_dt.tzname() or zone
    part = _day_part(local_dt.hour)

    source_note = {
        'profile': 'confirmed in Overseer profile',
        'hermes_config': 'from Hermes config.yaml',
        'system_detected': 'auto-detected from host OS (unconfirmed in profile — confirm with user during onboarding if relevant)',
        'utc_fallback': 'UTC fallback (timezone not set — ask user or call overseer_controls to confirm timezone)',
    }[source]

    quiet_state = 'disabled'
    if settings.get('quietEnabled') and settings.get('quietStart') and settings.get('quietEnd'):
        active = _in_quiet_hours(local_dt, settings)
        quiet_state = (
            f"ACTIVE ({settings['quietStart']}–{settings['quietEnd']})"
            if active else f"inactive (window {settings['quietStart']}–{settings['quietEnd']})"
        )

    life = summarize_session_lifetime(conversation_history, current, is_first_turn=is_first_turn)
    if life['is_first_turn']:
        lifetime_line = 'Session lifetime: turn 1 (new conversation start; no prior messages in this session).'
    else:
        parts = [f"turn {life['user_turns']}"]
        if life['session_age_human']:
            parts.append(f"session age {life['session_age_human']}")
        if life['since_previous_user_human']:
            parts.append(f"elapsed since user's previous message {life['since_previous_user_human']}")
        if life['since_last_assistant_human']:
            parts.append(f"elapsed since your last reply {life['since_last_assistant_human']}")
        lifetime_line = 'Session lifetime: ' + '; '.join(parts) + '.'

    tone = settings.get('tone', 'Balanced')
    persistence = settings.get('persistence', 'Occasional')
    surface = f' on {platform}' if platform else ''

    lines = [
        f'[Overseer Live Time & Session Lifetime{surface}]',
        f"Local time: {local_dt.strftime('%A, %Y-%m-%d %H:%M:%S')} {tz_abbr} ({offset}, {zone} — {source_note}; {part}).",
        f'{lifetime_line} Use exact elapsed gaps to calibrate pacing—never guess wait durations, acknowledge long/overnight gaps naturally, and stay crisp on rapid exchanges.',
        f'Posture: tone={tone}, persistence={persistence}, quiet_hours={quiet_state}.',
    ]

    if settings.get('naturalStatusStyle', True):
        lines.append(
            'Communication & status load: write like a concise, attentive human—short natural messages, no robotic status banners or repetitive boilerplate.'
        )

    enforced = 'synced to Hermes config' if settings.get('applySystemControls') else 'profile-saved (not yet synced to Hermes config)'
    lines.append(
        f"System notification/load controls ({enforced}): warnings_muted={bool(settings.get('suppressWarningNotifications', True))}, "
        f"tool_progress={settings.get('toolProgressMode', 'off')}, interim_narration={bool(settings.get('interimAssistantMessages', False))}, "
        f"skill_nudges={bool(settings.get('skillNudgesEnabled', False))}, gateway_restart_banner={bool(settings.get('gatewayRestartNotifications', False))}. "
        'If the user asks to quiet or change system/provider/tool/session notifications or confirm their timezone, use overseer_controls.'
    )
    return '\n'.join(lines)
