"""Optional LiveKit Agents voice worker for Butler (`python -m butler.voice_worker start`).

Core Butler (`butler.phone`) dispatches rooms over pure-stdlib HTTPS Twirp without
requiring `livekit` or `livekit-agents` in the Hermes gateway process. Install the
optional `[voice]` extra (`pip install -e '.[voice]'`) in the worker environment to
run real-time PSTN audio sessions over LiveKit SIP:

- `OwnerRingAgent` (`direction='owner_ring'`): rings the owner's carrier phone,
  states the urgent reason concisely, bridges live questions to headless `hermes`
  (`ask_butler`), and honors verbal kill-switch phrases (`stop_escalation`).
- `OutboundButlerAgent` (`direction='outbound'`): re-verifies human approval, 24h TTL,
  blocklist, `phone_stopped`, and canonical `SHA-256` right before `CreateSIPParticipant`,
  delivers mandatory first-sentence AI disclosure, handles AMD/voicemail, bounds
  RFC 4733 DTMF to `allowed_dtmf`, and uses token-free `HoldMonitor` while on hold.
- `InboundButlerAgent` (`direction='inbound'`): screens inbound SIP calls, links
  recent outbound plans via `open_plans_for`, filters spam, and saves structured
  post-call summaries for Butler.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Callable

from .paths import butler_data_dir
from .phone import (
    PhoneError,
    PhoneSafetyError,
    PhoneStore,
    build_ai_disclosure_opening,
    create_sip_participant,
    mask_phone,
    resolve_livekit_credentials,
)
from .settings import load as load_settings
try:
    from .followup import VISIBLE_WAKE_MARKER
except ImportError:
    VISIBLE_WAKE_MARKER = 'Butler check-in.'

UTC = timezone.utc

MAX_DTMF_PER_CALL = 16
DEFAULT_MAX_HOLD_SECONDS = 900.0
DEFAULT_HERMES_TIMEOUT_SECONDS = 45.0

_DTMF_CODE_MAP: dict[str, int] = {
    '0': 0, '1': 1, '2': 2, '3': 3, '4': 4,
    '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
    '*': 10, '#': 11,
}

_STOP_PHRASE_RE = re.compile(
    r'\b(?:stop\s+calling\s+me|don\x27?t\s+call\s+me(?:\s+again)?|never\s+call\s+me|'
    r'disable\s+phone\s+calls|phone\s+kill\s*switch|stop\s+all\s+calls)\b',
    re.IGNORECASE,
)

_VOICEMAIL_RE = re.compile(
    r'(?:leave\s+(?:a\s+|your\s+)?(?:brief\s+|detailed\s+)?message|'
    r'after\s+the\s+(?:beep|tone)|'
    r'person\s+you\s+are\s+trying\s+to\s+reach|'
    r'not\s+available\s+to\s+take\s+your\s+call|'
    r'reached\s+(?:the\s+)?voicemail|'
    r'mailbox\s+is\s+full|'
    r'record\s+your\s+message)',
    re.IGNORECASE,
)

_HOLD_PHRASE_RE = re.compile(
    r'(?:please\s+(?:hold|continue\s+to\s+hold|stay\s+on\s+the\s+line)|'
    r'your\s+call\s+is\s+(?:very\s+)?important\s+to\s+us|'
    r'all\s+(?:of\s+our\s+)?(?:representatives|agents|operators)\s+are\s+(?:currently\s+)?(?:busy|assisting)|'
    r'next\s+available\s+(?:representative|agent)|'
    r'estimated\s+wait\s+time|'
    r'placing\s+you\s+on\s+hold|'
    r'one\s+moment\s+while\s+i\s+(?:transfer|check))',
    re.IGNORECASE,
)

_HUMAN_RETURN_RE = re.compile(
    r'\b(?:hello|hi\s+there|thanks?\s+for\s+(?:holding|waiting)|how\s+can\s+i\s+help|'
    r'i\x27?m\s+back|are\s+you\s+still\s+there|speaking)\b',
    re.IGNORECASE,
)


def is_verbal_stop_request(text: str) -> bool:
    """Detect explicit verbal instructions from the owner to halt phone escalations."""
    return bool(text and _STOP_PHRASE_RE.search(text))


def detect_answering_machine(text: str) -> bool:
    """Detect common voicemail / answering-machine prompts in early call transcripts."""
    return bool(text and _VOICEMAIL_RE.search(text))


def classify_utterance_for_hold(text: str) -> str:
    """Classify an utterance as `'hold'`, `'human_return'`, or `'normal'` for HoldMonitor."""
    if not text or not text.strip():
        return 'normal'
    if _HOLD_PHRASE_RE.search(text):
        return 'hold'
    if _HUMAN_RETURN_RE.search(text):
        return 'human_return'
    return 'normal'


class HoldMonitor:
    """Token-free state machine that suppresses LLM replies during IVR/queue hold."""

    def __init__(
        self,
        *,
        max_hold_seconds: float = DEFAULT_MAX_HOLD_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_hold_seconds = max(30.0, float(max_hold_seconds))
        self._clock = clock
        self.on_hold = False
        self.hold_reason = ''
        self.entered_at: float | None = None
        self.total_hold_seconds: float = 0.0
        self.transitions: int = 0

    def enter_hold(self, reason: str = 'queue_hold') -> None:
        if not self.on_hold:
            self.on_hold = True
            self.hold_reason = (reason or 'queue_hold').strip()[:120]
            self.entered_at = self._clock()
            self.transitions += 1

    def exit_hold(self) -> float:
        if not self.on_hold:
            return 0.0
        elapsed = self.current_hold_seconds()
        self.total_hold_seconds += elapsed
        self.on_hold = False
        self.hold_reason = ''
        self.entered_at = None
        return elapsed

    def current_hold_seconds(self) -> float:
        if not self.on_hold or self.entered_at is None:
            return 0.0
        return max(0.0, self._clock() - self.entered_at)

    def is_timed_out(self) -> bool:
        return (self.total_hold_seconds + self.current_hold_seconds()) >= self.max_hold_seconds

    def observe_remote_speech(self, text: str) -> str:
        """Update hold state from remote transcript without spending LLM tokens.

        Returns:
          - `'suppress'` while still on hold (caller should skip LLM reply generation),
          - `'timed_out'` when hold exceeded `max_hold_seconds`,
          - `'resumed'` when a live human returned from hold,
          - `'active'` during normal conversation.
        """
        if self.is_timed_out():
            return 'timed_out'
        kind = classify_utterance_for_hold(text)
        if not self.on_hold:
            if kind == 'hold':
                self.enter_hold(text[:80])
                return 'suppress'
            return 'active'
        if kind == 'human_return':
            self.exit_hold()
            return 'resumed'
        return 'suppress'


class DtmfController:
    """Enforces plan-scoped RFC 4733 DTMF allowlists and per-call digit caps."""

    def __init__(self, allowed_dtmf: str = '', *, max_digits: int = MAX_DTMF_PER_CALL):
        raw = (allowed_dtmf or '').strip()
        self.allowed_spec = raw
        self.allowed_chars = frozenset(ch for ch in raw if ch in _DTMF_CODE_MAP)
        self.max_digits = max(1, int(max_digits))
        self.sent_digits: list[str] = []

    def validate_sequence(self, sequence: str) -> list[str]:
        """Return ordered tokens (`'0'`-`'9'`, `'*'`, `'#'`, or `'w'` pause) or raise PhoneSafetyError."""
        if not self.allowed_chars:
            raise PhoneSafetyError(
                'dtmf_not_permitted',
                'DTMF tones are disabled for this call plan (allowed_dtmf is empty)',
            )
        cleaned = (sequence or '').strip()
        if not cleaned:
            raise PhoneSafetyError('empty_dtmf_sequence', 'DTMF sequence cannot be empty')
        tokens: list[str] = []
        digit_count = 0
        for ch in cleaned:
            if ch in ('w', 'W'):
                tokens.append('w')
                continue
            if ch not in _DTMF_CODE_MAP:
                raise PhoneSafetyError('invalid_dtmf_char', f'Unsupported DTMF character: {ch!r}')
            if ch not in self.allowed_chars:
                raise PhoneSafetyError(
                    'dtmf_digit_not_allowed',
                    f'DTMF digit {ch!r} is not permitted by approved allowed_dtmf',
                )
            digit_count += 1
            tokens.append(ch)
        if digit_count == 0:
            raise PhoneSafetyError('empty_dtmf_sequence', 'At least one DTMF tone is required')
        if len(self.sent_digits) + digit_count > self.max_digits:
            raise PhoneSafetyError(
                'dtmf_cap_exceeded',
                f'DTMF per-call limit ({self.max_digits} digits) exceeded',
            )
        return tokens

    async def send(
        self,
        sequence: str,
        publish_fn: Callable[[int, str], Any],
        *,
        pause_seconds: float = 0.5,
        inter_digit_seconds: float = 0.18,
    ) -> str:
        tokens = self.validate_sequence(sequence)
        sent_now: list[str] = []
        for tok in tokens:
            if tok == 'w':
                await asyncio.sleep(max(0.0, pause_seconds))
                continue
            code = _DTMF_CODE_MAP[tok]
            res = publish_fn(code, tok)
            if asyncio.iscoroutine(res):
                await res
            self.sent_digits.append(tok)
            sent_now.append(tok)
            if inter_digit_seconds > 0:
                await asyncio.sleep(inter_digit_seconds)
        return ''.join(sent_now)


class TranscriptRecorder:
    """Collects sanitized call utterances, persists results, and parks wake context."""

    def __init__(self, store: PhoneStore, call_id: str, *, direction: str, label: str = ''):
        self.store = store
        self.call_id = call_id
        self.direction = direction
        self.label = (label or direction).strip()[:80]
        self.started_mono = time.monotonic()
        self.entries: list[dict[str, str]] = []
        self._finalized = False

    def add(self, speaker: str, text: str, *, now: datetime | None = None) -> None:
        clean = ' '.join(str(text or '').strip().split())
        if not clean:
            return
        ts = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        self.entries.append({
            'speaker': (speaker or 'agent').strip()[:24],
            'text': clean[:1000],
            'ts': ts,
        })

    def finalize(
        self,
        *,
        status: str = 'completed',
        outcome_summary: str = '',
        park_followup: bool = True,
    ) -> dict:
        if self._finalized:
            return self.store.get_call(self.call_id) or {}
        self._finalized = True
        dur = max(0, int(time.monotonic() - self.started_mono))
        summary = ' '.join(str(outcome_summary or '').strip().split())[:1000]
        if not summary and self.entries:
            last = self.entries[-1]
            summary = f"Call ended ({status}). Last utterance ({last['speaker']}): {last['text'][:220]}"
        elif not summary:
            summary = f'Call finished with status: {status}'
        saved = self.store.save_call_result(
            self.call_id,
            status=status,
            outcome_summary=summary,
            transcript=self.entries,
            duration_seconds=dur,
        )
        if park_followup:
            # Enqueue hidden post-call wake context in ResponsibilityStore (chat hygiene:
            # visible chat turns carry only VISIBLE_WAKE_MARKER, never internal IDs/prompts).
            hidden_prompt = (
                f"Phone call completed ({self.direction}, status={status}, duration={dur}s, "
                f"target={saved.get('masked_number', '')}, label={self.label}).\n"
                f"Outcome summary: {summary}"
            )
            try:
                from .routes import RouteRegistry
                from .store import ResponsibilityStore
                db_path = self.store.directory / 'responsibilities.sqlite3'
                with RouteRegistry(db_path) as reg:
                    routes = reg.list_routes()
                target_route_ids = []
                for r in routes:
                    if r.get('is_preferred') and r.get('route_id'):
                        target_route_ids.append(r['route_id'])
                        break
                if not target_route_ids and routes and routes[0].get('route_id'):
                    target_route_ids.append(routes[0]['route_id'])
                if not target_route_ids:
                    target_route_ids.append('phone:post_call')
                rstore = ResponsibilityStore(db_path)
                try:
                    now_utc = datetime.now(UTC)
                    for rid in target_route_ids:
                        rstore.enqueue_pending_wake(rid, hidden_prompt, now_utc)
                finally:
                    rstore.close()
            except Exception:
                log.debug('Butler voice worker could not enqueue post-call wake', exc_info=True)
        return saved


async def run_ask_butler_bridge(
    question: str,
    *,
    hermes_bin: str = '',
    timeout_seconds: float = DEFAULT_HERMES_TIMEOUT_SECONDS,
    exec_fn=None,
) -> str:
    """Query headless Hermes CLI (`hermes chat -q ... --quiet`) for live owner questions."""
    clean_q = ' '.join(str(question or '').strip().split())[:600]
    if not clean_q:
        return 'I did not catch a specific question to check, sir.'
    binary = hermes_bin or shutil.which('hermes') or ''
    if not binary and exec_fn is None:
        return 'Hermes CLI is not available on this worker host right now.'
    spoken_prompt = (
        'You are answering a live voice query for Butler while the owner is on a phone call. '
        'Reply in 1 to 3 plain spoken sentences with no markdown, URLs, or raw IDs. '
        f'Question: {clean_q}'
    )
    runner = exec_fn or asyncio.create_subprocess_exec
    try:
        proc = await runner(
            binary or 'hermes',
            'chat',
            '-q',
            spoken_prompt,
            '--quiet',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return 'Checking that is taking longer than expected; I will follow up in messages shortly.'
    except OSError:
        return 'I could not reach the local Hermes session from the voice worker.'
    text = (stdout_b or b'').decode('utf-8', errors='replace').strip()
    if not text:
        return 'I checked, sir, and nothing new came back.'
    return ' '.join(text.split())[:700]


class OwnerRingController:
    """Deterministic safety & tool controller for `OwnerRingAgent`."""

    def __init__(
        self,
        store: PhoneStore,
        *,
        call_id: str,
        room_name: str,
        opening_reason: str,
        context_brief: str = '',
        caller_name: str = 'Butler',
        hermes_bin: str = '',
    ):
        self.store = store
        self.call_id = call_id
        self.room_name = room_name
        self.opening_reason = ' '.join(str(opening_reason or '').strip().split())[:400]
        self.context_brief = str(context_brief or '').strip()[:1500]
        self.caller_name = (caller_name or 'Butler').strip()[:64] or 'Butler'
        self.hermes_bin = hermes_bin
        self.recorder = TranscriptRecorder(
            store, call_id, direction='owner_ring', label='Owner ring'
        )
        self.hangup_requested = False

    def system_instructions(self) -> str:
        ctx = f'\nContext brief: {self.context_brief}' if self.context_brief else ''
        return (
            f'You are {self.caller_name}, a calm, concise, capable executive assistant ringing '
            f'your owner on the phone for a time-sensitive matter. '
            f'Address the owner naturally ("sir" is fine when natural; never say "Your Highness" or "Overseer"). '
            f'Keep every spoken turn brief (1-2 sentences). '
            f'Opening reason for calling: {self.opening_reason}.{ctx}\n'
            f'Use `ask_butler` if the owner asks you to look up or verify anything in Hermes. '
            f'If the owner says "stop calling me" or asks to halt phone calls, immediately call `stop_escalation`.'
        )

    def dial_owner_sip(
        self,
        *,
        creds: dict[str, str],
        sip_fn=create_sip_participant,
    ) -> dict:
        target = self.store.get_owner_ring_target(self.call_id)
        return sip_fn(
            livekit_url=creds['LIVEKIT_URL'],
            api_key=creds['LIVEKIT_API_KEY'],
            api_secret=creds['LIVEKIT_API_SECRET'],
            sip_trunk_id=creds['LIVEKIT_SIP_TRUNK_ID'],
            call_to=target['to_e164'],
            room_name=self.room_name,
            participant_identity=f'owner-{self.call_id}',
            participant_name='Owner',
            from_number=creds.get('LIVEKIT_CALLER_NUMBER', ''),
        )

    def handle_owner_utterance(self, text: str) -> str | None:
        """Record owner speech and deterministically trip `phone_stopped` on stop phrases."""
        self.recorder.add('owner', text)
        if is_verbal_stop_request(text):
            return self.stop_escalation(reason=f'Verbal stop on call: {text[:120]}')
        return None

    def stop_escalation(self, reason: str = 'Owner requested stop during phone call') -> str:
        self.store.set_phone_stopped(True, reason=reason)
        self.hangup_requested = True
        reply = 'Understood, sir. I have enabled the phone kill-switch and will not call again until you re-enable it.'
        self.recorder.add('agent', reply)
        self.recorder.finalize(
            status='cancelled',
            outcome_summary=f'Owner activated phone_stopped kill-switch: {reason[:200]}',
            park_followup=False,
        )
        return reply

    async def ask_butler(self, question: str, *, exec_fn=None) -> str:
        return await run_ask_butler_bridge(
            question, hermes_bin=self.hermes_bin, exec_fn=exec_fn
        )

    def complete(self, summary: str = '', *, status: str = 'completed') -> dict:
        self.hangup_requested = True
        return self.recorder.finalize(status=status, outcome_summary=summary, park_followup=True)


class OutboundCallController:
    """Double-gated safety, AMD, DTMF, and HoldMonitor controller for `OutboundButlerAgent`."""

    def __init__(
        self,
        store: PhoneStore,
        *,
        plan_id: str,
        call_id: str,
        expected_sha256: str,
        room_name: str,
        caller_name: str = 'Butler',
    ):
        self.store = store
        self.plan_id = plan_id
        self.call_id = call_id
        self.expected_sha256 = expected_sha256
        self.room_name = room_name
        self.caller_name = (caller_name or 'Butler').strip()[:64] or 'Butler'
        self.verified_plan: dict | None = None
        self.dtmf: DtmfController | None = None
        self.hold = HoldMonitor()
        self.recorder = TranscriptRecorder(
            store, call_id, direction='outbound', label=plan_id
        )
        self.hangup_requested = False

    def verify_and_dial_sip(
        self,
        *,
        creds: dict[str, str],
        sip_fn=create_sip_participant,
    ) -> dict:
        """Gate 2: Re-verify approval, TTL, blocklist, kill-switch, and SHA-256 right before SIP dial."""
        plan = self.store.verify_outbound_ready_for_sip(
            plan_id=self.plan_id,
            call_id=self.call_id,
            expected_sha256=self.expected_sha256,
        )
        self.verified_plan = plan
        self.recorder.label = plan.get('recipient_label') or plan.get('masked_number') or 'Outbound'
        self.dtmf = DtmfController(plan.get('allowed_dtmf', ''))
        return sip_fn(
            livekit_url=creds['LIVEKIT_URL'],
            api_key=creds['LIVEKIT_API_KEY'],
            api_secret=creds['LIVEKIT_API_SECRET'],
            sip_trunk_id=creds['LIVEKIT_SIP_TRUNK_ID'],
            call_to=plan['to_e164'],
            room_name=self.room_name,
            participant_identity=f'pstn-{self.call_id}',
            participant_name=plan.get('recipient_label') or 'Recipient',
            from_number=creds.get('LIVEKIT_CALLER_NUMBER', ''),
        )

    def opening_disclosure(self) -> str:
        if not self.verified_plan:
            raise PhoneSafetyError('unverified_outbound_plan', 'Outbound plan has not passed Gate 2 verification')
        opening = build_ai_disclosure_opening(
            self.verified_plan['ai_disclosure_opening'], caller_name=self.caller_name
        )
        self.recorder.add('agent', opening)
        return opening

    def system_instructions(self) -> str:
        if not self.verified_plan:
            raise PhoneSafetyError('unverified_outbound_plan', 'Outbound plan has not passed Gate 2 verification')
        p = self.verified_plan
        dtmf_hint = (
            f"Allowed DTMF digits for IVR menus: {p['allowed_dtmf']}."
            if p.get('allowed_dtmf')
            else 'DTMF tones are disabled for this call.'
        )
        notes = f"\nAdditional context: {p['context_notes']}" if p.get('context_notes') else ''
        return (
            f"You are {self.caller_name}, an automated AI assistant placing a human-approved call "
            f"to {p['recipient_label']} ({p['masked_number']}).\n"
            f"Mandatory first sentence (already spoken or speak verbatim first): {p['ai_disclosure_opening']}\n"
            f"Approved objective: {p['objective']}{notes}\n"
            f"{dtmf_hint}\n"
            'Stay strictly within the approved objective. Never invent payment details, passwords, '
            'personal identifiers, or unapproved commitments. Be polite, concise, and call `end_call` '
            'as soon as the objective is resolved.'
        )

    def observe_remote_utterance(self, text: str) -> dict[str, Any]:
        """Inspect remote speech for AMD (voicemail) and token-free HoldMonitor transitions."""
        self.recorder.add('remote', text)
        plan = self.verified_plan or {}
        if len(self.recorder.entries) <= 4 and detect_answering_machine(text):
            policy = plan.get('voicemail_policy') or 'hangup'
            vm_msg = (plan.get('voicemail_message') or '').strip()
            self.hangup_requested = True
            if policy == 'leave_message' and vm_msg:
                spoken = f"{plan.get('ai_disclosure_opening', '')} {vm_msg}".strip()
                self.recorder.add('agent', spoken)
                self.recorder.finalize(
                    status='voicemail',
                    outcome_summary=f'Voicemail detected; left approved message for {self.recorder.label}.',
                )
                return {'action': 'speak_and_hangup', 'text': spoken, 'status': 'voicemail'}
            self.recorder.finalize(
                status='voicemail',
                outcome_summary=f'Voicemail detected for {self.recorder.label}; hung up per voicemail_policy=hangup.',
            )
            return {'action': 'hangup', 'status': 'voicemail'}

        hold_state = self.hold.observe_remote_speech(text)
        if hold_state == 'timed_out':
            self.hangup_requested = True
            self.recorder.finalize(
                status='failed',
                outcome_summary='Exceeded maximum hold duration (15m); hung up cleanly.',
            )
            return {'action': 'hangup', 'status': 'hold_timeout'}
        if hold_state == 'suppress':
            return {'action': 'suppress_llm', 'on_hold': True}
        if hold_state == 'resumed':
            return {'action': 'resume_llm', 'on_hold': False}
        return {'action': 'normal', 'on_hold': False}

    async def send_dtmf(self, digits: str, publish_fn: Callable[[int, str], Any]) -> str:
        if self.dtmf is None:
            raise PhoneSafetyError('unverified_outbound_plan', 'Outbound plan not verified')
        sent = await self.dtmf.send(digits, publish_fn)
        self.recorder.add('agent_dtmf', sent)
        return sent

    def complete(self, outcome_summary: str, *, status: str = 'completed') -> dict:
        self.hangup_requested = True
        return self.recorder.finalize(status=status, outcome_summary=outcome_summary, park_followup=True)


class InboundCallController:
    """Inbound call screening and callback-linking controller for `InboundButlerAgent`."""

    def __init__(
        self,
        store: PhoneStore,
        *,
        caller_number: str,
        room_name: str,
        caller_name: str = 'Butler',
        call_id: str | None = None,
    ):
        self.store = store
        self.caller_number = caller_number
        self.room_name = room_name
        self.caller_name = (caller_name or 'Butler').strip()[:64] or 'Butler'
        settings = load_settings(store.directory)
        if not settings.get('phoneEnabled') or not settings.get('inboundScreeningEnabled'):
            raise PhoneSafetyError('inbound_screening_disabled', 'Inbound call screening is disabled')
        if store.is_phone_stopped():
            raise PhoneSafetyError('phone_stopped_kill_switch', 'Inbound screening halted by phone_stopped')
        record = store.record_inbound_call(
            caller_number=caller_number, room_name=room_name, call_id=call_id
        )
        self.call_id = record['call_id']
        self.masked_number = record['masked_number']
        self.matched_plans: list[dict] = record.get('matched_plans') or []
        label = record.get('recipient_label') or self.masked_number or 'Inbound caller'
        self.recorder = TranscriptRecorder(
            store, self.call_id, direction='inbound', label=label
        )
        self.hangup_requested = False

    def opening_greeting(self) -> str:
        if self.matched_plans:
            top = self.matched_plans[0]
            greeting = (
                f"Hello, this is {self.caller_name}, an automated AI assistant for my owner. "
                f"It looks like you may be returning our call regarding {top['recipient_label']}; how can I help?"
            )
        else:
            greeting = (
                f"Hello, this is {self.caller_name}, an automated AI assistant screening calls for my owner. "
                'May I ask who is calling and how I can help?'
            )
        self.recorder.add('agent', greeting)
        return greeting

    def system_instructions(self) -> str:
        plan_ctx = ''
        if self.matched_plans:
            top = self.matched_plans[0]
            plan_ctx = (
                f"\nMatched recent outbound call plan ({top['plan_id']}) to {top['recipient_label']}: "
                f"objective was '{top['objective']}'."
            )
        return (
            f"You are {self.caller_name}, an automated AI assistant screening an inbound call from {self.masked_number}.{plan_ctx}\n"
            'Never share private owner details, schedules, addresses, or credentials. '
            'If the caller is a robocall, telemarketer, or spam, call `mark_spam_and_hangup`. '
            'Otherwise gather their name, message, and callback details concisely, then call `record_message_and_end`.'
        )

    def mark_spam_and_hangup(self, reason: str = 'Automated/spam call') -> dict:
        self.hangup_requested = True
        return self.recorder.finalize(
            status='screened_spam',
            outcome_summary=f'Screened spam/robocall ({self.masked_number}): {reason[:200]}',
            park_followup=False,
        )

    def record_message_and_end(
        self, caller_label: str, message_summary: str, *, urgent: bool = False
    ) -> dict:
        self.hangup_requested = True
        prefix = '[URGENT] ' if urgent else ''
        summary = f'{prefix}Inbound message from {caller_label.strip()[:80]} ({self.masked_number}): {message_summary.strip()[:500]}'
        return self.recorder.finalize(
            status='completed',
            outcome_summary=summary,
            park_followup=True,
        )


def parse_dispatch_metadata(raw_metadata: str | None) -> dict[str, Any]:
    if not raw_metadata or not str(raw_metadata).strip():
        return {}
    try:
        parsed = json.loads(raw_metadata)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _require_livekit_agents():
    try:
        lk_agents = importlib.import_module('livekit.agents')
        rtc = importlib.import_module('livekit.rtc')
        return lk_agents, rtc
    except ImportError as exc:
        raise RuntimeError(
            "Butler voice worker requires optional LiveKit packages. "
            "Install them with: pip install -e '.[voice]'"
        ) from exc


def build_livekit_entrypoint(*, default_data_dir: Path | None = None, hermes_bin: str = ''):
    """Build the async `entrypoint(ctx)` handler for `livekit-agents` WorkerOptions."""
    lk_agents, _rtc = _require_livekit_agents()
    lk_openai = importlib.import_module('livekit.plugins.openai')
    lk_silero = importlib.import_module('livekit.plugins.silero')
    Agent = lk_agents.Agent
    AgentSession = lk_agents.AgentSession
    function_tool = lk_agents.function_tool

    async def entrypoint(ctx) -> None:
        await ctx.connect()
        job_meta = parse_dispatch_metadata(getattr(ctx.job, 'metadata', '') or getattr(ctx.room, 'metadata', ''))
        direction = str(job_meta.get('direction') or 'inbound').strip()
        data_dir = Path(job_meta['butler_data_dir']).expanduser() if job_meta.get('butler_data_dir') else (
            default_data_dir or butler_data_dir(create=False)
        )
        store = PhoneStore(data_dir)
        creds = resolve_livekit_credentials(require_all=(direction in ('owner_ring', 'outbound')))
        caller_name = str(job_meta.get('caller_name') or load_settings(data_dir).get('callerName') or 'Butler')

        def _publish_dtmf(code: int, digit: str):
            return ctx.room.local_participant.publish_dtmf(code=code, digit=digit)

        session = AgentSession(
            vad=lk_silero.VAD.load(),
            stt=lk_openai.STT(),
            llm=lk_openai.LLM(),
            tts=lk_openai.TTS(),
        )

        if direction == 'owner_ring':
            ctrl = OwnerRingController(
                store,
                call_id=str(job_meta['call_id']),
                room_name=ctx.room.name,
                opening_reason=str(job_meta.get('opening_reason') or 'Urgent follow-up'),
                context_brief=str(job_meta.get('context_brief') or ''),
                caller_name=caller_name,
                hermes_bin=hermes_bin,
            )
            await asyncio.to_thread(ctrl.dial_owner_sip, creds=creds)

            @function_tool
            async def ask_butler(question: str) -> str:
                """Query Butler's Hermes session for live commitments, messages, or context."""
                return await ctrl.ask_butler(question)

            @function_tool
            async def stop_escalation(reason: str = 'Owner requested stop') -> str:
                """Latch the master phone_stopped kill-switch and end the call immediately."""
                msg = ctrl.stop_escalation(reason)
                await ctx.room.disconnect()
                return msg

            @function_tool
            async def end_call(summary: str = 'Owner acknowledged update') -> str:
                """Save the call summary and disconnect cleanly."""
                ctrl.complete(summary)
                await ctx.room.disconnect()
                return 'Call ended.'

            agent = Agent(
                instructions=ctrl.system_instructions(),
                tools=[ask_butler, stop_escalation, end_call],
            )
            await session.start(agent=agent, room=ctx.room)
            await session.say(ctrl.opening_reason, allow_interruptions=True)
            return

        if direction == 'outbound':
            ctrl = OutboundCallController(
                store,
                plan_id=str(job_meta['plan_id']),
                call_id=str(job_meta['call_id']),
                expected_sha256=str(job_meta.get('script_sha256') or ''),
                room_name=ctx.room.name,
                caller_name=caller_name,
            )
            await asyncio.to_thread(ctrl.verify_and_dial_sip, creds=creds)

            @function_tool
            async def send_dtmf(digits: str) -> str:
                """Send IVR touch-tone digits permitted by this call plan's allowed_dtmf."""
                return await ctrl.send_dtmf(digits, _publish_dtmf)

            @function_tool
            async def enter_hold_mode(reason: str = 'Placed on hold') -> str:
                """Enter token-free HoldMonitor mode while waiting on hold music or queue."""
                ctrl.hold.enter_hold(reason)
                return 'Hold mode enabled; staying silent until a human returns.'

            @function_tool(name='end_call')
            async def finish_outbound_call(outcome_summary: str) -> str:
                """Record the final call outcome and disconnect."""
                ctrl.complete(outcome_summary)
                await ctx.room.disconnect()
                return 'Call completed.'

            agent = Agent(
                instructions=ctrl.system_instructions(),
                tools=[send_dtmf, enter_hold_mode, finish_outbound_call],
            )
            await session.start(agent=agent, room=ctx.room)
            await session.say(ctrl.opening_disclosure(), allow_interruptions=False)
            return

        # Inbound SIP call screening
        caller_num = str(job_meta.get('caller_number') or 'unknown')
        for p in ctx.room.remote_participants.values():
            attrs = getattr(p, 'attributes', None) or {}
            sip_from = attrs.get('sip.phoneNumber') or attrs.get('sip.callFrom')
            if sip_from:
                caller_num = sip_from
                break
        ctrl = InboundCallController(
            store,
            caller_number=caller_num,
            room_name=ctx.room.name,
            caller_name=caller_name,
        )

        @function_tool
        async def mark_spam_and_hangup(reason: str = 'Spam or automated robocall') -> str:
            """Log an unwanted spam/robocall and hang up immediately."""
            ctrl.mark_spam_and_hangup(reason)
            await ctx.room.disconnect()
            return 'Spam call terminated.'

        @function_tool
        async def record_message_and_end(
            caller_label: str, message_summary: str, urgent: bool = False
        ) -> str:
            """Save the caller's message for the owner and disconnect politely."""
            ctrl.record_message_and_end(caller_label, message_summary, urgent=urgent)
            await ctx.room.disconnect()
            return 'Message saved.'

        agent = Agent(
            instructions=ctrl.system_instructions(),
            tools=[mark_spam_and_hangup, record_message_and_end],
        )
        await session.start(agent=agent, room=ctx.room)
        await session.say(ctrl.opening_greeting(), allow_interruptions=True)

    return entrypoint


