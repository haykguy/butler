"""Gateway LLM tool (`butler_phone`) for LiveKit SIP calls and owner escalations.

Safety invariants:
- Outbound third-party calls strictly follow `draft_call` -> human approval -> `place_call`.
- Human approval is NEVER exposed as an LLM tool action (Catalog Rule 12).
- `stop_escalation` can only latch `phone_stopped=True`; clearing the kill-switch
  requires explicit human action in Butler Control Center or `python -m butler.phone resume`.
- Returned plans and call records expose `masked_number` only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from .context import detect_system_timezone, resolve_effective_timezone
from .notifications import read_hermes_load_state
from .paths import butler_data_dir
from .phone import PhoneError, PhoneStore
from .policy import Policy
from .settings import load as load_settings

_ACTIONS = (
    'ring_owner',
    'draft_call',
    'place_call',
    'call_status',
    'list_calls',
    'cancel_call',
    'stop_escalation',
)

_READ_ONLY_ACTIONS = frozenset({'call_status', 'list_calls'})

_PHONE_SCHEMA: dict[str, Any] = {
    'name': 'butler_phone',
    'description': (
        'LiveKit SIP phone calling for Butler: ring the owner on urgent matters (`ring_owner`), '
        'draft non-blocking outbound third-party call plans (`draft_call`), dial human-approved '
        'plans (`place_call`), inspect call/plan status and transcripts (`call_status`, `list_calls`), '
        'cancel active calls or unplaced plans (`cancel_call`), or engage the phone kill-switch '
        '(`stop_escalation`). Outbound third-party calls require human approval in Butler Control '
        'Center or `python -m butler.phone approve <plan_id>` before `place_call`.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': list(_ACTIONS),
                'description': 'Phone operation to perform (never approve; human approval happens outside LLM tools).',
            },
            'reason': {
                'type': 'string',
                'description': 'For ring_owner (concise spoken opening reason), cancel_call, or stop_escalation.',
            },
            'context_brief': {
                'type': 'string',
                'description': 'For ring_owner: background notes for the voice agent during the call.',
            },
            'urgent': {
                'type': 'boolean',
                'description': 'For ring_owner: set true only for time-critical emergencies eligible for quietHoursBreakthrough.',
            },
            'to_number': {
                'type': 'string',
                'description': 'For draft_call: destination phone number in E.164 format (e.g. +14155550199). Emergency/premium numbers are blocked.',
            },
            'recipient_label': {
                'type': 'string',
                'description': 'For draft_call: human-readable callee name or business label (e.g. Dr. Patel Office).',
            },
            'objective': {
                'type': 'string',
                'description': 'For draft_call: clear, bounded goal for the call (1-2000 chars).',
            },
            'ai_disclosure_opening': {
                'type': 'string',
                'description': 'For draft_call: optional custom first sentence; must explicitly identify Butler as an automated/AI assistant.',
            },
            'context_notes': {
                'type': 'string',
                'description': 'For draft_call: supporting facts the voice agent may reference (never include raw secrets unless intended).',
            },
            'allowed_dtmf': {
                'type': 'string',
                'description': 'For draft_call: optional allowlist of IVR touch-tone digits (0-9, *, #, w) permitted during the call.',
            },
            'voicemail_policy': {
                'type': 'string',
                'enum': ['hangup', 'leave_message'],
                'description': 'For draft_call: behavior if answering machine / voicemail is detected (default hangup).',
            },
            'voicemail_message': {
                'type': 'string',
                'description': 'For draft_call: concise voicemail script (required when voicemail_policy=leave_message).',
            },
            'plan_id': {
                'type': 'string',
                'description': 'Call plan ID (e.g. plan_...) for place_call, call_status, or cancel_call.',
            },
            'call_id': {
                'type': 'string',
                'description': 'Call ID (e.g. call_...) for call_status or cancel_call.',
            },
            'plan_status': {
                'type': 'string',
                'description': 'For list_calls: optional plan status filter (e.g. pending_approval, approved, completed).',
            },
            'include_transcript': {
                'type': 'boolean',
                'description': 'For call_status or list_calls: include saved call transcript turns.',
            },
            'limit': {
                'type': 'integer',
                'description': 'For list_calls: max records to return (1-100, default 20).',
            },
        },
        'required': ['action'],
        'additionalProperties': False,
    },
}


def register_phone_tools(
    ctx,
    *,
    directory: Callable[[], Path] | None = None,
    store_factory: Callable[[], PhoneStore] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    system_tz_fn: Callable[[], str] = detect_system_timezone,
    config_loader: Callable[[], dict] | None = None,
    env_override: dict[str, str] | None = None,
    dispatch_fn: Callable[..., dict] | None = None,
) -> None:
    def _resolve_dir(*, create: bool) -> Path:
        if directory is not None:
            p = Path(directory())
            if create:
                p.mkdir(parents=True, exist_ok=True, mode=0o700)
            return p
        return butler_data_dir(create=create)

    def _open_store(*, create: bool) -> PhoneStore:
        if store_factory is not None:
            return store_factory()
        return PhoneStore(_resolve_dir(create=create))

    def _effective_tz(prof_dir: Path) -> str:
        try:
            settings = load_settings(prof_dir)
        except Exception:
            settings = {}
        live_cfg = read_hermes_load_state(config_loader=config_loader)
        cfg_tz = str(live_cfg.get('timezone') or '') if isinstance(live_cfg, dict) else ''
        eff_tz, _ = resolve_effective_timezone(
            settings,
            config_timezone=cfg_tz,
            system_timezone=system_tz_fn(),
        )
        return eff_tz

    def handle(args, **kwargs) -> str:
        def reply(**payload: Any) -> str:
            return json.dumps(payload, ensure_ascii=False, default=str)

        if not isinstance(args, dict):
            return reply(ok=False, error='invalid_arguments', detail='arguments must be a JSON object')
        action = args.get('action')
        if action == 'approve':
            return reply(
                ok=False,
                error='human_approval_only',
                detail=(
                    'Outbound call plans cannot be approved by an LLM tool. '
                    'Ask the owner to approve the plan in Butler Control Center '
                    'or run `python -m butler.phone approve <plan_id>`.'
                ),
            )
        if action not in _ACTIONS:
            return reply(ok=False, error='unsupported_action', detail=f'Unsupported phone action: {action!r}')

        now = now_fn()
        store = _open_store(create=(action not in _READ_ONLY_ACTIONS))
        try:
            if action == 'ring_owner':
                eff_tz = _effective_tz(store.directory)
                call = store.ring_owner(
                    reason=str(args.get('reason') or ''),
                    context_brief=str(args.get('context_brief') or ''),
                    urgent=bool(args.get('urgent', False)),
                    fallback_timezone=eff_tz,
                    now=now,
                    env_override=env_override,
                    dispatch_fn=dispatch_fn,
                )
                return reply(ok=True, call=call)

            if action == 'draft_call':
                opening_raw = args.get('ai_disclosure_opening')
                opening = str(opening_raw) if isinstance(opening_raw, str) and opening_raw.strip() else None
                plan = store.draft_plan(
                    to_number=str(args.get('to_number') or ''),
                    recipient_label=str(args.get('recipient_label') or ''),
                    objective=str(args.get('objective') or ''),
                    ai_disclosure_opening=opening,
                    context_notes=str(args.get('context_notes') or ''),
                    allowed_dtmf=str(args.get('allowed_dtmf') or ''),
                    voicemail_policy=str(args.get('voicemail_policy') or 'hangup'),
                    voicemail_message=str(args.get('voicemail_message') or ''),
                    now=now,
                )
                return reply(
                    ok=True,
                    plan=plan,
                    approval_instructions=(
                        f"Outbound call plan {plan['plan_id']} to {plan['recipient_label']} ({plan['masked_number']}) "
                        'is saved as pending_approval. Ask the owner to review and approve it in Butler Control Center '
                        f"or run `python -m butler.phone approve {plan['plan_id']}` before calling place_call."
                    ),
                )

            if action == 'place_call':
                call = store.place_call(
                    str(args.get('plan_id') or ''),
                    now=now,
                    env_override=env_override,
                    dispatch_fn=dispatch_fn,
                )
                return reply(ok=True, call=call)

            if action == 'call_status':
                call_id = str(args.get('call_id') or '').strip()
                plan_id = str(args.get('plan_id') or '').strip()
                inc_tx = bool(args.get('include_transcript', True))
                if call_id:
                    call = store.get_call(call_id, include_transcript=inc_tx)
                    if call is None:
                        return reply(ok=False, error='call_not_found', detail=f'Call {call_id} not found')
                    linked_plan = store.get_plan(str(call['plan_id'])) if call.get('plan_id') else None
                    return reply(ok=True, call=call, plan=linked_plan)
                if plan_id:
                    plan = store.get_plan(plan_id)
                    if plan is None:
                        return reply(ok=False, error='plan_not_found', detail=f'Call plan {plan_id} not found')
                    last_call = (
                        store.get_call(str(plan['last_call_id']), include_transcript=inc_tx)
                        if plan.get('last_call_id') else None
                    )
                    return reply(ok=True, plan=plan, last_call=last_call)
                eff_tz = _effective_tz(store.directory)
                summary = store.status_summary(now=now, env_override=env_override)
                ring_policy = Policy(store.directory).evaluate_owner_ring(
                    now, urgent=False, fallback_timezone=eff_tz
                )
                return reply(ok=True, status=summary, owner_ring_policy=ring_policy)

            if action == 'list_calls':
                raw_limit = args.get('limit', 20)
                limit = int(raw_limit) if isinstance(raw_limit, (int, str)) and str(raw_limit).lstrip('-').isdigit() else 20
                inc_tx = bool(args.get('include_transcript', False))
                plan_status_raw = args.get('plan_status')
                plan_status = str(plan_status_raw).strip() if isinstance(plan_status_raw, str) and plan_status_raw.strip() else None
                return reply(
                    ok=True,
                    calls=store.list_calls(limit=limit, include_transcript=inc_tx),
                    plans=store.list_plans(status=plan_status, limit=limit),
                )

            if action == 'cancel_call':
                call_id = str(args.get('call_id') or '').strip()
                plan_id = str(args.get('plan_id') or '').strip()
                reason = str(args.get('reason') or '').strip()
                if call_id:
                    call = store.cancel_call(call_id, reason=reason, now=now)
                    return reply(ok=True, call=call)
                if plan_id:
                    plan = store.reject_plan(plan_id, reason=reason or 'Cancelled before dial', now=now)
                    return reply(ok=True, plan=plan)
                return reply(ok=False, error='missing_identifier', detail='Provide call_id or plan_id to cancel')

            if action == 'stop_escalation':
                reason = str(args.get('reason') or 'Stopped on owner request').strip()
                stopped = store.set_phone_stopped(True, reason=reason, now=now)
                return reply(
                    ok=True,
                    **stopped,
                    note='Phone kill-switch engaged. Only the owner can resume phone calls in Butler Control Center or via `python -m butler.phone resume`.',
                )

            return reply(ok=False, error='unsupported_action', detail=f'Unsupported phone action: {action!r}')
        except PhoneError as exc:
            return reply(ok=False, error=exc.code, detail=exc.detail)
        except ValueError as exc:
            return reply(ok=False, error='invalid_argument', detail=str(exc))

    ctx.register_tool(
        name='butler_phone',
        toolset='butler',
        schema=_PHONE_SCHEMA,
        handler=handle,
        description=_PHONE_SCHEMA['description'],
    )
