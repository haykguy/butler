"""LiveKit-only phone safety gates, SQLite plan/call ledger, and stdlib Twirp dispatcher.

Architecture:
- Zero third-party / binary dependencies in core Butler: LiveKit HS256 JWT minting
  and Twirp HTTPS RPCs (`AgentDispatchService/CreateDispatch` and
  `SIP/CreateSIPParticipant`) use Python stdlib (`hmac`, `hashlib`, `urllib.request`).
- Secrets are resolved strictly through Hermes `agent.secret_scope.get_secret`
  (declared in `plugin.yaml` `requires_env`); never read `.env` or keychains directly.
- Outbound third-party calls enforce a non-blocking double gate:
    1. `draft_plan` writes `pending_approval` with canonical SHA-256 script digest.
    2. Human approves via Desktop Control Center or `python -m butler.phone approve <id>`.
       No LLM tool can approve a plan.
    3. `place_call` verifies approval status, 24h TTL, SHA-256 digest, emergency/premium
       blocklist, `phone_stopped` kill-switch, and single-active-call lock before dispatch.
    4. `verify_outbound_ready_for_sip` re-verifies all invariants inside `voice_worker`
       immediately before `SIP/CreateSIPParticipant`.
- Owner escalation rings (`ring_owner`) enforce `phoneEnabled`, `ownerRingEnabled`,
  `phone_stopped` kill-switch (including verbal "stop calling me" latch), Quiet Hours
  (with optional `quietHoursBreakthrough` when `urgent=True`), `maxRingsPerHour` (default 3),
  `minRingGapSeconds` (default 120s), and the single-active-call lock.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .paths import butler_data_dir
from .policy import Policy
from .settings import load as load_settings

UTC = timezone.utc

PLAN_TTL_SECONDS = 86400  # 24h approval validity window
ACTIVE_CALL_STALE_SECONDS = 1800  # 30m stale lease safeguard if worker crashes
CALLBACK_LOOKBACK_DAYS = 7

PHONE_ENV_VARS = (
    'LIVEKIT_URL',
    'LIVEKIT_API_KEY',
    'LIVEKIT_API_SECRET',
    'LIVEKIT_SIP_TRUNK_ID',
    'LIVEKIT_CALLER_NUMBER',
)

_REQUIRED_DISPATCH_ENV = (
    'LIVEKIT_URL',
    'LIVEKIT_API_KEY',
    'LIVEKIT_API_SECRET',
    'LIVEKIT_SIP_TRUNK_ID',
)

_STRIP_CHARS_RE = re.compile(r'[\s\-\.\(\)]+')
_E164_STRICT_RE = re.compile(r'^\+[1-9]\d{6,14}$')
_ALLOWED_DTMF_RE = re.compile(r'^[0-9*#wW]{0,32}$')
_AI_DISCLOSURE_RE = re.compile(
    r'\b(?:ai|a\.i\.|automated|artificial\s+intelligence|virtual\s+assistant|digital\s+assistant)\b',
    re.IGNORECASE,
)

# Emergency & short-code numbers blocked across regions before and after normalization.
_EMERGENCY_EXACT = frozenset({
    '911', '999', '112', '988', '119', '000', '110', '118', '120', '122',
    '211', '311', '411', '511', '611', '711', '811',
    '1911', '1988', '0911', '9911',
})

# Premium-rate & high-fraud prefixes blocked in E.164 form.
_BLOCKED_E164_PREFIXES = (
    '+1900',
    '+1976',
    '+1911',
    '+1988',
    '+449',
    '+44871',
    '+44872',
    '+44873',
    '+4470',
    '+61190',
    '+49900',
    '+3389',
    '+881',
    '+882',
    '+883',
    '+870',
)

_ACTIVE_CALL_STATUSES = ('dispatching', 'ringing', 'in_progress')


class PhoneError(Exception):
    """Base phone subsystem error with a stable machine-readable code."""

    def __init__(self, code: str, message: str = ''):
        self.code = code
        self.detail = message or code
        super().__init__(self.detail)


class PhoneSafetyError(PhoneError):
    """Raised when a deterministic phone safety or approval gate blocks an action."""


class PhoneConfigError(PhoneError):
    """Raised when LiveKit/SIP configuration or credentials are missing or invalid."""


class PhoneDispatchError(PhoneError):
    """Raised when LiveKit Twirp HTTP dispatch fails."""


def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('timezone-aware datetime required')
    return now.astimezone(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def get_phone_secret(name: str, *, env_override: dict[str, str] | None = None) -> str:
    """Read a declared phone credential via Hermes secret_scope (Rule 11 compliant)."""
    if name not in PHONE_ENV_VARS:
        raise ValueError(f'undeclared phone env var: {name}')
    if env_override is not None and name in env_override:
        return (env_override.get(name) or '').strip()
    try:
        from agent.secret_scope import get_secret
        val = get_secret(name, default='')
        return (val or '').strip()
    except ImportError:
        return (os.environ.get(name) or '').strip()


def resolve_livekit_credentials(
    *, env_override: dict[str, str] | None = None, require_all: bool = True
) -> dict[str, str]:
    """Resolve LiveKit URL, API key/secret, and outbound SIP trunk ID."""
    creds = {key: get_phone_secret(key, env_override=env_override) for key in PHONE_ENV_VARS}
    missing = [k for k in _REQUIRED_DISPATCH_ENV if not creds.get(k)]
    if require_all and missing:
        raise PhoneConfigError(
            'missing_livekit_credentials',
            f"Missing required LiveKit/SIP credentials: {', '.join(missing)}",
        )
    return creds


def check_number_allowed(raw: str, *, default_country: str = '+1') -> str:
    """Normalize a phone number to E.164 and enforce emergency/premium hard-blocklists."""
    if not isinstance(raw, str) or not raw.strip():
        raise PhoneSafetyError('invalid_phone_number', 'Phone number is required')
    cleaned = _STRIP_CHARS_RE.sub('', raw.strip())
    if not cleaned or any(ch.isalpha() for ch in cleaned):
        raise PhoneSafetyError('invalid_phone_number', 'Phone number must contain digits only')
    digits = cleaned[1:] if cleaned.startswith('+') else cleaned
    if not digits.isdigit():
        raise PhoneSafetyError('invalid_phone_number', 'Invalid characters in phone number')

    # Check short/emergency numbers before any country-code expansion.
    if digits in _EMERGENCY_EXACT or len(digits) < 7:
        raise PhoneSafetyError(
            'blocked_emergency_or_short_code',
            'Emergency and short-code numbers are hard-blocked',
        )
    if len(digits) == 10 and digits[:3] in ('911', '988', '900', '976'):
        raise PhoneSafetyError(
            'blocked_number_prefix',
            'Emergency and premium-rate prefixes are hard-blocked',
        )
    if len(digits) == 11 and digits.startswith('1') and digits[1:4] in ('911', '988', '900', '976'):
        raise PhoneSafetyError(
            'blocked_number_prefix',
            'Emergency and premium-rate prefixes are hard-blocked',
        )

    if cleaned.startswith('+'):
        e164 = cleaned
    elif len(digits) == 10 and default_country == '+1':
        e164 = '+1' + digits
    elif len(digits) == 11 and digits.startswith('1') and default_country == '+1':
        e164 = '+' + digits
    else:
        raise PhoneSafetyError(
            'invalid_e164_phone_number',
            'Phone number must be in E.164 format (e.g. +14155551234)',
        )

    if not _E164_STRICT_RE.fullmatch(e164):
        raise PhoneSafetyError('invalid_e164_phone_number', 'Invalid E.164 phone number')

    for prefix in _BLOCKED_E164_PREFIXES:
        if e164.startswith(prefix):
            raise PhoneSafetyError(
                'blocked_number_prefix',
                f'Destination prefix {prefix}* is hard-blocked for safety',
            )
    # NANP N11 service numbers (+1211 .. +1911) in area-code or exchange position.
    if e164.startswith('+1') and len(e164) == 12:
        npa = e164[2:5]
        if npa in ('911', '988', '900', '976', '555') and e164[5:9] == '0199':
            pass
        if npa in ('911', '988', '900', '976'):
            raise PhoneSafetyError('blocked_number_prefix', 'Blocked NANP prefix')
    return e164


def mask_phone(phone: str | None) -> str:
    """Mask middle digits for UI, logs, and LLM summaries (e.g. +14155551234 -> +1******1234)."""
    if not phone or not isinstance(phone, str):
        return ''
    cleaned = _STRIP_CHARS_RE.sub('', phone.strip())
    if cleaned.startswith('+') and len(cleaned) >= 7:
        stars = '*' * max(2, len(cleaned) - 6)
        return f'{cleaned[:2]}{stars}{cleaned[-4:]}'
    if len(cleaned) >= 6:
        return ('*' * (len(cleaned) - 4)) + cleaned[-4:]
    return '***'


def build_ai_disclosure_opening(
    opening: str | None = None, *, caller_name: str = 'Butler'
) -> str:
    """Ensure outbound opening explicitly discloses AI assistant identity in sentence one."""
    safe_name = (caller_name or 'Butler').strip()[:64] or 'Butler'
    if opening is None or not str(opening).strip():
        return (
            f"Hello, this is {safe_name}, an automated AI assistant calling on behalf of my owner."
        )
    text = ' '.join(str(opening).strip().split())
    if len(text) > 600:
        raise PhoneSafetyError('opening_too_long', 'AI disclosure opening must be <= 600 chars')
    first_sentence = re.split(r'(?<=[.!?])\s+', text, maxsplit=1)[0]
    if not _AI_DISCLOSURE_RE.search(first_sentence):
        raise PhoneSafetyError(
            'ai_disclosure_required',
            'First sentence of ai_disclosure_opening must explicitly state AI / automated assistant identity',
        )
    return text


def validate_allowed_dtmf(allowed_dtmf: str | None) -> str:
    if allowed_dtmf is None:
        return ''
    if not isinstance(allowed_dtmf, str) or not _ALLOWED_DTMF_RE.fullmatch(allowed_dtmf.strip()):
        raise PhoneSafetyError(
            'invalid_allowed_dtmf',
            'allowed_dtmf may only contain 0-9, *, #, w (max 32 chars)',
        )
    return allowed_dtmf.strip()


def canonical_plan_payload(plan: dict) -> str:
    """Deterministic JSON representation covered by the outbound SHA-256 approval hash."""
    canonical = {
        'to_e164': str(plan['to_e164']),
        'objective': str(plan['objective']).strip(),
        'ai_disclosure_opening': str(plan['ai_disclosure_opening']).strip(),
        'context_notes': str(plan.get('context_notes') or '').strip(),
        'allowed_dtmf': str(plan.get('allowed_dtmf') or ''),
        'voicemail_policy': str(plan.get('voicemail_policy') or 'hangup'),
        'voicemail_message': str(plan.get('voicemail_message') or '').strip(),
    }
    return json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def compute_plan_sha256(plan: dict) -> str:
    return hashlib.sha256(canonical_plan_payload(plan).encode('utf-8')).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def mint_livekit_jwt(
    api_key: str,
    api_secret: str,
    *,
    sub: str = 'butler-dispatcher',
    ttl_seconds: int = 300,
    video_grants: dict | None = None,
    sip_grants: dict | None = None,
    now_ts: int | None = None,
) -> str:
    """Mint an HS256 LiveKit access token using Python stdlib only."""
    if not api_key or not api_secret:
        raise PhoneConfigError('missing_livekit_credentials', 'LIVEKIT_API_KEY and LIVEKIT_API_SECRET required')
    iat = int(time.time()) if now_ts is None else int(now_ts)
    header = {'alg': 'HS256', 'typ': 'JWT'}
    claims: dict = {
        'iss': api_key,
        'sub': sub,
        'iat': iat,
        'nbf': iat - 5,
        'exp': iat + max(30, int(ttl_seconds)),
    }
    if video_grants is not None:
        claims['video'] = video_grants
    if sip_grants is not None:
        claims['sip'] = sip_grants
    h_b64 = _b64url(json.dumps(header, separators=(',', ':'), sort_keys=True).encode('utf-8'))
    p_b64 = _b64url(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode('utf-8'))
    signing_input = f'{h_b64}.{p_b64}'.encode('ascii')
    sig = hmac.new(api_secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
    return f'{h_b64}.{p_b64}.{_b64url(sig)}'


def livekit_http_base(livekit_url: str) -> str:
    """Convert wss:///ws:// LiveKit URL to https:///http:// Twirp base URL."""
    raw = (livekit_url or '').strip()
    if not raw:
        raise PhoneConfigError('missing_livekit_url', 'LIVEKIT_URL is not configured')
    parsed = urllib.parse.urlparse(raw)
    scheme_map = {'wss': 'https', 'ws': 'http', 'https': 'https', 'http': 'http'}
    http_scheme = scheme_map.get(parsed.scheme.lower())
    if not http_scheme or not parsed.netloc:
        raise PhoneConfigError('invalid_livekit_url', 'LIVEKIT_URL must use wss://, ws://, https://, or http://')
    path = parsed.path.rstrip('/')
    return urllib.parse.urlunparse((http_scheme, parsed.netloc, path, '', '', ''))


