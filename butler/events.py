"""Durable, bounded ledger of *untrusted* externally observed conversation events.

No collection or delivery integration is provided. Delivery states are assertions
from the caller, not proof of receipt. Storage is plaintext; protect the DB and
its backups. Capacity is fail-closed: no event or checkpoint is silently evicted.
"""
from dataclasses import dataclass
from datetime import datetime
import os
import sqlite3


class LedgerFull(Exception):
    """An event could not be stored without exceeding a retention bound."""


class EventConflict(ValueError):
    """An existing scoped event ID was reused with different contents."""


class CheckpointConflict(ValueError):
    """A batch is stale, invalid, or not a contiguous next batch."""


@dataclass(frozen=True)
class Event:
    profile: str
    channel: str
    conversation: str
    event_id: str
    source_kind: str
    delivery_state: str
    timestamp: str
    text: str

    @property
    def untrusted(self):
        return True


@dataclass(frozen=True)
class Batch:
    profile: str
    channel: str
    conversation: str
    session: str
    previous: int
    sequences: tuple
    events: tuple


def _identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('identifiers must be nonempty strings')


class EventLedger:
    """SQLite ledger. Explicit commit acknowledges a whole returned batch.

    Limits count all retained rows and UTF-8 text bytes globally. They are stored
    on creation and must match on reopening. No automatic pruning is performed.
    Each delivery-state observation needs its own stable event ID.
    """
    def __init__(self, path, *, max_events=10000, max_text_bytes=10_000_000):
        for limit in (max_events, max_text_bytes):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError('limits must be positive integers')
        path = os.fspath(path)
        if path == ':memory:':
            raise ValueError('a durable file path is required')
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        try:
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), max_events INTEGER NOT NULL, max_bytes INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL, channel TEXT NOT NULL, conversation TEXT NOT NULL, event_id TEXT NOT NULL, source_kind TEXT NOT NULL, delivery_state TEXT NOT NULL, timestamp TEXT NOT NULL, text TEXT NOT NULL, text_bytes INTEGER NOT NULL, UNIQUE(profile, channel, conversation, event_id))')
            self.db.execute('CREATE INDEX IF NOT EXISTS event_scope ON events(profile, channel, conversation, seq)')
            self.db.execute('CREATE TABLE IF NOT EXISTS checkpoints (profile TEXT NOT NULL, channel TEXT NOT NULL, conversation TEXT NOT NULL, session TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(profile,channel,conversation,session))')
            self.db.execute('INSERT OR IGNORE INTO config VALUES (1, ?, ?)', (max_events, max_text_bytes))
            if self.db.execute('SELECT max_events,max_bytes FROM config WHERE id=1').fetchone() != (max_events, max_text_bytes):
                raise ValueError('retention limits differ from existing ledger')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            self.db.close()
            raise
        self.max_events, self.max_text_bytes = max_events, max_text_bytes

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def append(self, event):
        if not isinstance(event, Event):
            raise TypeError('expected Event')
        for value in (event.profile, event.channel, event.conversation, event.event_id, event.source_kind):
            _identifier(value)
        if event.delivery_state not in {'attempted', 'accepted', 'delivered', 'unknown'}:
            raise ValueError('invalid delivery state')
        if not isinstance(event.text, str) or not isinstance(event.timestamp, str):
            raise ValueError('text and timestamp must be strings')
        try:
            parsed = datetime.fromisoformat(event.timestamp.replace('Z', '+00:00'))
            if parsed.utcoffset() is None:
                raise ValueError('timestamp needs a timezone')
        except ValueError as exc:
            raise ValueError('timestamp must be ISO 8601 with timezone') from exc
        size = len(event.text.encode('utf-8'))
        values = (event.profile, event.channel, event.conversation, event.event_id, event.source_kind, event.delivery_state, event.timestamp, event.text)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT profile,channel,conversation,event_id,source_kind,delivery_state,timestamp,text FROM events WHERE profile=? AND channel=? AND conversation=? AND event_id=?', values[:4]).fetchone()
            if old is not None:
                if old != values:
                    raise EventConflict('event ID already exists with different contents')
                self.db.execute('COMMIT')
                return False
            count, used = self.db.execute('SELECT COUNT(*), COALESCE(SUM(text_bytes),0) FROM events').fetchone()
            if count >= self.max_events or used + size > self.max_text_bytes:
                raise LedgerFull('retention limit reached; event not stored')
            self.db.execute('INSERT INTO events(profile,channel,conversation,event_id,source_kind,delivery_state,timestamp,text,text_bytes) VALUES (?,?,?,?,?,?,?,?,?)', (*values, size))
            self.db.execute('COMMIT')
            return True
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def read(self, profile, channel, conversation, session, *, limit=100):
        for value in (profile, channel, conversation, session):
            _identifier(value)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('limit must be positive')
        scope = (profile, channel, conversation, session)
        row = self.db.execute('SELECT seq FROM checkpoints WHERE profile=? AND channel=? AND conversation=? AND session=?', scope).fetchone()
        previous = row[0] if row else 0
        rows = self.db.execute('SELECT seq,profile,channel,conversation,event_id,source_kind,delivery_state,timestamp,text FROM events WHERE profile=? AND channel=? AND conversation=? AND seq>? ORDER BY seq LIMIT ?', (*scope[:3], previous, limit)).fetchall()
        return Batch(*scope, previous, tuple(row[0] for row in rows), tuple(Event(*row[1:]) for row in rows))

    def commit(self, batch):
        if not isinstance(batch, Batch):
            raise TypeError('expected Batch')
        scope = (batch.profile, batch.channel, batch.conversation, batch.session)
        for value in scope:
            _identifier(value)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT seq FROM checkpoints WHERE profile=? AND channel=? AND conversation=? AND session=?', scope).fetchone()
            current = row[0] if row else 0
            if current != batch.previous or len(batch.sequences) != len(batch.events):
                raise CheckpointConflict('stale or malformed batch')
            rows = self.db.execute('SELECT seq,profile,channel,conversation,event_id,source_kind,delivery_state,timestamp,text FROM events WHERE profile=? AND channel=? AND conversation=? AND seq>? ORDER BY seq LIMIT ?', (*scope[:3], current, len(batch.events))).fetchall()
            if tuple(row[0] for row in rows) != batch.sequences or tuple(Event(*row[1:]) for row in rows) != batch.events:
                raise CheckpointConflict('batch does not match contiguous stored events')
            if rows:
                self.db.execute('INSERT INTO checkpoints VALUES (?,?,?,?,?) ON CONFLICT(profile,channel,conversation,session) DO UPDATE SET seq=excluded.seq', (*scope, rows[-1][0]))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
