"""Profile-scoped opt-in decisions. Reservations count attempts, not delivery.

Call capture_allowed immediately before capture/read. Call reserve_wake immediately
before each gateway injection, including retries; a successful reservation is
consumed even if the gateway refuses or crashes. Decisions are fail-closed.
Settings changes and external injections cannot be made atomic across processes.
"""
from contextlib import closing
from datetime import datetime, time
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .settings import load


class Policy:
    def __init__(self, directory):
        self.directory = Path(directory)

    def capture_allowed(self):
        try:
            settings = load(self.directory)
            return settings['activityEnabled'] and not settings['paused']
        except (OSError, ValueError, TypeError):
            return False

    def reserve_wake(self, now):
        """Reserve one wake attempt atomically across processes, or deny it."""
        try:
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                return False
            settings = load(self.directory)
            if settings['paused'] or not settings['notificationsEnabled']:
                return False
            if (settings['quietEnabled'] or settings['dailyLimit'] is not None) and not settings['timezone']:
                return False
            local = now.astimezone(ZoneInfo(settings['timezone'] or 'UTC'))
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
