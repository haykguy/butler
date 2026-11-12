"""Session-independent SQLite responsibility storage and fenced work claims.

All datetimes accepted by this API must be timezone-aware. Claims are leases,
not an exactly-once delivery guarantee: external effects need idempotency keys.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid
from zoneinfo import ZoneInfo

UTC = timezone.utc
VALID_KINDS = ('commitment', 'non_negotiable', 'check_in', 'watch', 'open_loop')
_VALID_STATUSES = ('active', 'paused', 'stopped', 'completed', 'queued', 'submitted')
_UNSET = object()
PENDING_WAKE_TTL_SECONDS = 600
PENDING_WAKE_CLAIM_LEASE_SECONDS = 600

_MIGRATION_COLUMNS = (
    ('kind', "TEXT NOT NULL DEFAULT 'commitment'"),
    ('route_id', 'TEXT'),
    ('stage', 'INTEGER NOT NULL DEFAULT -1'),
    ('nudge_count', 'INTEGER NOT NULL DEFAULT 0'),
    ('last_nudged_at', 'TEXT'),
    ('deadline_at', 'TEXT'),
    ('completed_day', 'TEXT'),
)


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('an aware datetime is required')
    return value.astimezone(UTC)


def stamp(value):
    return utc(value).isoformat(timespec='microseconds')


def _normalize_timezone(timezone_name):
    if not isinstance(timezone_name, str) or not timezone_name.strip() or timezone_name.startswith('/') or '..' in timezone_name:
        raise ValueError('invalid timezone_name')
    ZoneInfo(timezone_name)
    return timezone_name


def _normalize_route_id(route_id):
    if route_id is None:
        return None
    if not isinstance(route_id, str):
        raise ValueError('route_id must be a string or None')
    stripped = route_id.strip()
    return stripped or None


class ResponsibilityStore:
    def __init__(self, path):
        path_str = str(path)
        if path_str != ':memory:':
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path_str, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS responsibilities (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, due_at TEXT NOT NULL,
          timezone TEXT NOT NULL, interval_seconds INTEGER,
          status TEXT NOT NULL CHECK(status IN ('active','paused','stopped','completed','queued','submitted')),
          metadata TEXT NOT NULL, claim_token TEXT, claim_until TEXT,
          generation INTEGER NOT NULL DEFAULT 0,
          kind TEXT NOT NULL DEFAULT 'commitment',
          route_id TEXT,
          stage INTEGER NOT NULL DEFAULT -1,
          nudge_count INTEGER NOT NULL DEFAULT 0,
          last_nudged_at TEXT,
          deadline_at TEXT,
          completed_day TEXT
        );
        CREATE INDEX IF NOT EXISTS responsibilities_due ON responsibilities(status,due_at);
        CREATE TABLE IF NOT EXISTS wake_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          item_id TEXT,
          timestamp TEXT NOT NULL,
          action TEXT NOT NULL,
          outcome TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS engagement (
          id INTEGER PRIMARY KEY CHECK(id=1),
          last_user_at TEXT, last_wake_at TEXT, unanswered_wakes INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS pending_wakes (
          id TEXT PRIMARY KEY, route_id TEXT NOT NULL, payload TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','claimed')),
          claim_token TEXT, claim_until TEXT
        );
        CREATE INDEX IF NOT EXISTS pending_wakes_route ON pending_wakes(route_id,state,created_at);
        CREATE INDEX IF NOT EXISTS wake_log_ts ON wake_log(timestamp DESC, id DESC);
        ''')
        self._migrate_schema()

    def _migrate_schema(self):
        existing = {
            row['name']
            for row in self.db.execute('PRAGMA table_info(responsibilities)').fetchall()
        }
        for col_name, col_spec in _MIGRATION_COLUMNS:
            if col_name not in existing:
                self.db.execute(f'ALTER TABLE responsibilities ADD COLUMN {col_name} {col_spec}')
        pending_columns = {
            row['name'] for row in self.db.execute('PRAGMA table_info(pending_wakes)').fetchall()
        }
        if 'claim_token' not in pending_columns:
            self.db.execute('ALTER TABLE pending_wakes ADD COLUMN claim_token TEXT')
        if 'claim_until' not in pending_columns:
            self.db.execute('ALTER TABLE pending_wakes ADD COLUMN claim_until TEXT')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def create(
        self,
        title,
        due_at,
        *,
        timezone_name='UTC',
        interval_seconds=None,
        metadata=None,
        id=None,
        kind='commitment',
        route_id=None,
        deadline_at=None,
    ):
        if not isinstance(title, str) or not title.strip():
            raise ValueError('title must not be empty')
        _normalize_timezone(timezone_name)
        if interval_seconds is not None and (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds <= 0):
            raise ValueError('interval_seconds must be a positive integer')
        if kind not in VALID_KINDS:
            raise ValueError(f'kind must be one of {VALID_KINDS}')
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError('metadata must be a dict')
        norm_route_id = _normalize_route_id(route_id)
        due_stamp = stamp(due_at)
        deadline_stamp = stamp(deadline_at) if deadline_at is not None else None
        item_id = id or str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO responsibilities(id,title,due_at,timezone,interval_seconds,status,metadata,kind,route_id,deadline_at) VALUES(?,?,?,?,?,'active',?,?,?,?)",
            (item_id, title, due_stamp, timezone_name, interval_seconds, json.dumps(metadata or {}), kind, norm_route_id, deadline_stamp),
        )
        return self.get(item_id)

    @staticmethod
    def _row(row):
        if row is None:
            return None
        item = dict(row)
        item['metadata'] = json.loads(item['metadata'])
        for key in ('due_at', 'claim_until', 'last_nudged_at', 'deadline_at'):
            if item.get(key) is not None:
                item[key] = datetime.fromisoformat(item[key].replace('Z', '+00:00'))
        return item

    def get(self, item_id):
        return self._row(self.db.execute('SELECT * FROM responsibilities WHERE id=?', (item_id,)).fetchone())

    def list(self):
        return [self._row(row) for row in self.db.execute('SELECT * FROM responsibilities ORDER BY due_at,id')]

    def update_item(
        self,
        item_id,
        *,
        title=_UNSET,
        due_at=_UNSET,
        timezone_name=_UNSET,
        interval_seconds=_UNSET,
        metadata=_UNSET,
        kind=_UNSET,
        route_id=_UNSET,
        deadline_at=_UNSET,
    ) -> dict | None:
        assignments = ['claim_token=NULL', 'claim_until=NULL', 'generation=generation+1']
        params = []

        if title is not _UNSET:
            if not isinstance(title, str) or not title.strip():
                raise ValueError('title must not be empty')
            assignments.append('title=?')
            params.append(title)
        if due_at is not _UNSET:
            assignments.append('due_at=?')
            params.append(stamp(due_at))
        if timezone_name is not _UNSET:
            assignments.append('timezone=?')
            params.append(_normalize_timezone(timezone_name))
        if interval_seconds is not _UNSET:
            if interval_seconds is not None and (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds <= 0):
                raise ValueError('interval_seconds must be a positive integer')
            assignments.append('interval_seconds=?')
            params.append(interval_seconds)
        if metadata is not _UNSET:
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError('metadata must be a dict')
            assignments.append('metadata=?')
            params.append(json.dumps(metadata or {}))
        if kind is not _UNSET:
            if kind not in VALID_KINDS:
                raise ValueError(f'kind must be one of {VALID_KINDS}')
            assignments.append('kind=?')
            params.append(kind)
        if route_id is not _UNSET:
            assignments.append('route_id=?')
            params.append(_normalize_route_id(route_id))
        if deadline_at is not _UNSET:
            assignments.append('deadline_at=?')
            params.append(stamp(deadline_at) if deadline_at is not None else None)

        params.append(item_id)
        sql = f"UPDATE responsibilities SET {', '.join(assignments)} WHERE id=?"
        result = self.db.execute(sql, params)
        if result.rowcount != 1:
            return None
        return self.get(item_id)

    def _transition(self, item_id, status, allowed):
        placeholders = ','.join('?' for _ in allowed)
        result = self.db.execute(
            f'UPDATE responsibilities SET status=?,claim_token=NULL,claim_until=NULL,generation=generation+1 WHERE id=? AND status IN ({placeholders})',
            (status, item_id, *allowed),
        )
        return result.rowcount == 1

    def pause(self, item_id, *, notes=None):
        if notes is None:
            return self._transition(item_id, 'paused', ('active', 'submitted'))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute(
                "SELECT metadata FROM responsibilities WHERE id=? AND status IN ('active','submitted')",
                (item_id,),
            ).fetchone()
            if row is None:
                self.db.execute('ROLLBACK')
                return False
            metadata = json.loads(row['metadata'] or '{}')
            metadata['notes'] = str(notes).strip()
            result = self.db.execute(
                """UPDATE responsibilities SET status='paused',
                   claim_token=NULL,claim_until=NULL,generation=generation+1,metadata=?
                   WHERE id=? AND status IN ('active','submitted')""",
                (json.dumps(metadata), item_id),
            )
            self.db.execute('COMMIT')
            return result.rowcount == 1
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def resume(self, item_id):
        return self._transition(item_id, 'active', ('paused',))

    def stop(self, item_id):
        return self._transition(item_id, 'stopped', ('active', 'paused', 'submitted'))

    def complete(self, item_id):
        return self._transition(item_id, 'completed', ('active', 'paused', 'submitted'))

    def snooze(self, item_id, until, *, notes=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute(
                "SELECT status,metadata FROM responsibilities WHERE id=? AND status IN ('active','paused','submitted')",
                (item_id,),
            ).fetchone()
            if row is None:
                self.db.execute('ROLLBACK')
                return False
            metadata_json = row['metadata']
            if notes is not None:
                metadata = json.loads(metadata_json or '{}')
                metadata['notes'] = str(notes).strip()
                metadata_json = json.dumps(metadata)
            result = self.db.execute(
                """UPDATE responsibilities SET due_at=?,status='active',
                   claim_token=NULL,claim_until=NULL,generation=generation+1,metadata=?
                   WHERE id=? AND status IN ('active','paused','submitted')""",
                (stamp(until), metadata_json, item_id),
            )
            self.db.execute('COMMIT')
            return result.rowcount == 1
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def reopen(self, item_id, *, due_at=None) -> bool:
        allowed = ('completed', 'submitted', 'paused', 'stopped', 'active')
        placeholders = ','.join('?' for _ in allowed)
        if due_at is not None:
            due_stamp = stamp(due_at)
            result = self.db.execute(
                f"UPDATE responsibilities SET status='active', due_at=?, completed_day=NULL, stage=-1, claim_token=NULL, claim_until=NULL, generation=generation+1 WHERE id=? AND status IN ({placeholders})",
                (due_stamp, item_id, *allowed),
            )
        else:
            result = self.db.execute(
                f"UPDATE responsibilities SET status='active', completed_day=NULL, stage=-1, claim_token=NULL, claim_until=NULL, generation=generation+1 WHERE id=? AND status IN ({placeholders})",
                (item_id, *allowed),
            )
        return result.rowcount == 1

    def delete(self, item_id) -> bool:
        result = self.db.execute('DELETE FROM responsibilities WHERE id=?', (item_id,))
        return result.rowcount == 1

    def mark_non_negotiable_done(self, item_id, local_date_str: str) -> bool:
        if not isinstance(local_date_str, str) or not local_date_str.strip():
            raise ValueError('local_date_str must be a non-empty string')
        result = self.db.execute(
            "UPDATE responsibilities SET status='completed', completed_day=?, claim_token=NULL, claim_until=NULL, generation=generation+1 WHERE id=? AND status IN ('active','paused','submitted','completed')",
            (local_date_str.strip(), item_id),
        )
        return result.rowcount == 1

    def reset_daily_non_negotiables(self, local_date_str: str, morning_due_at: datetime) -> int:
        if not isinstance(local_date_str, str) or not local_date_str.strip():
            raise ValueError('local_date_str must be a non-empty string')
        due_stamp = stamp(morning_due_at)
        clean_day = local_date_str.strip()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = self.db.execute(
                """
                UPDATE responsibilities
                SET status='active',
                    stage=-1,
                    nudge_count=0,
                    completed_day=NULL,
                    due_at=?,
                    claim_token=NULL,
                    claim_until=NULL,
                    generation=generation+1
                WHERE kind='non_negotiable'
                  AND status IN ('completed', 'submitted')
                  AND (completed_day IS NULL OR completed_day != ?)
                """,
                (due_stamp, clean_day),
            )
            self.db.execute('COMMIT')
            return result.rowcount
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def claim_due(self, now, *, limit=10, lease_seconds=300):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('limit must be a positive integer')
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError('lease_seconds must be a positive integer')
        now = utc(now)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows = self.db.execute(
                "SELECT id FROM responsibilities WHERE status='active' AND due_at<=? AND (claim_until IS NULL OR claim_until<=?) ORDER BY due_at,id LIMIT ?",
                (stamp(now), stamp(now), limit),
            ).fetchall()
            claimed = []
            for row in rows:
                token = str(uuid.uuid4())
                self.db.execute(
                    'UPDATE responsibilities SET claim_token=?,claim_until=? WHERE id=?',
                    (token, stamp(now + timedelta(seconds=lease_seconds)), row['id']),
                )
                claimed.append(self.get(row['id']))
            self.db.execute('COMMIT')
            return claimed
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def claim_is_current(self, item_id, token, now):
        row = self.db.execute(
            "SELECT 1 FROM responsibilities WHERE id=? AND status='active' AND claim_token=? AND claim_until>?",
            (item_id, token, stamp(now)),
        ).fetchone()
        return row is not None

    def finish_claim(self, item_id, token, now):
        """Acknowledge work if lease is current; coalesce missed intervals."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            now = utc(now)
            row = self.db.execute(
                "SELECT * FROM responsibilities WHERE id=? AND status='active' AND claim_token=? AND claim_until>?",
                (item_id, token, stamp(now)),
            ).fetchone()
            if row is None:
                self.db.execute('COMMIT')
                return False
            if row['interval_seconds'] is None:
                self.db.execute(
                    "UPDATE responsibilities SET status='submitted',claim_token=NULL,claim_until=NULL,generation=generation+1 WHERE id=?",
                    (item_id,),
                )
            else:
                due = datetime.fromisoformat(row['due_at'])
                interval = timedelta(seconds=row['interval_seconds'])
                steps = max(1, (now - due) // interval + 1)
                self.db.execute(
                    'UPDATE responsibilities SET due_at=?,claim_token=NULL,claim_until=NULL,generation=generation+1 WHERE id=?',
                    (stamp(due + steps * interval), item_id),
                )
            self.db.execute('COMMIT')
            return True
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def advance_followup(self, item_id, token, now, *, next_due_at=None, stage=-1, next_status='active') -> bool:
        """Advance follow-up stage/count under an active fenced lease."""
        if isinstance(stage, bool) or not isinstance(stage, int):
            raise ValueError('stage must be an integer')
        if next_status not in _VALID_STATUSES:
            raise ValueError(f'next_status must be one of {_VALID_STATUSES}')
        now_stamp = stamp(utc(now))
        next_due_stamp = stamp(next_due_at) if next_due_at is not None else None
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute(
                "SELECT due_at FROM responsibilities WHERE id=? AND status='active' AND claim_token=? AND claim_until>?",
                (item_id, token, now_stamp),
            ).fetchone()
            if row is None:
                self.db.execute('COMMIT')
                return False
            effective_due = next_due_stamp if next_due_stamp is not None else row['due_at']
            self.db.execute(
                """
                UPDATE responsibilities
                SET stage=?,
                    nudge_count=nudge_count+1,
                    last_nudged_at=?,
                    status=?,
                    due_at=?,
                    claim_token=NULL,
                    claim_until=NULL,
                    generation=generation+1
                WHERE id=?
                """,
                (stage, now_stamp, next_status, effective_due, item_id),
            )
            self.db.execute('COMMIT')
            return True
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def park_claim_for_reply(self, item_id, token, now) -> bool:
        """Park a gated proactive item until a real user turn; do not count a send or choose a retry time."""
        now_stamp = stamp(utc(now))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = self.db.execute(
                """UPDATE responsibilities SET status='submitted',claim_token=NULL,claim_until=NULL,
                   generation=generation+1 WHERE id=? AND status='active' AND claim_token=? AND claim_until>?""",
                (item_id, token, now_stamp),
            )
            self.db.execute('COMMIT')
            return result.rowcount == 1
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def defer_claim(self, item_id, token, now, *, until) -> bool:
        """Defer due_at under an active fenced lease without incrementing nudge_count."""
        now_stamp = stamp(utc(now))
        until_stamp = stamp(until)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = self.db.execute(
                """
                UPDATE responsibilities
                SET due_at=?,
                    claim_token=NULL,
                    claim_until=NULL,
                    generation=generation+1
                WHERE id=? AND status='active' AND claim_token=? AND claim_until>?
                """,
                (until_stamp, item_id, token, now_stamp),
            )
            self.db.execute('COMMIT')
            return result.rowcount == 1
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def release_claim(self, item_id, token, now):
        result = self.db.execute(
            "UPDATE responsibilities SET claim_token=NULL,claim_until=NULL WHERE id=? AND status='active' AND claim_token=? AND claim_until>?",
            (item_id, token, stamp(now)),
        )
        return result.rowcount == 1

    def get_engagement(self) -> dict:
        row = self.db.execute('SELECT last_user_at,last_wake_at,unanswered_wakes FROM engagement WHERE id=1').fetchone()
        out = {'last_user_at': None, 'last_wake_at': None, 'unanswered_wakes': 0}
        if row:
            out['unanswered_wakes'] = int(row['unanswered_wakes'])
            for k in ('last_user_at', 'last_wake_at'):
                if row[k]:
                    out[k] = datetime.fromisoformat(row[k].replace('Z', '+00:00'))
        return out

    def enqueue_pending_wake(self, route_id, payload, now, *, ttl_seconds=PENDING_WAKE_TTL_SECONDS):
        if not isinstance(route_id, str) or not route_id.strip():
            raise ValueError('route_id must be a non-empty string')
        if not isinstance(payload, str) or not payload.strip():
            raise ValueError('payload must be a non-empty string')
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise ValueError('ttl_seconds must be a positive integer')
        created = utc(now)
        wake_id = str(uuid.uuid4())
        self.db.execute(
            'INSERT INTO pending_wakes(id,route_id,payload,created_at,expires_at) VALUES (?,?,?,?,?)',
            (wake_id, route_id.strip(), payload, stamp(created), stamp(created + timedelta(seconds=ttl_seconds))),
        )
        return wake_id

    def cancel_pending_wake(self, wake_id) -> bool:
        result = self.db.execute("DELETE FROM pending_wakes WHERE id=? AND state='pending'", (wake_id,))
        return result.rowcount == 1

    def claim_pending_wake(self, route_id, now):
        now_utc = utc(now)
        now_stamp = stamp(now_utc)
        lease_stamp = stamp(now_utc + timedelta(seconds=PENDING_WAKE_CLAIM_LEASE_SECONDS))
        refreshed_expiry = stamp(now_utc + timedelta(seconds=PENDING_WAKE_TTL_SECONDS))
        claim_token = str(uuid.uuid4())
        self.db.execute('BEGIN IMMEDIATE')
        try:
            # A pre-hook may have been interrupted or vetoed by a later hook. Requeue
            # its lease with a fresh TTL; the fencing token prevents a late post-hook
            # from acknowledging a newer claimant's turn.
            self.db.execute(
                "UPDATE pending_wakes SET state='pending',claim_token=NULL,claim_until=NULL,expires_at=? "
                "WHERE state='claimed' AND (claim_until IS NULL OR claim_until<=?)",
                (refreshed_expiry, now_stamp),
            )
            self.db.execute("DELETE FROM pending_wakes WHERE state='pending' AND expires_at<=?", (now_stamp,))
            row = self.db.execute(
                "SELECT id,payload FROM pending_wakes WHERE route_id=? AND state='pending' AND expires_at>? ORDER BY created_at,id LIMIT 1",
                (route_id, now_stamp),
            ).fetchone()
            if row is None:
                self.db.execute('COMMIT')
                return None
            result = self.db.execute(
                "UPDATE pending_wakes SET state='claimed',claim_token=?,claim_until=? WHERE id=? AND state='pending'",
                (claim_token, lease_stamp, row['id']),
            )
            self.db.execute('COMMIT')
            return {'id': row['id'], 'payload': row['payload'], 'claim_token': claim_token} if result.rowcount == 1 else None
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def finish_pending_wake(self, wake_id, claim_token, now, *, visible_response: bool) -> bool:
        now_stamp = stamp(now)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute(
                "SELECT id FROM pending_wakes WHERE id=? AND state='claimed' AND claim_token=? AND claim_until>?",
                (wake_id, claim_token, now_stamp),
            ).fetchone()
            if row is None:
                self.db.execute('ROLLBACK')
                return False
            if visible_response:
                self.db.execute(
                    'INSERT INTO engagement(id,last_wake_at,unanswered_wakes) VALUES (1,?,1) '
                    'ON CONFLICT(id) DO UPDATE SET last_wake_at=excluded.last_wake_at, unanswered_wakes=unanswered_wakes+1',
                    (now_stamp,),
                )
            self.db.execute('DELETE FROM pending_wakes WHERE id=? AND claim_token=?', (wake_id, claim_token))
            self.db.execute('COMMIT')
            return True
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def note_user_reply(self, now) -> None:
        """A real user message arrived: the unanswered-nudge counter resets."""
        self.db.execute(
            'INSERT INTO engagement(id,last_user_at,unanswered_wakes) VALUES (1,?,0) '
            'ON CONFLICT(id) DO UPDATE SET last_user_at=excluded.last_user_at, unanswered_wakes=0',
            (stamp(now),),
        )

    def note_wake_sent(self, now) -> None:
        """Record a non-silent model reply finalized from a proactive wake (not injection acceptance)."""
        self.db.execute(
            'INSERT INTO engagement(id,last_wake_at,unanswered_wakes) VALUES (1,?,1) '
            'ON CONFLICT(id) DO UPDATE SET last_wake_at=excluded.last_wake_at, unanswered_wakes=unanswered_wakes+1',
            (stamp(now),),
        )

    def record_wake_log(self, item_id, now, action, outcome, detail='') -> None:
        if not isinstance(action, str) or not action.strip():
            raise ValueError('action must be a non-empty string')
        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError('outcome must be a non-empty string')
        self.db.execute(
            'INSERT INTO wake_log(item_id, timestamp, action, outcome, detail) VALUES (?, ?, ?, ?, ?)',
            (item_id, stamp(now), action.strip(), outcome.strip(), '' if detail is None else str(detail)),
        )

    def list_wake_logs(self, limit=30) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('limit must be a positive integer')
        rows = self.db.execute(
            'SELECT id, item_id, timestamp, action, outcome, detail FROM wake_log ORDER BY timestamp DESC, id DESC LIMIT ?',
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
