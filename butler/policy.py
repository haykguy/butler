"""Profile-scoped opt-in decisions. Reservations count attempts, not delivery.

Call capture_allowed immediately before capture/read. Call reserve_wake immediately
before each gateway injection, including retries; a successful reservation is
consumed even if the gateway refuses or crashes. Decisions are fail-closed.
Settings changes and external injections cannot be made atomic across processes.
"""
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .settings import load

UTC = timezone.utc


def _is_aware_datetime(value) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _resolve_zone(tz_name: str) -> ZoneInfo:
    if not isinstance(tz_name, str) or not tz_name or len(tz_name) > 128 or tz_name.startswith('/') or '..' in tz_name:
        raise ValueError('invalid timezone')
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('invalid timezone') from exc


def _quiet_end_for_settings(settings: dict, now: datetime, tz: ZoneInfo) -> datetime | None:
    if not settings['quietEnabled']:
        return None
    local = now.astimezone(tz)
    start = time.fromisoformat(settings['quietStart'])
    end = time.fromisoformat(settings['quietEnd'])
    current = local.time().replace(tzinfo=None)
    if start < end:
        if not (start <= current < end):
            return None
        end_date = local.date()
    else:
        if not (current >= start or current < end):
            return None
        end_date = local.date() + timedelta(days=1) if current >= start else local.date()
    end_local = datetime.combine(end_date, end, tzinfo=tz)
    end_utc = end_local.astimezone(UTC)
    if end_utc <= now.astimezone(UTC):
        end_utc = datetime.combine(end_date, end, tzinfo=tz, fold=1).astimezone(UTC)
    return end_utc


