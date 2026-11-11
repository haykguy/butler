"""Structured, opt-in observation checks; no evaluation of generated code.

URL checks are disabled unless a *trusted* transport is supplied. That transport
must itself prevent DNS rebinding, private-address access, redirects and proxies;
allowlisting a URL alone is not sufficient to defend against SSRF. This module
does not ship a network transport. Probe callables are trusted host code, not
user-provided source. The optional SQLite connection can be a ResponsibilityStore
connection; the caller owns and closes it. Checks do not schedule or notify.

File allowlisting authorizes an exact absolute pathname, not a particular inode
or immutable content. Symlink components are rejected, but hard links, mount
changes, replacement of regular files, and concurrent content changes are not
prevented; allowlisted paths and their directories must be trusted accordingly.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
import sqlite3
import stat
from urllib.parse import urlsplit


@dataclass(frozen=True)
class WatchPolicy:
    files: frozenset = field(default_factory=frozenset)
    urls: frozenset = field(default_factory=frozenset)
    probes: dict = field(default_factory=dict)
    max_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class WatchResult:
    changed: bool
    digest: str
    previous_digest: str | None


class WatcherEngine:
    """Compare successive observations. First observation establishes baseline.

    State is keyed by watcher ID and canonical check specification. A changed
    specification resets the baseline. A failed check never changes state.
    Use distinct IDs for independent watches. Concurrent calls require distinct
    SQLite connections; the transaction serializes observations and updates,
    including across different IDs. Slow probes hold the SQLite write lock;
    trusted probes must not re-enter checks using the same database.
    """
    def __init__(self, policy, db=None, *, url_transport=None):
        if isinstance(policy.max_bytes, bool) or not isinstance(policy.max_bytes, int) or not 1 <= policy.max_bytes <= 10 * 1024 * 1024:
            raise ValueError('invalid maximum observation size')
        self.policy = policy
        self.db = db if db is not None else sqlite3.connect(':memory:', isolation_level=None)
        self.url_transport = url_transport
        self.db.execute('CREATE TABLE IF NOT EXISTS watcher_state (watch_id TEXT PRIMARY KEY, spec TEXT NOT NULL, digest TEXT NOT NULL)')

    def _file(self, path):
        if not isinstance(path, str) or path not in self.policy.files or not os.path.isabs(path):
            raise ValueError('file not allowlisted')
        components = path.split('/')[1:]
        if not components or any(part in ('', '.', '..') for part in components):
            raise ValueError('invalid path')
        # Open each component relative to an already-open directory, disallowing
        # symlinks at every step to avoid path traversal and symlink swaps.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open('/', flags | os.O_DIRECTORY)
        try:
            for index, component in enumerate(components):
                new_fd = os.open(component, flags | (os.O_DIRECTORY if index < len(components) - 1 else 0), dir_fd=fd)
                os.close(fd)
                fd = new_fd
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('only regular files are allowed')
            with os.fdopen(fd, 'rb') as stream:
                fd = -1
                return stream.read(self.policy.max_bytes + 1)
        except OSError as exc:
            raise ValueError('cannot safely open file') from exc
        finally:
            if fd != -1:
                os.close(fd)

    def _observe(self, spec):
        if not isinstance(spec, dict):
            raise ValueError('check must be an object')
        kind = spec.get('kind')
        fields = {'file': 'path', 'probe': 'name', 'url': 'url'}
        if kind not in fields or set(spec) != {'kind', fields.get(kind)}:
            raise ValueError('invalid check specification')
        value = spec[fields[kind]]
        if not isinstance(value, str) or not value:
            raise ValueError('invalid check target')
        if kind == 'file':
            result = self._file(value)
        elif kind == 'probe':
            probe = self.policy.probes.get(value)
            if not callable(probe):
                raise ValueError('probe not allowlisted')
            result = probe()
        else:
            try:
                parts = urlsplit(value)
                valid = (parts.scheme == 'https' and parts.hostname and parts.username is None
                         and parts.password is None and not parts.fragment and parts.port in (None, 443))
            except ValueError:
                valid = False
            if not valid or value not in self.policy.urls or self.url_transport is None:
                raise ValueError('URL check not authorized or transport unavailable')
            result = self.url_transport(value, self.policy.max_bytes)
        if isinstance(result, str):
            result = result.encode('utf-8')
        if not isinstance(result, bytes) or len(result) > self.policy.max_bytes:
            raise ValueError('invalid or oversized observation')
        return result

    def check(self, watch_id, spec):
        if not isinstance(watch_id, str) or not watch_id or len(watch_id) > 256:
            raise ValueError('invalid watcher id')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            data = self._observe(spec)
            digest = hashlib.sha256(data).hexdigest()
            canonical = json.dumps(spec, sort_keys=True, separators=(',', ':'))
            row = self.db.execute('SELECT spec,digest FROM watcher_state WHERE watch_id=?', (watch_id,)).fetchone()
            previous = row[1] if row and row[0] == canonical else None
            self.db.execute('INSERT INTO watcher_state(watch_id,spec,digest) VALUES(?,?,?) ON CONFLICT(watch_id) DO UPDATE SET spec=excluded.spec,digest=excluded.digest', (watch_id, canonical, digest))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return WatchResult(previous is not None and previous != digest, digest, previous)