def run_worker_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m butler.voice_worker',
        description='Optional LiveKit SIP voice worker for Butler.',
    )
    parser.add_argument('mode', nargs='?', default='start', choices=('start', 'dev', 'check'))
    parser.add_argument('--data-dir', default='', help='Override Butler plugin-data directory')
    parser.add_argument('--agent-name', default='', help='Override LiveKit agent_name')
    parser.add_argument('--hermes-bin', default='', help='Path to hermes CLI binary for ask_butler')
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else butler_data_dir(create=False)
    settings = load_settings(data_dir)
    agent_name = args.agent_name or str(settings.get('livekitAgentName') or 'butler-voice')

    if args.mode == 'check':
        creds = resolve_livekit_credentials(require_all=False)
        has_lk = True
        try:
            _require_livekit_agents()
        except RuntimeError:
            has_lk = False
        report = {
            'agent_name': agent_name,
            'data_dir': str(data_dir),
            'livekit_agents_installed': has_lk,
            'configured_env': [k for k, v in creds.items() if v],
            'masked_caller_number': mask_phone(creds.get('LIVEKIT_CALLER_NUMBER', '')),
        }
        print(json.dumps(report, indent=2))
        return 0 if has_lk else 1

    lk_agents, _ = _require_livekit_agents()
    creds = resolve_livekit_credentials(require_all=True)
    entrypoint = build_livekit_entrypoint(default_data_dir=data_dir, hermes_bin=args.hermes_bin)
    opts = lk_agents.WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name=agent_name,
        ws_url=creds['LIVEKIT_URL'],
        api_key=creds['LIVEKIT_API_KEY'],
        api_secret=creds['LIVEKIT_API_SECRET'],
    )
    sys.argv = [sys.argv[0], args.mode]
    lk_agents.cli.run_app(opts)
    return 0


if __name__ == '__main__':
    raise SystemExit(run_worker_cli())
