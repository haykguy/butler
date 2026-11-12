"""Live simulation: REAL Butler code + REAL model (Codex CLI) + scripted user + virtual clock.
usage: PYTHONPATH=<repo> python sim.py <scenario>
"""
import json, os, re, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import butler.tools as btools
import butler.awareness as bawareness
from butler.awareness import AwarenessHooks
from butler.runner import Runner
from butler.settings import defaults, save
from butler.followup import VISIBLE_WAKE_MARKER, WAKE_MARKER
from butler.store import ResponsibilityStore
from butler.tools import register_tools

CHI = ZoneInfo('America/Chicago')
START = datetime(2026, 11, 11, 10, 0, tzinfo=CHI).astimezone(timezone.utc)
ROUTE = {'route_id': 'sim-route', 'kind': 'session_key', 'session_key': 'imessage:hayk', 'origin': None, 'platform': 'imessage'}

SCENARIOS = {
    'deadline_quiet_then_reply': dict(
        first='I have to send the Acme invoice before 5pm tomorrow. Keep me honest on it.',
        replies={3: 'ugh sorry, was slammed all day. sending it in 20 min'},
        completion_reply='done, sent it', completion_after_eta_minutes=25,
        settings=dict(maxUnanswered=5)),
    'busy_backoff': dict(
        first='Remind me to call the dentist sometime this week to reschedule my cleaning.',
        replies={1: 'not now, in a meeting. ask me later, but make that the last nudge today'},
        settings=dict(maxUnanswered=5)),
    'nothing_open': dict(
        first="thanks, that's all for today! have a good one",
        replies={}, settings=dict(maxUnanswered=5)),
    'ignored_hits_gate': dict(
        first="I need to finish my tax docs this week. Push me hard on it, I mean it.",
        replies={}, settings=dict(maxUnanswered=3)),
}


SIM_ROOT = Path(os.environ.get('HERMES_SIM_DIR', Path.home() / '.hermes/cache/scratch/butler-sim'))
SIM_ROOT.mkdir(parents=True, exist_ok=True)

def llm(prompt):
    r = subprocess.run(['codex', 'exec', '--ephemeral', '--skip-git-repo-check',
                        '--sandbox', 'read-only', '--model', os.environ.get('BUTLER_SIM_MODEL', 'gpt-6.1-sol'), '-'], input=prompt,
                       capture_output=True, text=True, cwd=SIM_ROOT, timeout=300)
    out = r.stdout.strip()
    m = re.search(r'\{.*\}', out, re.S)
    if not m:
        return {'tool_calls': [], 'reply': '[SILENT]', '_raw': out[:300], '_err': r.stderr[-200:]}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {'tool_calls': [], 'reply': '[SILENT]', '_raw': out[:300]}


class Ctx:
    def __init__(self): self.tools, self.calls = {}, []
    def register_tool(self, **kw): self.tools[kw['name']] = kw
    def inject_message(self, text, **kw): self.calls.append(text); return True


