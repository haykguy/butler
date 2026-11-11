"""Authenticated session/origin route capture and SQLite route registry.

Security invariant: turn route capture ONLY trusts task-bound session values
read through the public gateway.session_context.get_session_env seam, and only
when the session key cannot have come from the process environment (the public
reader falls back to os.environ when no ContextVar is bound). Cron sessions are
rejected. Never fall back to os.environ or hermes_cli.plugin_session.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3


def _hash_prefix(prefix: str, text: str) -> str:
    return f'{prefix}:' + hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def _nonempty_id(value) -> str:
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ''


def capture_bound_turn_route() -> dict | None:
    """Capture the task-bound non-cron session route, failing closed without ContextVars."""
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return None

    # get_session_env returns the task-bound ContextVar when one is bound here and
    # otherwise falls back to os.environ. A value identical to the ambient
    # environment value is therefore indistinguishable from an unbound/ambient
    # key, so fail closed on it; a differing value can only be the bound one.
    try:
        key = get_session_env('HERMES_SESSION_KEY', '')
    except Exception:
        return None
    if not isinstance(key, str) or not key.strip():
        return None
    if key == os.environ.get('HERMES_SESSION_KEY'):
        return None

    # A '1' (bound or ambient) means cron: either way, fail closed.
    if get_session_env('HERMES_CRON_SESSION', '') == '1':
        return None

    def _clean(name: str) -> str:
        val = get_session_env(name, '')
        if val is None:
            return ''
        return val.strip() if isinstance(val, str) else str(val).strip()

    platform = _clean('HERMES_SESSION_PLATFORM')
    source = _clean('HERMES_SESSION_SOURCE')

    clean_key = key.strip()
    route_id = _hash_prefix('sk', clean_key)
    return {
        'route_id': route_id,
        'kind': 'session_key',
        'session_key': clean_key,
        'origin': None,
        'platform': str(platform or source or 'local'),
    }


def capture_admitted_gateway_origin(*, platform='', source=None, session_key='', **kwargs) -> dict | None:
    """Validate an admitted gateway DM payload and build an authenticated route."""
    if not isinstance(source, dict):
        return None
    if source.get('chat_type') != 'dm' or source.get('shared_session'):
        return None
    if not isinstance(platform, str) or not platform.strip():
        return None
    if source.get('platform') != platform:
        return None

    chat_id = _nonempty_id(source.get('chat_id'))
    user_id = _nonempty_id(source.get('user_id'))
    if not chat_id or not user_id:
        return None

    raw_thread = source.get('thread_id')
    if raw_thread is not None and not isinstance(raw_thread, (str, int)) or isinstance(raw_thread, bool):
        return None

    clean_platform = platform.strip()
    origin = {
        'platform': clean_platform,
        'chat_id': chat_id,
        'chat_type': 'dm',
        'user_id': user_id,
        'thread_id': str(raw_thread or ''),
    }

    if isinstance(session_key, str) and session_key.strip():
        kind = 'session_key'
        sk = session_key.strip()
        route_id = _hash_prefix('sk', sk)
    else:
        kind = 'origin'
        sk = None
        route_id = _hash_prefix('orig', json.dumps(origin, sort_keys=True))

    return {
        'route_id': route_id,
        'kind': kind,
        'session_key': sk,
        'origin': origin,
        'platform': clean_platform,
    }


class RouteRegistry:
    """Durable SQLite registry of admitted routes authorized for proactive wakes."""

    def __init__(self, path):
        path_str = str(path)
        if path_str != ':memory:':
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path_str, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''
        CREATE TABLE IF NOT EXISTS admitted_routes (
          route_id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          session_key TEXT,
          origin_json TEXT,
          platform TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          is_preferred INTEGER NOT NULL DEFAULT 0
        )
        ''')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        origin_raw = row['origin_json']
        return {
            'route_id': row['route_id'],
            'kind': row['kind'],
            'session_key': row['session_key'],
            'origin': json.loads(origin_raw) if origin_raw else None,
            'platform': row['platform'],
            'updated_at': row['updated_at'],
            'is_preferred': bool(row['is_preferred']),
        }

    def record_route(self, route: dict, *, preferred: bool = True) -> str:
        if not isinstance(route, dict):
            raise ValueError('route must be a dict')
        route_id = route.get('route_id')
        kind = route.get('kind')
        platform = route.get('platform')
        if not isinstance(route_id, str) or not route_id.strip():
            raise ValueError('route_id must be a non-empty string')
        if kind not in ('session_key', 'origin'):
            raise ValueError("kind must be 'session_key' or 'origin'")
        if not isinstance(platform, str) or not platform.strip():
            raise ValueError('platform must be a non-empty string')

        session_key = route.get('session_key')
        if session_key is not None:
            if not isinstance(session_key, str) or not session_key.strip():
                raise ValueError('session_key must be a non-empty string or None')
            session_key = session_key.strip()
        if kind == 'session_key' and not session_key:
            raise ValueError('session_key route requires session_key')

        origin = route.get('origin')
        if origin is not None and not isinstance(origin, dict):
            raise ValueError('origin must be a dict or None')
        if kind == 'origin' and not origin:
            raise ValueError('origin route requires origin')

        clean_route_id = route_id.strip()
        clean_platform = platform.strip()
        origin_json = json.dumps(origin, sort_keys=True) if origin is not None else None
        now_iso = datetime.now(timezone.utc).isoformat(timespec='microseconds')

        self.db.execute('BEGIN IMMEDIATE')
        try:
            if preferred:
                self.db.execute('UPDATE admitted_routes SET is_preferred=0 WHERE is_preferred!=0')
                self.db.execute('''
                INSERT INTO admitted_routes(route_id, kind, session_key, origin_json, platform, updated_at, is_preferred)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(route_id) DO UPDATE SET
                  kind=excluded.kind,
                  session_key=COALESCE(excluded.session_key, admitted_routes.session_key),
                  origin_json=COALESCE(excluded.origin_json, admitted_routes.origin_json),
                  platform=excluded.platform,
                  updated_at=excluded.updated_at,
                  is_preferred=1
                ''', (clean_route_id, kind, session_key, origin_json, clean_platform, now_iso))
            else:
                self.db.execute('''
                INSERT INTO admitted_routes(route_id, kind, session_key, origin_json, platform, updated_at, is_preferred)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(route_id) DO UPDATE SET
                  kind=excluded.kind,
                  session_key=COALESCE(excluded.session_key, admitted_routes.session_key),
                  origin_json=COALESCE(excluded.origin_json, admitted_routes.origin_json),
                  platform=excluded.platform,
                  updated_at=excluded.updated_at
                ''', (clean_route_id, kind, session_key, origin_json, clean_platform, now_iso))
            self.db.execute('COMMIT')
            return clean_route_id
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def get_route(self, route_id: str) -> dict | None:
        if not isinstance(route_id, str) or not route_id.strip():
            return None
        row = self.db.execute('SELECT * FROM admitted_routes WHERE route_id=?', (route_id.strip(),)).fetchone()
        return self._row(row)

    def preferred_route(self) -> dict | None:
        row = self.db.execute(
            'SELECT * FROM admitted_routes ORDER BY is_preferred DESC, updated_at DESC, route_id ASC LIMIT 1'
        ).fetchone()
        return self._row(row)

    def list_routes(self) -> list[dict]:
        rows = self.db.execute(
            'SELECT * FROM admitted_routes ORDER BY is_preferred DESC, updated_at DESC, route_id ASC'
        ).fetchall()
        return [self._row(row) for row in rows]

    def remove_route(self, route_id: str) -> bool:
        if not isinstance(route_id, str) or not route_id.strip():
            return False
        result = self.db.execute('DELETE FROM admitted_routes WHERE route_id=?', (route_id.strip(),))
        return result.rowcount == 1