class Policy:
    def __init__(self, directory):
        self.directory = Path(directory)

    def capture_allowed(self):
        try:
            settings = load(self.directory)
            return settings['activityEnabled'] and not settings['paused']
        except (OSError, ValueError, TypeError):
            return False

    def _read_used_today(self, day: str) -> int:
        db_path = self.directory / 'policy.sqlite3'
        if not db_path.exists():
            return 0
        with closing(sqlite3.connect(db_path, timeout=30, isolation_level=None)) as db:
            has_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wake_attempts'"
            ).fetchone()
            if not has_table:
                return 0
            row = db.execute('SELECT count FROM wake_attempts WHERE day=?', (day,)).fetchone()
            return int(row[0]) if row else 0

    def quiet_window_end(self, now: datetime, *, fallback_timezone: str = '') -> datetime | None:
        """Return aware UTC datetime when active quiet window ends, or None if not in quiet hours."""
        try:
            if not _is_aware_datetime(now):
                return None
            settings = load(self.directory)
            if not settings['quietEnabled']:
                return None
            tz_name = settings['timezone'] or fallback_timezone
            if not tz_name:
                return None
            tz = _resolve_zone(tz_name)
            return _quiet_end_for_settings(settings, now, tz)
        except (OSError, ValueError, TypeError):
            return None

    def evaluate_wake(self, now: datetime, *, fallback_timezone: str = '') -> dict:
        """Non-consuming guardrail evaluation with deferral target and quota telemetry."""
        if not _is_aware_datetime(now):
            return {
                'allowed': False,
                'reason': 'invalid_now',
                'defer_until': None,
                'used_today': 0,
                'daily_limit': None,
                'in_quiet_hours': False,
            }
        try:
            settings = load(self.directory)
            daily_limit = settings['dailyLimit']
            tz_name = settings['timezone'] or fallback_timezone
            needs_tz = bool(settings['quietEnabled'] or daily_limit is not None)

            tz = _resolve_zone(tz_name) if tz_name else (None if needs_tz else ZoneInfo('UTC'))
            local = now.astimezone(tz) if tz is not None else None
            quiet_end = _quiet_end_for_settings(settings, now, tz) if tz is not None else None
            in_quiet_hours = quiet_end is not None
            used_today = self._read_used_today(local.date().isoformat()) if local is not None else 0

            if settings['paused']:
                return {
                    'allowed': False,
                    'reason': 'paused',
                    'defer_until': None,
                    'used_today': used_today,
                    'daily_limit': daily_limit,
                    'in_quiet_hours': in_quiet_hours,
                }
            if not settings['notificationsEnabled']:
                return {
                    'allowed': False,
                    'reason': 'notifications_disabled',
                    'defer_until': None,
                    'used_today': used_today,
                    'daily_limit': daily_limit,
                    'in_quiet_hours': in_quiet_hours,
                }
            if needs_tz and tz is None:
                return {
                    'allowed': False,
                    'reason': 'timezone_required',
                    'defer_until': None,
                    'used_today': 0,
                    'daily_limit': daily_limit,
                    'in_quiet_hours': False,
                }
            if in_quiet_hours:
                return {
                    'allowed': False,
                    'reason': 'quiet_hours',
                    'defer_until': quiet_end,
                    'used_today': used_today,
                    'daily_limit': daily_limit,
                    'in_quiet_hours': True,
                }
            if daily_limit is not None and (daily_limit == 0 or used_today >= daily_limit):
                next_day = local.date() + timedelta(days=1)
                wake_time = time.fromisoformat(settings['quietEnd']) if settings['quietEnabled'] else time(8, 0)
                defer_until = datetime.combine(next_day, wake_time, tzinfo=tz).astimezone(UTC)
                return {
                    'allowed': False,
                    'reason': 'daily_limit_reached',
                    'defer_until': defer_until,
                    'used_today': used_today,
                    'daily_limit': daily_limit,
                    'in_quiet_hours': False,
                }
            return {
                'allowed': True,
                'reason': 'ok',
                'defer_until': None,
                'used_today': used_today,
                'daily_limit': daily_limit,
                'in_quiet_hours': False,
            }
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return {
                'allowed': False,
                'reason': 'invalid_configuration',
                'defer_until': None,
                'used_today': 0,
                'daily_limit': None,
                'in_quiet_hours': False,
            }

    def wake_quota_status(self, now: datetime, *, fallback_timezone: str = '') -> dict:
        """Read-only summary of wake quota, quiet hours, and policy state."""
        try:
            settings = load(self.directory)
            paused = bool(settings['paused'])
            notifications_enabled = bool(settings['notificationsEnabled'])
        except (OSError, ValueError, TypeError):
            return {
                'used_today': 0,
                'daily_limit': None,
                'remaining_today': None,
                'in_quiet_hours': False,
                'quiet_until': None,
                'paused': False,
                'notifications_enabled': False,
                'allowed_now': False,
                'reason': 'invalid_configuration',
            }

        ev = self.evaluate_wake(now, fallback_timezone=fallback_timezone)
        daily_limit = ev['daily_limit']
        used_today = ev['used_today']
        remaining_today = max(0, daily_limit - used_today) if daily_limit is not None else None
        quiet_end = self.quiet_window_end(now, fallback_timezone=fallback_timezone) if ev['in_quiet_hours'] else None
        return {
            'used_today': used_today,
            'daily_limit': daily_limit,
            'remaining_today': remaining_today,
            'in_quiet_hours': ev['in_quiet_hours'],
            'quiet_until': quiet_end.isoformat() if quiet_end is not None else None,
            'paused': paused,
            'notifications_enabled': notifications_enabled,
            'allowed_now': ev['allowed'],
            'reason': ev['reason'],
        }

    def evaluate_owner_ring(
        self, now: datetime, *, urgent: bool = False, fallback_timezone: str = ''
    ) -> dict:
        """Evaluate policy-level gates (pause + quiet hours / breakthrough) for an owner phone ring."""
        if not _is_aware_datetime(now):
            return {'allowed': False, 'reason': 'invalid_now', 'in_quiet_hours': False, 'quiet_until': None}
        try:
            settings = load(self.directory)
            if settings['paused']:
                return {'allowed': False, 'reason': 'paused', 'in_quiet_hours': False, 'quiet_until': None}
            if not settings.get('phoneEnabled') or not settings.get('ownerRingEnabled'):
                return {'allowed': False, 'reason': 'owner_ring_disabled', 'in_quiet_hours': False, 'quiet_until': None}
            tz_name = str(settings['timezone'] or fallback_timezone)
            if settings['quietEnabled'] and not tz_name:
                return {'allowed': False, 'reason': 'timezone_required', 'in_quiet_hours': False, 'quiet_until': None}
            tz = _resolve_zone(tz_name) if tz_name else ZoneInfo('UTC')
            quiet_end = _quiet_end_for_settings(settings, now, tz)
            if quiet_end is not None:
                if urgent and settings.get('quietHoursBreakthrough'):
                    return {
                        'allowed': True,
                        'reason': 'quiet_hours_breakthrough',
                        'in_quiet_hours': True,
                        'quiet_until': quiet_end.isoformat(),
                    }
                return {
                    'allowed': False,
                    'reason': 'quiet_hours',
                    'in_quiet_hours': True,
                    'quiet_until': quiet_end.isoformat(),
                }
            return {'allowed': True, 'reason': 'ok', 'in_quiet_hours': False, 'quiet_until': None}
        except (OSError, ValueError, TypeError):
            return {'allowed': False, 'reason': 'invalid_configuration', 'in_quiet_hours': False, 'quiet_until': None}

    def release_wake(self, now, *, fallback_timezone: str = '') -> None:
        """Refund one reserved wake whose delivery the host refused (it never reached the owner)."""
        try:
            settings = load(self.directory)
            tz_name = settings['timezone'] or fallback_timezone
            tz = _resolve_zone(tz_name) if tz_name else ZoneInfo('UTC')
            day = now.astimezone(tz).date().isoformat()
            with closing(sqlite3.connect(self.directory / 'policy.sqlite3', timeout=30, isolation_level=None)) as db:
                db.execute('CREATE TABLE IF NOT EXISTS wake_attempts (day TEXT PRIMARY KEY, count INTEGER NOT NULL)')
                db.execute('UPDATE wake_attempts SET count=count-1 WHERE day=? AND count>0', (day,))
        except (OSError, ValueError, TypeError, sqlite3.Error):
            pass

    def reserve_wake(self, now, *, fallback_timezone: str = ''):
        """Reserve one wake attempt atomically across processes, or deny it."""
        try:
            if not _is_aware_datetime(now):
                return False
            settings = load(self.directory)
            if settings['paused'] or not settings['notificationsEnabled']:
                return False
            tz_name = settings['timezone'] or fallback_timezone
            if (settings['quietEnabled'] or settings['dailyLimit'] is not None) and not tz_name:
                return False
            tz = _resolve_zone(tz_name) if tz_name else ZoneInfo('UTC')
            local = now.astimezone(tz)
            if settings['quietEnabled']:
                start = time.fromisoformat(settings['quietStart'])
                end = time.fromisoformat(settings['quietEnd'])
                current = local.time().replace(tzinfo=None)
                if (start <= current < end if start < end else current >= start or current < end):
                    return False
            limit = settings['dailyLimit']
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with closing(sqlite3.connect(self.directory / 'policy.sqlite3', timeout=30, isolation_level=None)) as db:
                db.execute('CREATE TABLE IF NOT EXISTS wake_attempts (day TEXT PRIMARY KEY, count INTEGER NOT NULL)')
                db.execute('BEGIN IMMEDIATE')
                day = local.date().isoformat()
                row = db.execute('SELECT count FROM wake_attempts WHERE day=?', (day,)).fetchone()
                if limit is not None and (limit == 0 or (row and row[0] >= limit)):
                    db.execute('ROLLBACK')
                    return False
                db.execute('INSERT INTO wake_attempts(day,count) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET count=count+1', (day,))
                db.execute('COMMIT')
                return True
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return False