def main(name):
    sc = SCENARIOS[name]
    tmp = Path(tempfile.mkdtemp(prefix=f'sim-{name}-', dir=SIM_ROOT))
    clock = [START]
    now = lambda: clock[0]
    bt = lambda: tmp / 'responsibilities.sqlite3'
    btools.capture_bound_turn_route = lambda: dict(ROUTE)
    bawareness.capture_bound_turn_route = lambda: dict(ROUTE)
    save(tmp, dict(defaults(), notificationsEnabled=True, timezone='America/Chicago', quietEnabled=True,
                   quietStart='22:00', quietEnd='07:00', minWakeGapMinutes=1, **sc['settings']))
    sf = lambda: ResponsibilityStore(bt())
    reg = Ctx()
    register_tools(reg, sf, directory=tmp, now_fn=now)
    hooks = AwarenessHooks(directory=lambda: tmp, load_state=lambda: {}, now_fn=now, system_tz_fn=lambda: 'America/Chicago')
    host = Ctx()
    runner = Runner(host, sf, directory=tmp, now_fn=now)

    transcript, log, events = [], [], []
    turn_counter = [0]
    nudges, pending_reply = 0, None
    def local(t): return t.astimezone(CHI).strftime('%a %m-%d %H:%M')
    tool_help = '\n'.join(f"- {n}: {json.dumps(t['schema'])}" for n, t in reg.tools.items() if n in ('butler_create', 'butler_manage'))

    def model_turn(user_message, label):
        turn_counter[0] += 1
        turn_id = f'sim-turn-{turn_counter[0]}'
        ctxt = (hooks.pre_llm_call(user_message=user_message, conversation_history=[], is_first_turn=False,
                                   platform='imessage', session_id='sim-session', turn_id=turn_id) or {}).get('context', '')
        if user_message == VISIBLE_WAKE_MARKER:
            assert WAKE_MARKER in ctxt and 'YOU OWN THE FOLLOW-UP' in ctxt, 'visible wake did not receive its route-bound hidden context'
        convo = '\n'.join(transcript[-30:]) or '(no earlier messages)'
        extra = ''
        for rnd in range(4):
            prompt = f"""You are Butler, Hayk's proactive personal assistant, texting him over iMessage. You act through tools and your reply text is sent to him as an iMessage.
TOOLS (call by name with JSON arguments):
{tool_help}
Respond with ONLY one JSON object, no prose: {{"tool_calls":[{{"name":"butler_create|butler_manage","arguments":{{...}}}}],"reply":"text to send, or [SILENT] to send nothing"}}.
If you include tool_calls you will be shown the results and then asked again; when you are finished, return tool_calls as [] with your final reply.
Timestamps you pass to tools must be timezone-aware ISO 8601 (the local timezone is America/Chicago).

=== LIVE CONTEXT FROM BUTLER ===
{ctxt}

=== CONVERSATION SO FAR (local times) ===
{convo}

=== NEW INPUT ({local(now())}) ===
{user_message}
{extra}"""
            out = llm(prompt)
            calls = out.get('tool_calls') or []
            log.append({'t': local(now()), 'turn': label, 'round': rnd, 'tool_calls': calls, 'reply': out.get('reply'), **({'raw': out['_raw']} if '_raw' in out else {})})
            if not calls:
                reply = out.get('reply') or '[SILENT]'
                hooks.post_llm_call(user_message=user_message, session_id='sim-session', turn_id=turn_id, assistant_response=reply)
                return reply
            results = []
            for c in calls:
                h = reg.tools.get(c.get('name'))
                res = h['handler'](c.get('arguments') or {}) if h else '{"error":"unknown tool"}'
                results.append({'call': c, 'result': json.loads(res) if res.startswith('{') else res})
                log.append({'t': local(now()), 'tool_result': results[-1]})
            extra = '\nTOOL RESULTS:\n' + json.dumps(results, default=str)[:3000] + '\nNow give your final JSON (more tool_calls only if still needed).'
        return '[SILENT]'

    def say(reply, who='BUTLER'):
        events.append({'at': now(), 'who': who, 'text': reply or '[SILENT]'})
        if reply and reply.strip() != '[SILENT]':
            transcript.append(f'[{local(now())}] {who}: {reply.strip()}')
            return True
        transcript.append(f'[{local(now())}] ({who} stayed silent)')
        return False

    def user_says(text):
        nonlocal pending_reply
        events.append({'at': now(), 'who': 'HAYK', 'text': text})
        transcript.append(f'[{local(now())}] HAYK: {text}')
        say(model_turn(text, 'user_msg'))
        if name == 'deadline_quiet_then_reply' and 'sending it in 20 min' in text.lower():
            pending_reply = (clock[0] + timedelta(minutes=sc['completion_after_eta_minutes']), sc['completion_reply'])

    user_says(sc['first'])
    end = START + timedelta(days=3)
    idle = 0
    while clock[0] < end and nudges < 10:
        items = [i for i in sf().list() if i['status'] == 'active']
        times = [i['due_at'] for i in items]
        if pending_reply:
            times.append(pending_reply[0])
        if not times:
            break
        t = max(min(times), clock[0])
        if t > end:
            break
        clock[0] = t
        if pending_reply and pending_reply[0] <= t:
            _, text = pending_reply; pending_reply = None
            user_says(text); continue
        res = runner.tick()
        if res.get('injected'):
            idle = 0
            prompt = host.calls[-1]
            assert prompt == VISIBLE_WAKE_MARKER, f'runner exposed unexpected visible wake text: {prompt!r}'
            sent = say(model_turn(prompt, 'wake'), 'BUTLER(wake)')
            if sent:
                nudges += 1
                if nudges in sc['replies']:
                    pending_reply = (clock[0] + timedelta(minutes=10), sc['replies'][nudges])
        else:
            log.append({'t': local(now()), 'tick_held': res.get('policy_reason'), 'claimed': res.get('claimed')})
            idle += 1
            clock[0] += timedelta(minutes=30)
            if res.get('policy_reason') == 'max_unanswered' or idle > 60:
                break
    s = sf(); eng = s.get_engagement()
    final = [(i['title'], i['status'], local(i['due_at'])) for i in s.list()]
    (SIM_ROOT / f'out_{name}.json').write_text(json.dumps(
        {'scenario': name, 'transcript': transcript, 'events': events, 'log': log, 'final_items': final},
        indent=1, default=str,
    ))
    checks = []
    visible_wakes = [e for e in events if e['who'] == 'BUTLER(wake)' and e['text'].strip() != '[SILENT]']
    if name == 'deadline_quiet_then_reply':
        anchor = next((e for e in events if e['who'] == 'HAYK' and 'sending it in 20 min' in e['text'].lower()), None)
        assert anchor is not None, 'scripted ETA reply was not reached'
        deadline_local = datetime.combine(START.astimezone(CHI).date() + timedelta(days=1), datetime.min.time(), tzinfo=CHI).replace(hour=17)
        done_reply = next((e for e in events if e['who'] == 'HAYK' and e['at'] > anchor['at'] and 'done' in e['text'].lower()), None)
        assert done_reply is not None, 'scripted completion reply was not reached'
        visible_after_eta = [e for e in visible_wakes if anchor['at'] < e['at'] < done_reply['at']]
        before_deadline = [e for e in visible_after_eta if e['at'] < deadline_local.astimezone(timezone.utc)]
        assert len(visible_after_eta) <= 1, f'more than one visible follow-up after the promised ETA before the user replied: {visible_after_eta}'
        assert all(e['at'] >= anchor['at'] + timedelta(minutes=20) for e in visible_after_eta), 'visible wake occurred before the promised 20-minute ETA'
        assert len(before_deadline) <= 1, f'more than one post-ETA wake before deadline: {before_deadline}'
        assert len(s.list()) == 1, f'duplicate responsibilities were created for the invoice: {s.list()}'
        checks.append('no pre-ETA wake; at most one post-ETA wake until user reply; one responsibility per task')
    elif name == 'busy_backoff':
        anchor = next((e for e in events if e['who'] == 'HAYK' and 'last nudge today' in e['text'].lower()), None)
        assert anchor is not None, 'scripted last-nudge boundary was not reached'
        same_day_after = [e for e in events if e['who'] == 'BUTLER(wake)' and anchor['at'] < e['at'] and e['at'].astimezone(CHI).date() == anchor['at'].astimezone(CHI).date()]
        assert len(same_day_after) <= 1, f'more than one wake after the last-nudge boundary: {same_day_after}'
        sunday = START.astimezone(CHI).date() + timedelta(days=6 - START.astimezone(CHI).weekday())
        week_end = datetime.combine(sunday, datetime.max.time(), tzinfo=CHI).astimezone(timezone.utc)
        late_wakes = [e for e in events if e['who'] == 'BUTLER(wake)' and e['at'] > week_end]
        scheduled_past_window = []
        for item in s.list() or []:
            if not isinstance(item, dict):
                continue
            due = item.get('due_at')
            if item.get('status') == 'active' and isinstance(due, datetime) and due > week_end:
                scheduled_past_window.append(item)
        assert not late_wakes and not scheduled_past_window, f'follow-up escaped the user-stated week: wakes={late_wakes}, items={scheduled_past_window}'
        checks.append('respected last-nudge-today and this-week boundaries')
    elif name == 'ignored_hits_gate':
        assert nudges == 3 and eng['unanswered_wakes'] == 3, f'unanswered gate accounting mismatch: nudges={nudges}, engagement={eng}'
        assert any(row.get('tick_held') == 'max_unanswered' for row in log), 'runner did not hold the next wake at max_unanswered'
        checks.append('visible unanswered wakes reached the configured gate; runner held the next wake')
    elif name == 'nothing_open':
        assert nudges == 0, f'empty responsibility list produced {nudges} proactive wakes'
        checks.append('no wake with no open responsibility')
    out = {'scenario': name, 'nudges_sent': nudges, 'final_items': final, 'unanswered': eng['unanswered_wakes'],
           'virtual_end': local(clock[0]), 'assertions': checks}
    (SIM_ROOT / f'out_{name}.json').write_text(json.dumps({'summary': out, 'transcript': transcript, 'events': events, 'log': log}, indent=1, default=str))
    print(json.dumps(out, indent=1))
    print('\n'.join(transcript))


if __name__ == '__main__':
    main(sys.argv[1])
