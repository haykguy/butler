from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import io
import json
from urllib.error import HTTPError

import pytest

from butler.phone import (
    PLAN_TTL_SECONDS,
    PhoneConfigError,
    PhoneDispatchError,
    PhoneSafetyError,
    PhoneStore,
     _cli_main,
    build_ai_disclosure_opening,
    check_number_allowed,
    compute_plan_sha256,
    create_sip_participant,
    livekit_http_base,
    mask_phone,
    mint_livekit_jwt,
    twirp_post,
)
from butler.settings import defaults as settings_defaults, save as save_settings
from butler.voice_worker import (
    DtmfController,
    HoldMonitor,
    InboundCallController,
    OutboundCallController,
    OwnerRingController,
    detect_answering_machine,
    is_verbal_stop_request,
)

UTC = timezone.utc
FAKE_ENV = {
    'LIVEKIT_URL': 'wss://butler-test.livekit.cloud',
    'LIVEKIT_API_KEY': 'APIkeyTest123',
    'LIVEKIT_API_SECRET': 'SecretTestValue45678901234567890',
    'LIVEKIT_SIP_TRUNK_ID': 'ST_testTrunk99',
    'LIVEKIT_CALLER_NUMBER': '+14155550100',
}


def _enable_phone(tmp_path, **overrides):
    cfg = settings_defaults()
    cfg.update({
        'timezone': 'America/Chicago',
        'phoneEnabled': True,
        'ownerRingEnabled': True,
        'ownerPhone': '+14155550199',
        'quietEnabled': False,
        'quietStart': '',
        'quietEnd': '',
        'quietHoursBreakthrough': False,
        'maxRingsPerHour': 3,
        'minRingGapSeconds': 120,
        'inboundScreeningEnabled': True,
        'livekitAgentName': 'butler-voice',
        'callerName': 'Butler',
    })
    cfg.update(overrides)
    save_settings(tmp_path, cfg)


def test_number_normalization_masking_and_blocklists():
    assert check_number_allowed('(415) 555-2671') == '+14155552671'
    assert check_number_allowed('+44 20 7946 0958') == '+442079460958'
    assert mask_phone('+14155552671') == '+1******2671'

    for blocked in ('911', '999', '112', '988', '119', '000', '+19005551212', '+19765551212', '+449098790000'):
        with pytest.raises(PhoneSafetyError):
            check_number_allowed(blocked)


def test_ai_disclosure_first_sentence_enforced():
    default_open = build_ai_disclosure_opening(None, caller_name='Butler')
    assert 'automated AI assistant' in default_open

    with pytest.raises(PhoneSafetyError) as exc:
        build_ai_disclosure_opening('Hi, I am calling from the office. By the way I am an AI.')
    assert exc.value.code == 'ai_disclosure_required'


def test_mint_jwt_and_twirp_post_stdlib():
    token = mint_livekit_jwt(
        'API123',
        'secret456',
        sub='butler-dispatcher',
        video_grants={'roomCreate': True, 'room': 'rm_1'},
        sip_grants={'call': True},
        now_ts=1700000000,
    )
    h_b64, p_b64, s_b64 = token.split('.')
    expected_sig = base64.urlsafe_b64encode(
        hmac.new(b'secret456', f'{h_b64}.{p_b64}'.encode(), hashlib.sha256).digest()
    ).rstrip(b'=').decode()
    assert s_b64 == expected_sig
    assert livekit_http_base('wss://example.livekit.cloud') == 'https://example.livekit.cloud'

    captured = {}

    class DummyResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"id": "AD_123"}'

    def fake_opener(req, timeout=15.0):
        captured['url'] = req.full_url
        captured['auth'] = req.headers.get('Authorization')
        captured['body'] = json.loads(req.data.decode('utf-8'))
        return DummyResp()

    out = twirp_post(
        'wss://example.livekit.cloud',
        'API123',
        'secret456',
        'AgentDispatchService',
        'CreateDispatch',
        {'agent_name': 'butler-voice', 'room': 'rm_1'},
        room_name='rm_1',
        opener=fake_opener,
    )
    assert out == {'id': 'AD_123'}
    assert captured['url'] == 'https://example.livekit.cloud/twirp/livekit.AgentDispatchService/CreateDispatch'
    assert captured['auth'].startswith('Bearer ')


