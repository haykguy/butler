"""Session-independent SQLite responsibility storage and fenced work claims.

All datetimes accepted by this API must be timezone-aware. Claims are leases,
not an exactly-once delivery guarantee: external effects need idempotency keys.
"""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('an aware datetime is required')
    return value.astimezone(UTC)


def stamp(value):
    return utc(value).isoformat(timespec='microseconds')


class ResponsibilityStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS responsibilities (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, due_at TEXT NOT NULL,
          timezone TEXT NOT NULL, interval_seconds INTEGER,
          status TEXT NOT NULL CHECK(status IN ('active','paused','stopped','completed','queued','submitted')),
          metadata TEXT NOT NULL, claim_token TEXT, claim_until TEXT,
          generation INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS responsibilities_due ON responsibilities(status,due_at);
        ''')

    def close(self):
        self.db.close()

    def create(self, title, due_at, *, timezone_name='UTC', interval_seconds=None, metadata=None, id=None):
        if not isinstance(title, str) or not title.strip():
            raise ValueError('title must not be empty')
        ZoneInfo(timezone_name)
        if interval_seconds is not None and (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds <= 0):
            raise ValueError('interval_seconds must be a positive integer')
        item_id = id or str(uuid.uuid4())
        self.db.execute('INSERT INTO responsibilities(id,title,due_at,timezone,interval_seconds,status,metadata) VALUES(?,?,?,?,?,\'active\',?)',
                        (item_id, title, stamp(due_at), timezone_name, interval_seconds, json.dumps(metadata or {})))
        return self.get(item_id)

    @staticmethod
    def _row(row):
        if row is None:
            return None
        item = dict(row)
        item['metadata'] = json.loads(item['metadata'])
        for key in ('due_at', 'claim_until'):
            if item[key] is not None:
                item[key] = datetime.fromisoformat(item[key])
        return item

    def get(self, item_id):
        return self._row(self.db.execute('SELECT * FROM responsibilities WHERE id=?', (item_id,)).fetchone())

    def list(self):
        return [self._row(row) for row in self.db.execute('SELECT * FROM responsibilities ORDER BY due_at,id')]

    def _transition(self, item_id, status, allowed):
        placeholders = ','.join('?' for _ in allowed)
        result = self.db.execute(f'UPDATE responsibilities SET status=?,claim_token=NULL,claim_until=NULL,generation=generation+1 WHERE id=? AND status IN ({placeholders})', (status, item_id, *allowed))
        return result.rowcount == 1

    def pause(self, item_id):
        return self._transition(item_id, 'paused', ('active',))

    def resume(self, item_id):
        return self._transition(item_id, 'active', ('paused',))

    def stop(self, item_id):
        return self._transition(item_id, 'stopped', ('active', 'paused'))

    def complete(self, item_id):
        return self._transition(item_id, 'completed', ('active', 'paused'))

    def snooze(self, item_id, until):
        result = self.db.execute("UPDATE responsibilities SET due_at=?,claim_token=NULL,claim_until=NULL,generation=generation+1 WHERE id=? AND status IN ('active','paused')", (stamp(until), item_id))
        return result.rowcount == 1

    def claim_due(self, now, *, limit=10, lease_seconds=300):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('limit must be a positive integer')
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError('lease_seconds must be a positive integer')
        now = utc(now)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows = self.db.execute("SELECT id FROM responsibilities WHERE status='active' AND due_at<=? AND (claim_until IS NULL OR claim_until<=?) ORDER BY due_at,id LIMIT ?", (stamp(now), stamp(now), limit)).fetchall()
            claimed = []
            for row in rows:
                token = str(uuid.uuid4())
                self.db.execute('UPDATE responsibilities SET claim_token=?,claim_until=? WHERE id=?', (token, stamp(now + timedelta(seconds=lease_seconds)), row['id']))
                claimed.append(self.get(row['id']))
            self.db.execute('COMMIT')
            return claimed
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def claim_is_current(self, item_id, token, now):
        row = self.db.execute("SELECT 1 FROM responsibilities WHERE id=? AND status='active' AND claim_token=? AND claim_until>?", (item_id, token, stamp(now))).fetchone()
        return row is not None

    def finish_claim(self, item_id, token, now):
        """Acknowledge work if lease is current; coalesce missed intervals."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            now = utc(now)
            row = self.db.execute("SELECT * FROM responsibilities WHERE id=? AND status='active' AND claim_token=? AND claim_until>?", (item_id, token, stamp(now))).fetchone()
            if row is None:
                self.db.execute('COMMIT')
                return False
            if row['interval_seconds'] is None:
                self.db.execute("UPDATE responsibilities SET status='submitted',claim_token=NULL,claim_until=NULL,generation=generation+1 WHERE id=?", (item_id,))
            else:
                due = datetime.fromisoformat(row['due_at'])
                interval = timedelta(seconds=row['interval_seconds'])
                steps = max(1, (now - due) // interval + 1)
                self.db.execute('UPDATE responsibilities SET due_at=?,claim_token=NULL,claim_until=NULL,generation=generation+1 WHERE id=?', (stamp(due + steps * interval), item_id))
            self.db.execute('COMMIT')
            return True
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def release_claim(self, item_id, token, now):
        result = self.db.execute("UPDATE responsibilities SET claim_token=NULL,claim_until=NULL WHERE id=? AND status='active' AND claim_token=? AND claim_until>?", (item_id, token, stamp(now)))
        return result.rowcount == 1