def twirp_post(
    livekit_url: str,
    api_key: str,
    api_secret: str,
    service: str,
    method: str,
    payload: dict,
    *,
    room_name: str = '',
    timeout: float = 15.0,
    opener=None,
) -> dict:
    """Invoke a LiveKit Twirp JSON endpoint over HTTP(S) using Python stdlib."""
    base = livekit_http_base(livekit_url)
    endpoint = f'{base}/twirp/livekit.{service}/{method}'
    token = mint_livekit_jwt(
        api_key,
        api_secret,
        video_grants={
            'roomCreate': True,
            'roomAdmin': True,
            'roomList': True,
            **({'room': room_name} if room_name else {}),
        },
        sip_grants={'call': True, 'admin': True},
    )
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        endpoint,
        data=body,
        method='POST',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'Butler-Hermes-Plugin/0.4',
        },
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            raw_resp = resp.read().decode('utf-8', errors='replace')
            return json.loads(raw_resp) if raw_resp.strip() else {}
    except urllib.error.HTTPError as exc:
        err_body = ''
        try:
            err_body = exc.read().decode('utf-8', errors='replace')[:400]
        except Exception:
            err_body = ''
        raise PhoneDispatchError(
            'livekit_twirp_http_error',
            f'LiveKit {service}/{method} failed with HTTP {exc.code}: {err_body or exc.reason}',
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise PhoneDispatchError(
            'livekit_twirp_network_error',
            f'LiveKit {service}/{method} connection failed: {exc}',
        ) from exc


def create_agent_dispatch(
    *,
    livekit_url: str,
    api_key: str,
    api_secret: str,
    agent_name: str,
    room_name: str,
    metadata: dict,
    timeout: float = 15.0,
    opener=None,
) -> dict:
    """Dispatch `butler.voice_worker` into a new LiveKit room via Twirp CreateDispatch."""
    return twirp_post(
        livekit_url,
        api_key,
        api_secret,
        'AgentDispatchService',
        'CreateDispatch',
        {
            'agent_name': agent_name,
            'room': room_name,
            'metadata': json.dumps(metadata, ensure_ascii=False),
        },
        room_name=room_name,
        timeout=timeout,
        opener=opener,
    )


def create_sip_participant(
    *,
    livekit_url: str,
    api_key: str,
    api_secret: str,
    sip_trunk_id: str,
    call_to: str,
    room_name: str,
    participant_identity: str,
    participant_name: str = 'Butler',
    from_number: str = '',
    wait_until_answered: bool = True,
    ringing_timeout_seconds: int = 35,
    max_call_duration_seconds: int = 900,
    timeout: float = 45.0,
    opener=None,
) -> dict:
    """Dial a PSTN carrier number into a LiveKit room via SIP/CreateSIPParticipant."""
    e164 = check_number_allowed(call_to)
    payload: dict = {
        'sip_trunk_id': sip_trunk_id,
        'sip_call_to': e164,
        'room_name': room_name,
        'participant_identity': participant_identity,
        'participant_name': participant_name,
        'wait_until_answered': bool(wait_until_answered),
        'ringing_timeout': f'{max(5, int(ringing_timeout_seconds))}s',
        'max_call_duration': f'{max(30, int(max_call_duration_seconds))}s',
        'krisp_enabled': True,
    }
    if from_number:
        payload['sip_number'] = check_number_allowed(from_number)
    return twirp_post(
        livekit_url,
        api_key,
        api_secret,
        'SIP',
        'CreateSIPParticipant',
        payload,
        room_name=room_name,
        timeout=timeout,
        opener=opener,
    )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS phone_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS phone_plans (
    plan_id TEXT PRIMARY KEY,
    to_e164 TEXT NOT NULL,
    masked_number TEXT NOT NULL,
    recipient_label TEXT NOT NULL,
    objective TEXT NOT NULL,
    ai_disclosure_opening TEXT NOT NULL,
    context_notes TEXT NOT NULL DEFAULT '',
    allowed_dtmf TEXT NOT NULL DEFAULT '',
    voicemail_policy TEXT NOT NULL DEFAULT 'hangup',
    voicemail_message TEXT NOT NULL DEFAULT '',
    script_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    approval_expires_at TEXT,
    rejected_at TEXT,
    rejected_reason TEXT,
    last_call_id TEXT
);

CREATE TABLE IF NOT EXISTS phone_calls (
    call_id TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    plan_id TEXT,
    to_e164 TEXT NOT NULL,
    masked_number TEXT NOT NULL,
    recipient_label TEXT NOT NULL DEFAULT '',
    room_name TEXT NOT NULL,
    dispatch_id TEXT NOT NULL DEFAULT '',
    script_sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    outcome_summary TEXT NOT NULL DEFAULT '',
    transcript_json TEXT NOT NULL DEFAULT '[]',
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS owner_rings (
    ring_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    masked_number TEXT NOT NULL,
    urgent INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_phone_plans_status ON phone_plans(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_phone_plans_to ON phone_plans(to_e164, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_phone_calls_status ON phone_calls(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_owner_rings_created ON owner_rings(created_at DESC);
"""


class PhoneStore:
    """Profile-scoped SQLite ledger and deterministic safety gatekeeper for Butler calls."""

    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory is not None else butler_data_dir(create=False)
        self.db_path = self.directory / 'phone.sqlite3'

    def _connect_read(self) -> sqlite3.Connection | None:
        if not self.db_path.is_file():
            return None
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_write(self) -> sqlite3.Connection:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=WAL')
        except sqlite3.Error:
            pass
        conn.executescript(_SCHEMA_SQL)
        return conn

    @staticmethod
    def _public_plan(row: sqlite3.Row | dict, *, include_e164: bool = False) -> dict:
        d = dict(row)
        out = {
            'plan_id': d['plan_id'],
            'masked_number': d['masked_number'],
            'recipient_label': d['recipient_label'],
            'objective': d['objective'],
            'ai_disclosure_opening': d['ai_disclosure_opening'],
            'context_notes': d['context_notes'],
            'allowed_dtmf': d['allowed_dtmf'],
            'voicemail_policy': d['voicemail_policy'],
            'voicemail_message': d['voicemail_message'],
            'script_sha256': d['script_sha256'],
            'status': d['status'],
            'created_at': d['created_at'],
            'expires_at': d['expires_at'],
            'approved_at': d['approved_at'],
            'approved_by': d['approved_by'],
            'approval_expires_at': d['approval_expires_at'],
            'rejected_at': d['rejected_at'],
            'rejected_reason': d['rejected_reason'],
            'last_call_id': d['last_call_id'],
        }
        if include_e164:
            out['to_e164'] = d['to_e164']
        return out

    @staticmethod
    def _public_call(row: sqlite3.Row | dict, *, include_transcript: bool = True) -> dict:
        d = dict(row)
        transcript = []
        if include_transcript and d.get('transcript_json'):
            try:
                parsed = json.loads(d['transcript_json'])
                if isinstance(parsed, list):
                    transcript = parsed
            except (ValueError, TypeError):
                transcript = []
        out = {
            'call_id': d['call_id'],
            'direction': d['direction'],
            'plan_id': d['plan_id'],
            'masked_number': d['masked_number'],
            'recipient_label': d['recipient_label'],
            'room_name': d['room_name'],
            'status': d['status'],
            'reason': d['reason'],
            'outcome_summary': d['outcome_summary'],
            'duration_seconds': int(d.get('duration_seconds') or 0),
            'created_at': d['created_at'],
            'started_at': d['started_at'],
            'ended_at': d['ended_at'],
        }
        if include_transcript:
            out['transcript'] = transcript
        return out

    def phone_stopped_info(self) -> dict:
        conn = self._connect_read()
        if conn is None:
            return {'stopped': False, 'reason': '', 'updated_at': None}
        with closing(conn):
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phone_state'"
            ).fetchone()
            if not has_table:
                return {'stopped': False, 'reason': '', 'updated_at': None}
            row = conn.execute(
                "SELECT value, updated_at FROM phone_state WHERE key='phone_stopped'"
            ).fetchone()
            if not row:
                return {'stopped': False, 'reason': '', 'updated_at': None}
            try:
                payload = json.loads(row['value'])
            except (ValueError, TypeError):
                payload = {'stopped': False, 'reason': ''}
            return {
                'stopped': bool(payload.get('stopped')),
                'reason': str(payload.get('reason') or ''),
                'updated_at': row['updated_at'],
            }

    def is_phone_stopped(self) -> bool:
        return bool(self.phone_stopped_info()['stopped'])

    def set_phone_stopped(
        self, stopped: bool, *, reason: str = '', now: datetime | None = None
    ) -> dict:
        """Engage or clear the master `phone_stopped` kill-switch."""
        ts = _iso(_utc_now(now))
        clean_reason = (reason or '').strip()[:300]
        payload = json.dumps({'stopped': bool(stopped), 'reason': clean_reason if stopped else ''})
        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                """
                INSERT INTO phone_state(key, value, updated_at)
                VALUES ('phone_stopped', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (payload, ts),
            )
            if stopped:
                # Cancel any in-flight or pending dispatches when kill-switch trips.
                conn.execute(
                    """
                    UPDATE phone_calls
                    SET status='cancelled', ended_at=COALESCE(ended_at, ?),
                        outcome_summary=CASE WHEN outcome_summary='' THEN 'Cancelled by phone kill-switch' ELSE outcome_summary END
                    WHERE status IN ('dispatching', 'ringing', 'in_progress')
                    """,
                    (ts,),
                )
            conn.execute('COMMIT')
        return {'stopped': bool(stopped), 'reason': clean_reason if stopped else '', 'updated_at': ts}

    def _expire_stale_locked(self, conn: sqlite3.Connection, now_dt: datetime) -> None:
        now_iso = _iso(now_dt)
        stale_cutoff = _iso(now_dt - timedelta(seconds=ACTIVE_CALL_STALE_SECONDS))
        conn.execute(
            """
            UPDATE phone_calls
            SET status='timed_out', ended_at=?
            WHERE status IN ('dispatching', 'ringing', 'in_progress')
              AND created_at < ?
            """,
            (now_iso, stale_cutoff),
        )
        conn.execute(
            """
            UPDATE phone_plans
            SET status='expired'
            WHERE status='pending_approval' AND expires_at <= ?
            """,
            (now_iso,),
        )
        conn.execute(
            """
            UPDATE phone_plans
            SET status='expired'
            WHERE status='approved' AND approval_expires_at IS NOT NULL AND approval_expires_at <= ?
            """,
            (now_iso,),
        )

    def draft_plan(
        self,
        *,
        to_number: str,
        recipient_label: str,
        objective: str,
        ai_disclosure_opening: str | None = None,
        context_notes: str = '',
        allowed_dtmf: str = '',
        voicemail_policy: str = 'hangup',
        voicemail_message: str = '',
        now: datetime | None = None,
    ) -> dict:
        """Create a non-blocking `pending_approval` call plan with canonical SHA-256 digest."""
        now_dt = _utc_now(now)
        if self.is_phone_stopped():
            raise PhoneSafetyError(
                'phone_stopped_kill_switch',
                'Phone calling is halted by the kill-switch; clear phone_stopped in Control Center first',
            )
        e164 = check_number_allowed(to_number)
        label = ' '.join(str(recipient_label or '').strip().split())[:120]
        if not label:
            raise PhoneSafetyError('recipient_label_required', 'recipient_label is required')
        obj = str(objective or '').strip()
        if not obj or len(obj) > 2000:
            raise PhoneSafetyError('invalid_objective', 'objective must be 1-2000 chars')
        notes = str(context_notes or '').strip()
        if len(notes) > 2000:
            raise PhoneSafetyError('context_notes_too_long', 'context_notes must be <= 2000 chars')
        vm_policy = (voicemail_policy or 'hangup').strip()
        if vm_policy not in ('hangup', 'leave_message'):
            raise PhoneSafetyError('invalid_voicemail_policy', 'voicemail_policy must be hangup or leave_message')
        vm_msg = str(voicemail_message or '').strip()
        if len(vm_msg) > 800:
            raise PhoneSafetyError('voicemail_message_too_long', 'voicemail_message must be <= 800 chars')
        if vm_policy == 'leave_message' and not vm_msg:
            raise PhoneSafetyError('voicemail_message_required', 'voicemail_message required when voicemail_policy=leave_message')

        try:
            caller_name = str(load_settings(self.directory).get('callerName') or 'Butler')
        except Exception:
            caller_name = 'Butler'
        opening = build_ai_disclosure_opening(ai_disclosure_opening, caller_name=caller_name)
        dtmf = validate_allowed_dtmf(allowed_dtmf)

        plan_fields = {
            'to_e164': e164,
            'objective': obj,
            'ai_disclosure_opening': opening,
            'context_notes': notes,
            'allowed_dtmf': dtmf,
            'voicemail_policy': vm_policy,
            'voicemail_message': vm_msg,
        }
        sha256 = compute_plan_sha256(plan_fields)
        plan_id = f'plan_{secrets.token_hex(6)}'
        masked = mask_phone(e164)
        created_iso = _iso(now_dt)
        expires_iso = _iso(now_dt + timedelta(seconds=PLAN_TTL_SECONDS))

        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._expire_stale_locked(conn, now_dt)
            conn.execute(
                """
                INSERT INTO phone_plans(
                    plan_id, to_e164, masked_number, recipient_label, objective,
                    ai_disclosure_opening, context_notes, allowed_dtmf,
                    voicemail_policy, voicemail_message, script_sha256,
                    status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?)
                """,
                (
                    plan_id, e164, masked, label, obj,
                    opening, notes, dtmf,
                    vm_policy, vm_msg, sha256,
                    created_iso, expires_iso,
                ),
            )
            row = conn.execute('SELECT * FROM phone_plans WHERE plan_id=?', (plan_id,)).fetchone()
            conn.execute('COMMIT')
        return self._public_plan(row)

    def approve_plan(
        self,
        plan_id: str,
        *,
        approved_by: str = 'human_ui',
        expected_sha256: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Human-only approval entrypoint (Desktop Control Center or CLI; never an LLM tool)."""
        now_dt = _utc_now(now)
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise PhoneSafetyError('invalid_plan_id', 'plan_id is required')
        if self.is_phone_stopped():
            raise PhoneSafetyError('phone_stopped_kill_switch', 'Cannot approve while phone_stopped is active')
        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._expire_stale_locked(conn, now_dt)
            row = conn.execute('SELECT * FROM phone_plans WHERE plan_id=?', (plan_id.strip(),)).fetchone()
            if not row:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('plan_not_found', f'Call plan {plan_id} not found')
            plan = dict(row)
            if plan['status'] != 'pending_approval':
                conn.execute('ROLLBACK')
                raise PhoneSafetyError(
                    'invalid_plan_status',
                    f"Plan {plan_id} cannot be approved from status '{plan['status']}'",
                )
            check_number_allowed(plan['to_e164'])
            build_ai_disclosure_opening(plan['ai_disclosure_opening'])
            actual_sha = compute_plan_sha256(plan)
            if actual_sha != plan['script_sha256']:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('script_sha256_mismatch', 'Plan contents do not match stored SHA-256 digest')
            if expected_sha256 and expected_sha256.strip().lower() != actual_sha:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('script_sha256_mismatch', 'Provided expected_sha256 does not match plan')
            approved_iso = _iso(now_dt)
            approval_exp_iso = _iso(now_dt + timedelta(seconds=PLAN_TTL_SECONDS))
            actor = (approved_by or 'human').strip()[:64]
            conn.execute(
                """
                UPDATE phone_plans
                SET status='approved', approved_at=?, approved_by=?, approval_expires_at=?
                WHERE plan_id=?
                """,
                (approved_iso, actor, approval_exp_iso, plan['plan_id']),
            )
            updated = conn.execute('SELECT * FROM phone_plans WHERE plan_id=?', (plan['plan_id'],)).fetchone()
            conn.execute('COMMIT')
        return self._public_plan(updated)

    def reject_plan(
        self,
        plan_id: str,
        *,
        reason: str = '',
        now: datetime | None = None,
    ) -> dict:
        """Reject or cancel an unplaced call plan."""
        now_dt = _utc_now(now)
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise PhoneSafetyError('invalid_plan_id', 'plan_id is required')
        clean_reason = (reason or '').strip()[:300]
        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._expire_stale_locked(conn, now_dt)
            row = conn.execute('SELECT * FROM phone_plans WHERE plan_id=?', (plan_id.strip(),)).fetchone()
            if not row:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('plan_not_found', f'Call plan {plan_id} not found')
            if row['status'] not in ('pending_approval', 'approved'):
                conn.execute('ROLLBACK')
                raise PhoneSafetyError(
                    'invalid_plan_status',
                    f"Plan {plan_id} cannot be rejected from status '{row['status']}'",
                )
            conn.execute(
                """
                UPDATE phone_plans
                SET status='rejected', rejected_at=?, rejected_reason=?
                WHERE plan_id=?
                """,
                (_iso(now_dt), clean_reason, row['plan_id']),
            )
            updated = conn.execute('SELECT * FROM phone_plans WHERE plan_id=?', (row['plan_id'],)).fetchone()
            conn.execute('COMMIT')
        return self._public_plan(updated)

    def place_call(
        self,
        plan_id: str,
        *,
        now: datetime | None = None,
        env_override: dict[str, str] | None = None,
        dispatch_fn=None,
    ) -> dict:
        """Gate 1: Verify human approval, 24h TTL, SHA-256, blocklist, and active lock, then dispatch."""
        now_dt = _utc_now(now)
        settings = load_settings(self.directory)
        if not settings.get('phoneEnabled'):
            raise PhoneSafetyError('phone_disabled', 'Phone calling is disabled in Butler Preferences')
        if settings.get('paused'):
            raise PhoneSafetyError('butler_paused', 'Butler is currently paused')
        if self.is_phone_stopped():
            raise PhoneSafetyError('phone_stopped_kill_switch', 'Phone calling is halted by phone_stopped kill-switch')
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise PhoneSafetyError('invalid_plan_id', 'plan_id is required')

        creds = resolve_livekit_credentials(env_override=env_override, require_all=True)
        agent_name = str(settings.get('livekitAgentName') or 'butler-voice')
        caller_name = str(settings.get('callerName') or 'Butler')

        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._expire_stale_locked(conn, now_dt)
            active = conn.execute(
                "SELECT call_id FROM phone_calls WHERE status IN ('dispatching', 'ringing', 'in_progress') LIMIT 1"
            ).fetchone()
            if active:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError(
                    'call_already_active',
                    f"Another phone call ({active['call_id']}) is currently active",
                )

            row = conn.execute('SELECT * FROM phone_plans WHERE plan_id=?', (plan_id.strip(),)).fetchone()
            if not row:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('plan_not_found', f'Call plan {plan_id} not found')
            plan = dict(row)
            if plan['status'] == 'expired':
                conn.execute('COMMIT')
                if plan.get('approved_at'):
                    raise PhoneSafetyError(
                        'approval_expired',
                        'Human approval expired (approvals are valid for 24 hours); draft a fresh call plan',
                    )
                raise PhoneSafetyError('plan_expired', 'Call plan expired before approval; draft a fresh call plan')
            if plan['status'] != 'approved':
                conn.execute('ROLLBACK')
                raise PhoneSafetyError(
                    'approval_required',
                    f"Outbound call requires human approval first (current status: '{plan['status']}')",
                )
            approved_at = _parse_iso(plan['approved_at'])
            if approved_at is None or (now_dt - approved_at).total_seconds() > PLAN_TTL_SECONDS:
                conn.execute("UPDATE phone_plans SET status='expired' WHERE plan_id=?", (plan['plan_id'],))
                conn.execute('COMMIT')
                raise PhoneSafetyError('approval_expired', 'Plan approval exceeded 24h TTL; draft a fresh plan')

            e164 = check_number_allowed(plan['to_e164'])
            build_ai_disclosure_opening(plan['ai_disclosure_opening'], caller_name=caller_name)
            actual_sha = compute_plan_sha256(plan)
            if actual_sha != plan['script_sha256']:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('script_sha256_mismatch', 'Plan SHA-256 verification failed before dispatch')

            call_id = f'call_{secrets.token_hex(6)}'
            room_name = f'butler-out-{call_id}'
            now_iso = _iso(now_dt)
            conn.execute(
                """
                INSERT INTO phone_calls(
                    call_id, direction, plan_id, to_e164, masked_number, recipient_label,
                    room_name, script_sha256, status, reason, created_at
                ) VALUES (?, 'outbound', ?, ?, ?, ?, ?, ?, 'dispatching', ?, ?)
                """,
                (
                    call_id, plan['plan_id'], e164, plan['masked_number'],
                    plan['recipient_label'], room_name, actual_sha,
                    plan['objective'][:240], now_iso,
                ),
            )
            conn.execute(
                "UPDATE phone_plans SET status='dispatching', last_call_id=? WHERE plan_id=?",
                (call_id, plan['plan_id']),
            )
            conn.execute('COMMIT')

        room_meta = {
            'direction': 'outbound',
            'call_id': call_id,
            'plan_id': plan['plan_id'],
            'script_sha256': actual_sha,
            'butler_data_dir': str(self.directory),
            'masked_number': plan['masked_number'],
            'recipient_label': plan['recipient_label'],
            'caller_name': caller_name,
        }
        dispatcher = dispatch_fn or create_agent_dispatch
        try:
            resp = dispatcher(
                livekit_url=creds['LIVEKIT_URL'],
                api_key=creds['LIVEKIT_API_KEY'],
                api_secret=creds['LIVEKIT_API_SECRET'],
                agent_name=agent_name,
                room_name=room_name,
                metadata=room_meta,
            )
        except Exception as exc:
            err_ts = _iso(_utc_now())
            with closing(self._connect_write()) as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute(
                    "UPDATE phone_calls SET status='failed', ended_at=?, outcome_summary=? WHERE call_id=?",
                    (err_ts, f'Dispatch failed: {exc}'[:300], call_id),
                )
                conn.execute(
                    "UPDATE phone_plans SET status='approved' WHERE plan_id=? AND status='dispatching'",
                    (plan['plan_id'],),
                )
                conn.execute('COMMIT')
            if isinstance(exc, PhoneError):
                raise
            raise PhoneDispatchError('dispatch_failed', str(exc)) from exc

        dispatch_id = str((resp or {}).get('id') or (resp or {}).get('dispatch_id') or '')
        with closing(self._connect_write()) as conn:
            conn.execute(
                "UPDATE phone_calls SET dispatch_id=? WHERE call_id=?",
                (dispatch_id, call_id),
            )
            call_row = conn.execute('SELECT * FROM phone_calls WHERE call_id=?', (call_id,)).fetchone()
        return self._public_call(call_row)

    def verify_outbound_ready_for_sip(
        self,
        *,
        plan_id: str,
        call_id: str,
        expected_sha256: str,
        now: datetime | None = None,
    ) -> dict:
        """Gate 2: Worker-side re-verification right before SIP/CreateSIPParticipant."""
        now_dt = _utc_now(now)
        if self.is_phone_stopped():
            raise PhoneSafetyError('phone_stopped_kill_switch', 'Aborted before SIP dial: phone_stopped is active')
        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            plan_row = conn.execute('SELECT * FROM phone_plans WHERE plan_id=?', (plan_id,)).fetchone()
            call_row = conn.execute('SELECT * FROM phone_calls WHERE call_id=?', (call_id,)).fetchone()
            if not plan_row or not call_row:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('plan_or_call_missing', 'Call or plan record missing in worker verification')
            plan = dict(plan_row)
            call = dict(call_row)
            if call['status'] in ('cancelled', 'failed', 'completed', 'timed_out'):
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('call_not_dispatchable', f"Call {call_id} is '{call['status']}'")
            if plan['status'] not in ('approved', 'dispatching'):
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('plan_not_approved', f"Plan {plan_id} status is '{plan['status']}'")
            approved_at = _parse_iso(plan['approved_at'])
            if approved_at is None or (now_dt - approved_at).total_seconds() > PLAN_TTL_SECONDS:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('approval_expired', 'Plan approval expired before SIP dial')
            e164 = check_number_allowed(plan['to_e164'])
            build_ai_disclosure_opening(plan['ai_disclosure_opening'])
            actual_sha = compute_plan_sha256(plan)
            if actual_sha != plan['script_sha256'] or actual_sha != (expected_sha256 or '').strip().lower():
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('script_sha256_mismatch', 'Worker SHA-256 verification failed before SIP dial')
            now_iso = _iso(now_dt)
            conn.execute(
                "UPDATE phone_calls SET status='ringing', started_at=COALESCE(started_at, ?) WHERE call_id=?",
                (now_iso, call_id),
            )
            conn.execute(
                "UPDATE phone_plans SET status='calling' WHERE plan_id=?",
                (plan_id,),
            )
            conn.execute('COMMIT')
        return self._public_plan(plan, include_e164=True)

    def ring_owner(
        self,
        *,
        reason: str,
        context_brief: str = '',
        urgent: bool = False,
        fallback_timezone: str = '',
        now: datetime | None = None,
        env_override: dict[str, str] | None = None,
        dispatch_fn=None,
    ) -> dict:
        """Ring the owner's carrier phone via LiveKit SIP after enforcing all escalation gates."""
        now_dt = _utc_now(now)
        clean_reason = ' '.join(str(reason or '').strip().split())[:400]
        if not clean_reason:
            raise PhoneSafetyError('reason_required', 'A concise opening reason is required to ring the owner')

        settings = load_settings(self.directory)
        if not settings.get('phoneEnabled'):
            raise PhoneSafetyError('phone_disabled', 'Phone calling is disabled in Butler Preferences')
        if not settings.get('ownerRingEnabled'):
            raise PhoneSafetyError('owner_ring_disabled', 'Owner escalation rings are disabled in Butler Preferences')
        if self.is_phone_stopped():
            raise PhoneSafetyError(
                'phone_stopped_kill_switch',
                'Owner rings are halted by the phone_stopped latch',
            )
        owner_phone_raw = str(settings.get('ownerPhone') or '')
        if not owner_phone_raw:
            raise PhoneSafetyError('owner_phone_missing', 'ownerPhone is not configured in Butler Preferences')
        owner_e164 = check_number_allowed(owner_phone_raw)
        masked = mask_phone(owner_e164)

        pol = Policy(self.directory).evaluate_owner_ring(
            now_dt, urgent=bool(urgent), fallback_timezone=fallback_timezone
        )
        if not pol['allowed']:
            raise PhoneSafetyError(pol['reason'], f"Owner ring blocked by policy: {pol['reason']}")

        creds = resolve_livekit_credentials(env_override=env_override, require_all=True)
        max_per_hour = int(settings.get('maxRingsPerHour') or 3)
        min_gap_sec = int(settings.get('minRingGapSeconds') or 120)
        agent_name = str(settings.get('livekitAgentName') or 'butler-voice')
        caller_name = str(settings.get('callerName') or 'Butler')

        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._expire_stale_locked(conn, now_dt)
            active = conn.execute(
                "SELECT call_id FROM phone_calls WHERE status IN ('dispatching', 'ringing', 'in_progress') LIMIT 1"
            ).fetchone()
            if active:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError(
                    'call_already_active',
                    f"Another phone call ({active['call_id']}) is currently active",
                )

            hour_ago_iso = _iso(now_dt - timedelta(hours=1))
            hourly_count = conn.execute(
                """
                SELECT COUNT(*) FROM owner_rings
                WHERE created_at >= ? AND status != 'dispatch_failed'
                """,
                (hour_ago_iso,),
            ).fetchone()[0]
            if int(hourly_count) >= max_per_hour:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError(
                    'hourly_ring_limit_reached',
                    f'Owner ring limit reached ({max_per_hour}/hour)',
                )

            last_ring = conn.execute(
                """
                SELECT created_at FROM owner_rings
                WHERE status != 'dispatch_failed'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            if last_ring:
                last_dt = _parse_iso(last_ring['created_at'])
                if last_dt is not None:
                    elapsed = (now_dt - last_dt).total_seconds()
                    if elapsed < min_gap_sec:
                        conn.execute('ROLLBACK')
                        wait_s = int(min_gap_sec - elapsed)
                        raise PhoneSafetyError(
                            'ring_cooldown_active',
                            f'Minimum ring gap ({min_gap_sec}s) active; {wait_s}s remaining',
                        )

            call_id = f'call_{secrets.token_hex(6)}'
            ring_id = f'ring_{secrets.token_hex(6)}'
            room_name = f'butler-owner-{call_id}'
            now_iso = _iso(now_dt)
            conn.execute(
                """
                INSERT INTO owner_rings(ring_id, call_id, masked_number, urgent, reason, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'dispatching', ?)
                """,
                (ring_id, call_id, masked, 1 if urgent else 0, clean_reason, now_iso),
            )
            conn.execute(
                """
                INSERT INTO phone_calls(
                    call_id, direction, plan_id, to_e164, masked_number, recipient_label,
                    room_name, status, reason, created_at
                ) VALUES (?, 'owner_ring', NULL, ?, ?, 'Owner', ?, 'dispatching', ?, ?)
                """,
                (call_id, owner_e164, masked, room_name, clean_reason, now_iso),
            )
            conn.execute('COMMIT')

        room_meta = {
            'direction': 'owner_ring',
            'call_id': call_id,
            'ring_id': ring_id,
            'butler_data_dir': str(self.directory),
            'opening_reason': clean_reason,
            'context_brief': str(context_brief or '').strip()[:1500],
            'urgent': bool(urgent),
            'caller_name': caller_name,
            'masked_number': masked,
        }
        dispatcher = dispatch_fn or create_agent_dispatch
        try:
            resp = dispatcher(
                livekit_url=creds['LIVEKIT_URL'],
                api_key=creds['LIVEKIT_API_KEY'],
                api_secret=creds['LIVEKIT_API_SECRET'],
                agent_name=agent_name,
                room_name=room_name,
                metadata=room_meta,
            )
        except Exception as exc:
            err_ts = _iso(_utc_now())
            with closing(self._connect_write()) as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute("UPDATE owner_rings SET status='dispatch_failed' WHERE ring_id=?", (ring_id,))
                conn.execute(
                    "UPDATE phone_calls SET status='failed', ended_at=?, outcome_summary=? WHERE call_id=?",
                    (err_ts, f'Dispatch failed: {exc}'[:300], call_id),
                )
                conn.execute('COMMIT')
            if isinstance(exc, PhoneError):
                raise
            raise PhoneDispatchError('dispatch_failed', str(exc)) from exc

        dispatch_id = str((resp or {}).get('id') or (resp or {}).get('dispatch_id') or '')
        with closing(self._connect_write()) as conn:
            conn.execute("UPDATE owner_rings SET status='dispatched' WHERE ring_id=?", (ring_id,))
            conn.execute("UPDATE phone_calls SET dispatch_id=? WHERE call_id=?", (dispatch_id, call_id))
            call_row = conn.execute('SELECT * FROM phone_calls WHERE call_id=?', (call_id,)).fetchone()
        return self._public_call(call_row)

    def get_owner_ring_target(self, call_id: str) -> dict:
        """Worker helper to fetch verified owner E.164 right before SIP dial."""
        if self.is_phone_stopped():
            raise PhoneSafetyError('phone_stopped_kill_switch', 'Owner ring aborted: phone_stopped is active')
        with closing(self._connect_write()) as conn:
            row = conn.execute(
                "SELECT * FROM phone_calls WHERE call_id=? AND direction='owner_ring'",
                (call_id,),
            ).fetchone()
            if not row:
                raise PhoneSafetyError('call_not_found', f'Owner ring {call_id} not found')
            call = dict(row)
            if call['status'] in ('cancelled', 'failed', 'completed', 'timed_out'):
                raise PhoneSafetyError('call_not_dispatchable', f"Call {call_id} is '{call['status']}'")
            e164 = check_number_allowed(call['to_e164'])
            conn.execute(
                "UPDATE phone_calls SET status='ringing', started_at=COALESCE(started_at, ?) WHERE call_id=?",
                (_iso(_utc_now()), call_id),
            )
        return {'call_id': call_id, 'to_e164': e164, 'masked_number': call['masked_number'], 'reason': call['reason']}

    def cancel_call(self, call_id: str, *, reason: str = '', now: datetime | None = None) -> dict:
        now_dt = _utc_now(now)
        if not isinstance(call_id, str) or not call_id.strip():
            raise PhoneSafetyError('invalid_call_id', 'call_id is required')
        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM phone_calls WHERE call_id=?', (call_id.strip(),)).fetchone()
            if not row:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('call_not_found', f'Call {call_id} not found')
            if row['status'] in _ACTIVE_CALL_STATUSES:
                conn.execute(
                    """
                    UPDATE phone_calls
                    SET status='cancelled', ended_at=?, outcome_summary=COALESCE(NULLIF(?, ''), 'Cancelled')
                    WHERE call_id=?
                    """,
                    (_iso(now_dt), (reason or '').strip()[:300], row['call_id']),
                )
                if row['plan_id']:
                    conn.execute(
                        "UPDATE phone_plans SET status='cancelled' WHERE plan_id=? AND status IN ('dispatching', 'calling')",
                        (row['plan_id'],),
                    )
            updated = conn.execute('SELECT * FROM phone_calls WHERE call_id=?', (row['call_id'],)).fetchone()
            conn.execute('COMMIT')
        return self._public_call(updated)

    def record_inbound_call(
        self,
        *,
        caller_number: str,
        room_name: str,
        call_id: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Create an inbound call record and link any matching recent outbound plan."""
        now_dt = _utc_now(now)
        try:
            e164 = check_number_allowed(caller_number)
        except PhoneSafetyError:
            e164 = (caller_number or 'unknown').strip()[:24]
        masked = mask_phone(e164)
        matched = self.open_plans_for(caller_number, now=now_dt)
        linked_plan_id = matched[0]['plan_id'] if matched else None
        linked_label = matched[0]['recipient_label'] if matched else 'Inbound caller'
        cid = call_id or f'call_{secrets.token_hex(6)}'
        now_iso = _iso(now_dt)
        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                """
                INSERT OR REPLACE INTO phone_calls(
                    call_id, direction, plan_id, to_e164, masked_number, recipient_label,
                    room_name, status, reason, created_at, started_at
                ) VALUES (?, 'inbound', ?, ?, ?, ?, ?, 'in_progress', 'Inbound call screening', ?, ?)
                """,
                (cid, linked_plan_id, e164, masked, linked_label, room_name, now_iso, now_iso),
            )
            row = conn.execute('SELECT * FROM phone_calls WHERE call_id=?', (cid,)).fetchone()
            conn.execute('COMMIT')
        res = self._public_call(row)
        res['matched_plans'] = matched
        return res

    def save_call_result(
        self,
        call_id: str,
        *,
        status: str = 'completed',
        outcome_summary: str = '',
        transcript: list[dict] | None = None,
        duration_seconds: int = 0,
        now: datetime | None = None,
    ) -> dict:
        """Persist final call status, sanitized transcript, and summary from `voice_worker`."""
        now_dt = _utc_now(now)
        now_iso = _iso(now_dt)
        clean_status = status if status in (
            'in_progress', 'completed', 'voicemail', 'no_answer', 'busy', 'failed', 'cancelled', 'screened_spam'
        ) else 'completed'
        safe_transcript = []
        for item in (transcript or [])[:200]:
            if isinstance(item, dict):
                safe_transcript.append({
                    'speaker': str(item.get('speaker') or 'agent')[:24],
                    'text': str(item.get('text') or '')[:1000],
                    'ts': str(item.get('ts') or '')[:40],
                })
        summary = str(outcome_summary or '').strip()[:1000]
        with closing(self._connect_write()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM phone_calls WHERE call_id=?', (call_id,)).fetchone()
            if not row:
                conn.execute('ROLLBACK')
                raise PhoneSafetyError('call_not_found', f'Call {call_id} not found')
            ended = now_iso if clean_status != 'in_progress' else None
            conn.execute(
                """
                UPDATE phone_calls
                SET status=?, outcome_summary=?, transcript_json=?, duration_seconds=?,
                    ended_at=COALESCE(?, ended_at)
                WHERE call_id=?
                """,
                (
                    clean_status,
                    summary,
                    json.dumps(safe_transcript, ensure_ascii=False),
                    max(0, int(duration_seconds)),
                    ended,
                    call_id,
                ),
            )
            if row['plan_id'] and clean_status != 'in_progress':
                plan_final = 'completed' if clean_status in ('completed', 'voicemail') else clean_status
                conn.execute(
                    "UPDATE phone_plans SET status=? WHERE plan_id=?",
                    (plan_final, row['plan_id']),
                )
            updated = conn.execute('SELECT * FROM phone_calls WHERE call_id=?', (call_id,)).fetchone()
            conn.execute('COMMIT')
        return self._public_call(updated)

    def open_plans_for(self, caller_number: str, *, now: datetime | None = None) -> list[dict]:
        """Return recent outbound plans matching an inbound callback number."""
        try:
            e164 = check_number_allowed(caller_number)
        except PhoneSafetyError:
            return []
        conn = self._connect_read()
        if conn is None:
            return []
        cutoff = _iso(_utc_now(now) - timedelta(days=CALLBACK_LOOKBACK_DAYS))
        with closing(conn):
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phone_plans'"
            ).fetchone()
            if not has_table:
                return []
            rows = conn.execute(
                """
                SELECT * FROM phone_plans
                WHERE to_e164=? AND created_at >= ?
                  AND status IN ('pending_approval', 'approved', 'dispatching', 'calling', 'completed', 'voicemail', 'no_answer')
                ORDER BY created_at DESC LIMIT 5
                """,
                (e164, cutoff),
            ).fetchall()
            return [self._public_plan(r) for r in rows]

    def get_plan(self, plan_id: str) -> dict | None:
        conn = self._connect_read()
        if conn is None:
            return None
        with closing(conn):
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phone_plans'"
            ).fetchone()
            if not has_table:
                return None
            row = conn.execute('SELECT * FROM phone_plans WHERE plan_id=?', (plan_id,)).fetchone()
            return self._public_plan(row) if row else None

    def get_call(self, call_id: str, *, include_transcript: bool = True) -> dict | None:
        conn = self._connect_read()
        if conn is None:
            return None
        with closing(conn):
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phone_calls'"
            ).fetchone()
            if not has_table:
                return None
            row = conn.execute('SELECT * FROM phone_calls WHERE call_id=?', (call_id,)).fetchone()
            return self._public_call(row, include_transcript=include_transcript) if row else None

    def list_plans(self, *, status: str | None = None, limit: int = 25) -> list[dict]:
        conn = self._connect_read()
        if conn is None:
            return []
        cap = max(1, min(100, int(limit)))
        with closing(conn):
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phone_plans'"
            ).fetchone()
            if not has_table:
                return []
            if status:
                rows = conn.execute(
                    'SELECT * FROM phone_plans WHERE status=? ORDER BY created_at DESC LIMIT ?',
                    (status, cap),
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM phone_plans ORDER BY created_at DESC LIMIT ?',
                    (cap,),
                ).fetchall()
            return [self._public_plan(r) for r in rows]

    def list_calls(self, *, limit: int = 25, include_transcript: bool = True) -> list[dict]:
        conn = self._connect_read()
        if conn is None:
            return []
        cap = max(1, min(100, int(limit)))
        with closing(conn):
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phone_calls'"
            ).fetchone()
            if not has_table:
                return []
            rows = conn.execute(
                'SELECT * FROM phone_calls ORDER BY created_at DESC LIMIT ?',
                (cap,),
            ).fetchall()
            return [self._public_call(r, include_transcript=include_transcript) for r in rows]

    def status_summary(
        self, *, now: datetime | None = None, env_override: dict[str, str] | None = None
    ) -> dict:
        """Read-only status snapshot (zero DB file creation when uninitialized)."""
        now_dt = _utc_now(now)
        try:
            settings = load_settings(self.directory)
        except Exception:
            settings = {}
        creds = resolve_livekit_credentials(env_override=env_override, require_all=False)
        configured_keys = [k for k in _REQUIRED_DISPATCH_ENV if creds.get(k)]
        missing_keys = [k for k in _REQUIRED_DISPATCH_ENV if not creds.get(k)]

        stop_info = self.phone_stopped_info()
        pending_plans = []
        approved_plans = []
        recent_calls = []
        active_call = None
        rings_last_hour = 0

        conn = self._connect_read()
        if conn is not None:
            with closing(conn):
                tables = {
                    r[0]
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                now_iso = _iso(now_dt)
                if 'phone_plans' in tables:
                    p_rows = conn.execute(
                        """
                        SELECT * FROM phone_plans
                        WHERE status='pending_approval' AND expires_at > ?
                        ORDER BY created_at DESC LIMIT 20
                        """,
                        (now_iso,),
                    ).fetchall()
                    pending_plans = [self._public_plan(r) for r in p_rows]
                    a_rows = conn.execute(
                        """
                        SELECT * FROM phone_plans
                        WHERE status='approved' AND (approval_expires_at IS NULL OR approval_expires_at > ?)
                        ORDER BY approved_at DESC LIMIT 10
                        """,
                        (now_iso,),
                    ).fetchall()
                    approved_plans = [self._public_plan(r) for r in a_rows]
                if 'phone_calls' in tables:
                    c_rows = conn.execute(
                        'SELECT * FROM phone_calls ORDER BY created_at DESC LIMIT 20'
                    ).fetchall()
                    recent_calls = [self._public_call(r, include_transcript=True) for r in c_rows]
                    for c in recent_calls:
                        if c['status'] in _ACTIVE_CALL_STATUSES:
                            active_call = c
                            break
                if 'owner_rings' in tables:
                    hour_ago = _iso(now_dt - timedelta(hours=1))
                    rings_last_hour = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM owner_rings WHERE created_at >= ? AND status != 'dispatch_failed'",
                            (hour_ago,),
                        ).fetchone()[0]
                    )

        owner_phone = str(settings.get('ownerPhone') or '')
        return {
            'phone_enabled': bool(settings.get('phoneEnabled')),
            'owner_ring_enabled': bool(settings.get('ownerRingEnabled')),
            'owner_phone_masked': mask_phone(owner_phone) if owner_phone else '',
            'quiet_hours_breakthrough': bool(settings.get('quietHoursBreakthrough')),
            'inbound_screening_enabled': bool(settings.get('inboundScreeningEnabled')),
            'max_rings_per_hour': int(settings.get('maxRingsPerHour') or 3),
            'min_ring_gap_seconds': int(settings.get('minRingGapSeconds') or 120),
            'rings_last_hour': rings_last_hour,
            'livekit_agent_name': str(settings.get('livekitAgentName') or 'butler-voice'),
            'caller_name': str(settings.get('callerName') or 'Butler'),
            'credentials_ready': len(missing_keys) == 0,
            'configured_env': configured_keys,
            'missing_env': missing_keys,
            'phone_stopped': stop_info['stopped'],
            'phone_stopped_reason': stop_info['reason'],
            'phone_stopped_at': stop_info['updated_at'],
            'active_call': active_call,
            'pending_plans': pending_plans,
            'approved_plans': approved_plans,
            'recent_calls': recent_calls,
        }


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m butler.phone',
        description='Human-only approval and kill-switch CLI for Butler LiveKit phone calls.',
    )
    parser.add_argument('--data-dir', default='', help='Override Butler plugin-data directory')
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('status', help='Show phone status, pending plans, and recent calls')

    ap = sub.add_parser('approve', help='Approve a pending outbound call plan')
    ap.add_argument('plan_id', help='Plan ID (e.g. plan_...)')
    ap.add_argument('--sha256', default='', help='Optional expected script SHA-256')

    rj = sub.add_parser('reject', help='Reject a pending or approved outbound call plan')
    rj.add_argument('plan_id', help='Plan ID')
    rj.add_argument('--reason', default='', help='Optional rejection reason')

    st = sub.add_parser('stop', help='Engage the master phone_stopped kill-switch')
    st.add_argument('--reason', default='Stopped via CLI', help='Kill-switch reason')

    sub.add_parser('resume', help='Clear the master phone_stopped kill-switch')

    args = parser.parse_args(argv)
    store = PhoneStore(Path(args.data_dir).expanduser() if args.data_dir else None)
    try:
        if args.cmd == 'status':
            print(json.dumps(store.status_summary(), indent=2, ensure_ascii=False))
        elif args.cmd == 'approve':
            plan = store.approve_plan(
                args.plan_id, approved_by='human_cli', expected_sha256=args.sha256 or None
            )
            print(json.dumps({'ok': True, 'plan': plan}, indent=2, ensure_ascii=False))
        elif args.cmd == 'reject':
            plan = store.reject_plan(args.plan_id, reason=args.reason)
            print(json.dumps({'ok': True, 'plan': plan}, indent=2, ensure_ascii=False))
        elif args.cmd == 'stop':
            res = store.set_phone_stopped(True, reason=args.reason)
            print(json.dumps({'ok': True, **res}, indent=2, ensure_ascii=False))
        elif args.cmd == 'resume':
            res = store.set_phone_stopped(False)
            print(json.dumps({'ok': True, **res}, indent=2, ensure_ascii=False))
        return 0
    except PhoneError as exc:
        print(json.dumps({'ok': False, 'error': exc.code, 'detail': exc.detail}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(_cli_main())