def test_double_gated_outbound_flow_sha256_ttl_and_active_lock(tmp_path):
    _enable_phone(tmp_path)
    store = PhoneStore(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)  # CLI approval uses the real clock; a fixed date expires after 24h

    # Read-only status creates zero SQLite files on disk.
    snap = store.status_summary(now=now, env_override=FAKE_ENV)
    assert snap['credentials_ready'] is True
    assert not store.db_path.exists()

    plan = store.draft_plan(
        to_number='+14155552671',
        recipient_label='Downtown Dental',
        objective='Reschedule Thursday cleaning to next Tuesday afternoon',
        allowed_dtmf='12#w',
        voicemail_policy='leave_message',
        voicemail_message='Please call us back to reschedule Thursday afternoon.',
        now=now,
    )
    assert plan['status'] == 'pending_approval'
    assert plan['masked_number'] == '+1******2671'
    assert 'to_e164' not in plan

    # Gate 1 blocks unapproved plans.
    with pytest.raises(PhoneSafetyError) as unapproved_exc:
        store.place_call(plan['plan_id'], now=now, env_override=FAKE_ENV, dispatch_fn=lambda **kw: {'id': 'd1'})
    assert unapproved_exc.value.code == 'approval_required'

    # Human approves via CLI.
    rc = _cli_main(['--data-dir', str(tmp_path), 'approve', plan['plan_id'], '--sha256', plan['script_sha256']])
    assert rc == 0

    # TTL expiry check (> 24h after CLI approval timestamp).
    approved_plan = store.get_plan(plan['plan_id'])
    assert approved_plan is not None and approved_plan['approval_expires_at']
    expired_now = datetime.fromisoformat(approved_plan['approval_expires_at']) + timedelta(seconds=10)
    with pytest.raises(PhoneSafetyError) as ttl_exc:
        store.place_call(plan['plan_id'], now=expired_now, env_override=FAKE_ENV, dispatch_fn=lambda **kw: {'id': 'd1'})
    assert ttl_exc.value.code == 'approval_expired'

    # Fresh plan -> approve -> dispatch -> Gate 2 worker verification -> active call lock.
    plan2 = store.draft_plan(
        to_number='+14155552671',
        recipient_label='Downtown Dental',
        objective='Confirm cleaning time',
        allowed_dtmf='12',
        now=now,
    )
    store.approve_plan(plan2['plan_id'], approved_by='human_ui', now=now)
    dispatched = {}

    def fake_dispatch(**kwargs):
        dispatched.update(kwargs)
        return {'id': 'AD_out_1'}

    call = store.place_call(
        plan2['plan_id'], now=now + timedelta(seconds=5), env_override=FAKE_ENV, dispatch_fn=fake_dispatch
    )
    assert call['status'] == 'dispatching'
    assert dispatched['metadata']['plan_id'] == plan2['plan_id']

    # Single active call lock blocks a second concurrent call or owner ring.
    plan3 = store.draft_plan(
        to_number='+14155552672',
        recipient_label='Pharmacy',
        objective='Check prescription status',
        now=now,
    )
    store.approve_plan(plan3['plan_id'], approved_by='human_ui', now=now)
    with pytest.raises(PhoneSafetyError) as lock_exc:
        store.place_call(plan3['plan_id'], now=now + timedelta(seconds=10), env_override=FAKE_ENV, dispatch_fn=fake_dispatch)
    assert lock_exc.value.code == 'call_already_active'

    # Gate 2 inside OutboundCallController rejects tampered SHA-256 before dialing SIP.
    bad_worker = OutboundCallController(
        store,
        plan_id=plan2['plan_id'],
        call_id=call['call_id'],
        expected_sha256='0' * 64,
        room_name=call['room_name'],
    )
    sip_called = False

    def fake_sip(**kwargs):
        nonlocal sip_called
        sip_called = True
        return {'participant_id': 'PA_1'}

    with pytest.raises(PhoneSafetyError) as sha_exc:
        bad_worker.verify_and_dial_sip(creds=FAKE_ENV, sip_fn=fake_sip)
    assert sha_exc.value.code == 'script_sha256_mismatch'
    assert sip_called is False

    # Valid Gate 2 succeeds and enforces AMD + HoldMonitor + DTMF bounds.
    good_worker = OutboundCallController(
        store,
        plan_id=plan2['plan_id'],
        call_id=call['call_id'],
        expected_sha256=plan2['script_sha256'],
        room_name=call['room_name'],
    )
    good_worker.verify_and_dial_sip(creds=FAKE_ENV, sip_fn=fake_sip)
    assert sip_called is True
    assert 'automated AI assistant' in good_worker.opening_disclosure()

    # HoldMonitor suppresses LLM replies on hold and resumes when human returns.
    h1 = good_worker.observe_remote_utterance('Your call is very important to us, please hold.')
    assert h1 == {'action': 'suppress_llm', 'on_hold': True}
    h2 = good_worker.observe_remote_utterance('Estimated wait time is two minutes.')
    assert h2 == {'action': 'suppress_llm', 'on_hold': True}
    h3 = good_worker.observe_remote_utterance('Hello, thanks for holding, how can I help?')
    assert h3 == {'action': 'resume_llm', 'on_hold': False}

    finished = good_worker.complete('Confirmed Tuesday at 2pm.')
    assert finished['status'] == 'completed'


@pytest.mark.anyio
async def test_dtmf_controller_bounds():
    ctrl = DtmfController('12#w', max_digits=4)
    sent = []
    out = await ctrl.send('1w2#', lambda code, digit: sent.append((code, digit)), pause_seconds=0, inter_digit_seconds=0)
    assert out == '12#'
    assert sent == [(1, '1'), (2, '2'), (11, '#')]

    with pytest.raises(PhoneSafetyError) as bad_digit:
        ctrl.validate_sequence('9')
    assert bad_digit.value.code == 'dtmf_digit_not_allowed'

    with pytest.raises(PhoneSafetyError) as cap_exc:
        ctrl.validate_sequence('12')
    assert cap_exc.value.code == 'dtmf_cap_exceeded'


def test_owner_ring_gates_quiet_hours_and_verbal_kill_switch(tmp_path):
    _enable_phone(
        tmp_path,
        quietEnabled=True,
        quietStart='22:00',
        quietEnd='07:00',
        quietHoursBreakthrough=False,
        maxRingsPerHour=3,
        minRingGapSeconds=120,
    )
    store = PhoneStore(tmp_path)
    # 23:00 America/Chicago == 05:00 UTC next day (inside quiet hours).
    quiet_now = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)
    with pytest.raises(PhoneSafetyError) as q_exc:
        store.ring_owner(
            reason='Flight gate changed',
            urgent=True,
            now=quiet_now,
            env_override=FAKE_ENV,
            dispatch_fn=lambda **kw: {'id': 'd_q'},
        )
    assert q_exc.value.code == 'quiet_hours'

    # Enable breakthrough: urgent=True passes, urgent=False still blocked.
    _enable_phone(
        tmp_path,
        quietEnabled=True,
        quietStart='22:00',
        quietEnd='07:00',
        quietHoursBreakthrough=True,
    )
    with pytest.raises(PhoneSafetyError):
        store.ring_owner(
            reason='Non-urgent note',
            urgent=False,
            now=quiet_now,
            env_override=FAKE_ENV,
            dispatch_fn=lambda **kw: {'id': 'd_q2'},
        )

    day_now = datetime(2026, 11, 11, 18, 0, tzinfo=UTC)
    r1 = store.ring_owner(
        reason='Contractor at front gate',
        urgent=True,
        now=day_now,
        env_override=FAKE_ENV,
        dispatch_fn=lambda **kw: {'id': 'd1'},
    )
    store.save_call_result(r1['call_id'], status='no_answer', now=day_now + timedelta(seconds=20))

    # 120s cooldown gate.
    with pytest.raises(PhoneSafetyError) as gap_exc:
        store.ring_owner(
            reason='Retry too soon',
            now=day_now + timedelta(seconds=60),
            env_override=FAKE_ENV,
            dispatch_fn=lambda **kw: {'id': 'd2'},
        )
    assert gap_exc.value.code == 'ring_cooldown_active'

    # Rings 2 and 3 succeed after 120s gaps; ring 4 hits 3/hr cap.
    r2 = store.ring_owner(
        reason='Second attempt',
        now=day_now + timedelta(seconds=130),
        env_override=FAKE_ENV,
        dispatch_fn=lambda **kw: {'id': 'd2'},
    )
    store.save_call_result(r2['call_id'], status='no_answer', now=day_now + timedelta(seconds=150))

    r3 = store.ring_owner(
        reason='Third attempt',
        now=day_now + timedelta(seconds=260),
        env_override=FAKE_ENV,
        dispatch_fn=lambda **kw: {'id': 'd3'},
    )

    # Verbal 'stop calling me' latches phone_stopped in SQLite.
    ring_ctrl = OwnerRingController(
        store,
        call_id=r3['call_id'],
        room_name=r3['room_name'],
        opening_reason='Third attempt',
    )
    assert is_verbal_stop_request('Please stop calling me right now')
    stop_reply = ring_ctrl.handle_owner_utterance('Please stop calling me right now')
    assert 'kill-switch' in stop_reply
    assert store.is_phone_stopped() is True

    # Subsequent ring_owner is hard-blocked by phone_stopped even hours later.
    with pytest.raises(PhoneSafetyError) as stop_exc:
        store.ring_owner(
            reason='Later alert',
            now=day_now + timedelta(hours=3),
            env_override=FAKE_ENV,
            dispatch_fn=lambda **kw: {'id': 'd4'},
        )
    assert stop_exc.value.code == 'phone_stopped_kill_switch'


def test_inbound_screening_links_open_plans_and_parks_clean_wake(tmp_path):
    _enable_phone(tmp_path)
    store = PhoneStore(tmp_path)
    now = datetime(2026, 11, 11, 19, 0, tzinfo=UTC)
    plan = store.draft_plan(
        to_number='+14155558899',
        recipient_label='City Permit Desk',
        objective='Check solar permit approval',
        now=now,
    )
    inbound = InboundCallController(
        store,
        caller_number='+1 (415) 555-8899',
        room_name='sip-inbound-1',
    )
    assert len(inbound.matched_plans) == 1
    assert inbound.matched_plans[0]['plan_id'] == plan['plan_id']
    greeting = inbound.opening_greeting()
    assert 'City Permit Desk' in greeting

    res = inbound.record_message_and_end('Permit Officer Lee', 'Solar permit is approved for pickup Friday.', urgent=True)
    assert res['status'] == 'completed'

    # Verify post-call wake is enqueued in ResponsibilityStore hidden wake ledger
    # (leaving visible chat turns clean via VISIBLE_WAKE_MARKER).
    from butler.followup import VISIBLE_WAKE_MARKER
    from butler.store import ResponsibilityStore
    assert 'plan_' not in VISIBLE_WAKE_MARKER
    rstore = ResponsibilityStore(tmp_path / 'responsibilities.sqlite3')
    try:
        claimed = rstore.claim_pending_wake('phone:post_call', datetime.now(UTC))
    finally:
        rstore.close()
    assert claimed is not None and 'Solar permit is approved' in claimed['payload']


def test_amd_detection():
    assert detect_answering_machine('Hi, you have reached voicemail. Please leave a message after the beep.')
    assert not detect_answering_machine('Hello, this is Downtown Dental, how may I help you?')
