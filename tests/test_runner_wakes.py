import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from butler.awareness import AwarenessHooks, register_awareness_and_controls
from butler.event_hooks import register_event_hooks
from butler.runner import Runner, get_active_runner
from butler.settings import defaults, save
from butler.store import ResponsibilityStore
from butler.tools import register_tools


class FakeHostContext:
    def __init__(self, accepted=True, opt_in=False):
        self.tools = {}
        self.hooks = {}
        self.calls = []
        self.accepted = accepted
        self.opt_in = opt_in
        self.cleanup = None

    def register_tool(self, **kw):
        self.tools[kw['name']] = kw['handler']

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def on_unload(self, callback):
        self.cleanup = callback

    def get_config(self, key, default=None):
        if key == 'collect_event_history':
            return self.opt_in
        return default

    def inject_message(self, text, **kw):
        if isinstance(self.accepted, Exception):
            raise self.accepted
        self.calls.append((text, kw))
        return bool(self.accepted)


class InMemoryRouteRegistry:
    def __init__(self):
        self.routes = {}
        self.preferred_id = None

    def record_route(self, route, preferred=True):
        rid = route['route_id']
        self.routes[rid] = dict(route)
        if preferred or self.preferred_id is None:
            self.preferred_id = rid
        return rid

    def get_route(self, route_id):
        return self.routes.get(route_id)

    def preferred_route(self):
        return self.routes.get(self.preferred_id) if self.preferred_id else None

    def list_routes(self):
        return list(self.routes.values())

    def close(self):
        return None


class UpgradedStoreAdapter:
    """Wraps ResponsibilityStore so tests run identically whether store.py
    already has Workstream 1 methods or is still on baseline schema."""

    def __init__(self, base: ResponsibilityStore, state: dict):
        self._base = base
        self._state = state
        self.db = base.db

    def _enrich(self, item):
        if item is None:
            return None
        out = dict(item)
        extra = self._state['extras'].get(item['id'], {})
        for k, v in extra.items():
            out.setdefault(k, v)
            if k in ('kind', 'route_id', 'deadline_at', 'followup_stage', 'completed_day') and out.get(k) is None:
                out[k] = v
        return out

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
        try:
            item = self._base.create(
                title,
                due_at,
                timezone_name=timezone_name,
                interval_seconds=interval_seconds,
                metadata=metadata,
                id=id,
                kind=kind,
                route_id=route_id,
                deadline_at=deadline_at,
            )
        except TypeError:
            item = self._base.create(
                title,
                due_at,
                timezone_name=timezone_name,
                interval_seconds=interval_seconds,
                metadata=metadata,
                id=id,
            )
            self._state['extras'][item['id']] = {
                'kind': kind,
                'route_id': route_id,
                'deadline_at': deadline_at,
                'followup_stage': -1,
                'completed_day': None,
            }
        return self._enrich(item)

    def get(self, item_id):
        return self._enrich(self._base.get(item_id))

    def list(self):
        return [self._enrich(x) for x in self._base.list()]

    def pause(self, item_id):
        return self._base.pause(item_id)

    def resume(self, item_id):
        return self._base.resume(item_id)

    def stop(self, item_id):
        return self._base.stop(item_id)

    def complete(self, item_id):
        return self._base.complete(item_id)

    def snooze(self, item_id, until):
        return self._base.snooze(item_id, until)

    def claim_due(self, now, *, limit=10, lease_seconds=300):
        return [self._enrich(x) for x in self._base.claim_due(now, limit=limit, lease_seconds=lease_seconds)]

    def claim_is_current(self, item_id, token, now):
        return self._base.claim_is_current(item_id, token, now)

    def finish_claim(self, item_id, token, now):
        return self._base.finish_claim(item_id, token, now)

    def release_claim(self, item_id, token, now):
        return self._base.release_claim(item_id, token, now)

    def defer_claim(self, item_id, token, now, *, until):
        if hasattr(self._base, 'defer_claim'):
            return self._base.defer_claim(item_id, token, now, until=until)
        if not self._base.claim_is_current(item_id, token, now):
            return False
        return self._base.snooze(item_id, until)

    def advance_followup(self, item_id, token, now, *, next_due_at=None, stage=-1, next_status='active'):
        if hasattr(self._base, 'advance_followup'):
            return self._base.advance_followup(
                item_id, token, now, next_due_at=next_due_at, stage=stage, next_status=next_status,
            )
        if not self._base.claim_is_current(item_id, token, now):
            return False
        self._state['extras'].setdefault(item_id, {})['followup_stage'] = stage
        if next_due_at is not None and next_status == 'active':
            return self._base.snooze(item_id, next_due_at)
        self._base.db.execute(
            'UPDATE responsibilities SET status=?,claim_token=NULL,claim_until=NULL,generation=generation+1 WHERE id=?',
            (next_status, item_id),
        )
        return True

    def mark_non_negotiable_done(self, item_id, local_date_str):
        if hasattr(self._base, 'mark_non_negotiable_done'):
            return self._base.mark_non_negotiable_done(item_id, local_date_str)
        cur = self.get(item_id)
        if not cur:
            return False
        self._state['extras'].setdefault(item_id, {})['completed_day'] = local_date_str
        self._base.complete(item_id)
        return True

    def reset_daily_non_negotiables(self, local_date_str, morning_due_at):
        if hasattr(self._base, 'reset_daily_non_negotiables'):
            return self._base.reset_daily_non_negotiables(local_date_str, morning_due_at)
        count = 0
        for item in self.list():
            if item.get('kind') == 'non_negotiable' and item.get('status') == 'completed':
                if item.get('completed_day') != local_date_str:
                    self._base.db.execute(
                        "UPDATE responsibilities SET status='active',due_at=?,claim_token=NULL,claim_until=NULL WHERE id=?",
                        (morning_due_at.astimezone(timezone.utc).isoformat(timespec='microseconds'), item['id']),
                    )
                    self._state['extras'].setdefault(item['id'], {})['followup_stage'] = -1
                    count += 1
        return count

    def reopen(self, item_id, *, due_at=None):
        if hasattr(self._base, 'reopen'):
            return self._base.reopen(item_id, due_at=due_at)
        cur = self._base.get(item_id)
        if not cur:
            return False
        due_val = (due_at or cur['due_at']).astimezone(timezone.utc).isoformat(timespec='microseconds')
        self._base.db.execute(
            "UPDATE responsibilities SET status='active',due_at=?,claim_token=NULL,claim_until=NULL WHERE id=?",
            (due_val, item_id),
        )
        return True

    def delete(self, item_id):
        if hasattr(self._base, 'delete'):
            return self._base.delete(item_id)
        res = self._base.db.execute('DELETE FROM responsibilities WHERE id=?', (item_id,))
        self._state['extras'].pop(item_id, None)
        return res.rowcount == 1

    def update_item(self, item_id, **fields):
        if hasattr(self._base, 'update_item'):
            return self._base.update_item(item_id, **fields)
        cur = self.get(item_id)
        if not cur:
            return False
        if 'title' in fields:
            self._base.db.execute('UPDATE responsibilities SET title=? WHERE id=?', (fields['title'], item_id))
        if 'due_at' in fields and fields['due_at'] is not None:
            self._base.snooze(item_id, fields['due_at'])
        if 'interval_seconds' in fields:
            self._base.db.execute(
                'UPDATE responsibilities SET interval_seconds=? WHERE id=?',
                (fields['interval_seconds'], item_id),
            )
        if 'metadata' in fields:
            self._base.db.execute(
                'UPDATE responsibilities SET metadata=? WHERE id=?',
                (json.dumps(fields['metadata']), item_id),
            )
        ext = self._state['extras'].setdefault(item_id, {})
        for k in ('kind', 'route_id', 'deadline_at'):
            if k in fields:
                ext[k] = fields[k]
        return True

    def record_wake_log(self, item_id, now, action, outcome, detail=''):
        if hasattr(self._base, 'record_wake_log'):
            self._base.record_wake_log(item_id, now, action, outcome, detail)
        self._state['logs'].append({
            'item_id': item_id,
            'at': now.isoformat(),
            'action': action,
            'outcome': outcome,
            'detail': detail,
        })

    def list_wake_logs(self, limit=30):
        if hasattr(self._base, 'list_wake_logs'):
            return self._base.list_wake_logs(limit=limit)
        return list(reversed(self._state['logs'][-limit:]))

    def close(self):
        self._base.close()

    def __getattr__(self, name):
        # forward any store method the adapter does not wrap (e.g. engagement tracking)
        return getattr(self._base, name)


def _make_harness(tmp_path):
    state = {'extras': {}, 'logs': []}
    db_path = tmp_path / 'responsibilities.sqlite3'
    routes = InMemoryRouteRegistry()

    def store_factory():
        return UpgradedStoreAdapter(ResponsibilityStore(db_path), state)

    return store_factory, lambda: routes, routes, state


def test_runner_coalesces_by_route_and_supports_session_key_and_origin(tmp_path):
    now = datetime(2026, 11, 11, 15, 0, 0, tzinfo=timezone.utc)
    save(tmp_path, dict(defaults(), notificationsEnabled=True, timezone='America/Chicago', dailyLimit=5))
    store_factory, reg_factory, routes, _ = _make_harness(tmp_path)

    routes.record_route({
        'route_id': 'r-tui',
        'kind': 'session_key',
        'session_key': 'tui:local:abc',
        'origin': None,
        'platform': 'tui',
    })
    routes.record_route({
        'route_id': 'r-tg',
        'kind': 'origin',
        'session_key': '',
        'origin': {'platform': 'telegram', 'chat_id': '777', 'user_id': '777', 'chat_type': 'dm'},
        'platform': 'telegram',
    })

    db = store_factory()
    a = db.create('Send investor deck', now - timedelta(minutes=5), route_id='r-tui', metadata={'prep_hint': 'Draft summary'})
    b = db.create('Review PR #42', now - timedelta(minutes=2), route_id='r-tui')
    c = db.create('Call dentist', now - timedelta(minutes=1), route_id='r-tg')
    unrouted = db.create('Legacy item', now - timedelta(minutes=10), metadata={'session_key': 'forged'})
    db.close()

    ctx = FakeHostContext(accepted=True)
    runner = Runner(ctx, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: now)
    res = runner.tick()

    assert res['claimed'] == 4
    assert res['skipped_unroutable'] == 1
    assert res['injected'] == 2
    assert res['woken_items'] == 3
    assert len(ctx.calls) == 2

    first_prompt, first_kw = ctx.calls[0]
    assert first_kw == {'role': 'user', 'session_key': 'tui:local:abc'}
    assert 'Send investor deck' in first_prompt and 'Review PR #42' in first_prompt

    second_prompt, second_kw = ctx.calls[1]
    assert second_kw == {
        'role': 'user',
        'origin': {'platform': 'telegram', 'chat_id': '777', 'user_id': '777', 'chat_type': 'dm'},
    }
    assert 'Call dentist' in second_prompt

    db = store_factory()
    assert db.get(unrouted['id'])['status'] == 'active'
    assert db.get(unrouted['id'])['claim_token'] is None
    logs = db.list_wake_logs()
    assert sum(1 for row in logs if row.get('outcome') == 'injected') == 3
    assert {row['item_id'] for row in logs} == {a['id'], b['id'], c['id']}
    db.close()


def test_quiet_hours_defers_claim_and_wakes_after_window(tmp_path):
    # 2026-11-11 04:00 UTC is 2026-11-10 22:00 CST (inside 22:00-07:00 quiet window)
    quiet_now = datetime(2026, 11, 11, 4, 30, 0, tzinfo=timezone.utc)
    save(
        tmp_path,
        dict(
            defaults(),
            notificationsEnabled=True,
            timezone='America/Chicago',
            quietEnabled=True,
            quietStart='22:00',
            quietEnd='07:00',
        ),
    )
    store_factory, reg_factory, routes, _ = _make_harness(tmp_path)
    routes.record_route({
        'route_id': 'r1',
        'kind': 'session_key',
        'session_key': 'desktop:1',
        'origin': None,
        'platform': 'desktop',
    })

    db = store_factory()
    item = db.create('Morning review', quiet_now - timedelta(minutes=5), route_id='r1')
    db.close()

    ctx = FakeHostContext(accepted=True)
    clock = [quiet_now]
    runner = Runner(ctx, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: clock[0])

    res_quiet = runner.tick()
    assert res_quiet['injected'] == 0
    assert res_quiet['deferred'] == 1
    assert not ctx.calls

    db = store_factory()
    deferred_item = db.get(item['id'])
    assert deferred_item['status'] == 'active'
    assert deferred_item['claim_token'] is None
    # 07:00 CST on 2026-11-11 is 13:00 UTC
    assert deferred_item['due_at'] >= datetime(2026, 11, 11, 13, 0, 0, tzinfo=timezone.utc)
    db.close()

    # Advance past quiet hours -> wakes cleanly
    clock[0] = datetime(2026, 11, 11, 13, 1, 0, tzinfo=timezone.utc)
    res_after = runner.tick()
    assert res_after['injected'] == 1
    assert len(ctx.calls) == 1


def test_failed_injection_releases_claim_and_retries_without_advancing_stage(tmp_path):
    now = datetime(2026, 11, 11, 15, 0, 0, tzinfo=timezone.utc)
    save(tmp_path, dict(defaults(), notificationsEnabled=True, timezone='UTC'))
    store_factory, reg_factory, routes, _ = _make_harness(tmp_path)
    routes.record_route({
        'route_id': 'r1',
        'kind': 'session_key',
        'session_key': 'cli:1',
        'origin': None,
        'platform': 'cli',
    })
    db = store_factory()
    item = db.create('Ship release notes', now - timedelta(seconds=10), route_id='r1')
    db.close()

    # 1) Host returns False
    ctx_false = FakeHostContext(accepted=False)
    res1 = Runner(ctx_false, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: now).tick()
    assert res1['injected'] == 0
    assert res1['failed'] == 1

    # 2) Host raises exception
    ctx_err = FakeHostContext(accepted=RuntimeError('bridge down'))
    res2 = Runner(ctx_err, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: now).tick()
    assert res2['injected'] == 0
    assert res2['failed'] == 1

    db = store_factory()
    still_due = db.get(item['id'])
    assert still_due['status'] == 'active'
    assert still_due['claim_token'] is None
    assert still_due.get('stage', -1) == -1
    assert still_due.get('nudge_count', 0) == 0
    db.close()

    # 3) Host recovers -> injects cleanly
    ctx_ok = FakeHostContext(accepted=True)
    res3 = Runner(ctx_ok, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: now).tick()
    assert res3['injected'] == 1
    assert len(ctx_ok.calls) == 1


def test_watcher_polling_establishes_baseline_and_wakes_only_on_file_change(tmp_path):
    now = datetime(2026, 11, 11, 15, 0, 0, tzinfo=timezone.utc)
    save(tmp_path, dict(defaults(), notificationsEnabled=True, timezone='UTC'))
    store_factory, reg_factory, routes, _ = _make_harness(tmp_path)
    routes.record_route({
        'route_id': 'r-watch',
        'kind': 'session_key',
        'session_key': 'desktop:watch',
        'origin': None,
        'platform': 'desktop',
    })

    watched = tmp_path / 'build_status.txt'
    watched.write_text('status=running\n', encoding='utf-8')

    db = store_factory()
    watch_item = db.create(
        'Monitor build_status.txt',
        now - timedelta(seconds=1),
        interval_seconds=60,
        kind='watch',
        route_id='r-watch',
        metadata={'watch': {'kind': 'file', 'path': str(watched)}},
    )
    db.close()

    ctx = FakeHostContext(accepted=True)
    clock = [now]
    runner = Runner(ctx, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: clock[0])

    # First check establishes baseline -> no wake
    r1 = runner.tick()
    assert r1['watches_unchanged'] == 1
    assert r1['injected'] == 0
    assert not ctx.calls

    # Second check after interval with identical content -> no wake
    clock[0] = now + timedelta(seconds=65)
    r2 = runner.tick()
    assert r2['watches_unchanged'] == 1
    assert r2['injected'] == 0
    assert not ctx.calls

    # Modify file -> third check wakes user with change details
    watched.write_text('status=failed exit_code=2\n', encoding='utf-8')
    clock[0] = now + timedelta(seconds=130)
    r3 = runner.tick()
    assert r3['injected'] == 1
    assert len(ctx.calls) == 1
    assert 'Monitor build_status.txt' in ctx.calls[0][0]

    db = store_factory()
    assert db.get(watch_item['id'])['status'] == 'active'
    db.close()


def test_non_negotiable_completion_daily_rollover_and_trigger_now(tmp_path, monkeypatch):
    day1 = datetime(2026, 11, 11, 16, 0, 0, tzinfo=timezone.utc)  # 2026-11-11 10:00 CST
    save(tmp_path, dict(defaults(), notificationsEnabled=False, timezone='America/Chicago'))
    store_factory, reg_factory, routes, _ = _make_harness(tmp_path)

    bound_route = {
        'route_id': 'r-bound',
        'kind': 'session_key',
        'session_key': 'desktop:session-9',
        'origin': None,
        'platform': 'desktop',
    }
    monkeypatch.setattr('butler.tools.capture_bound_turn_route', lambda: bound_route)

    ctx = FakeHostContext(accepted=True)
    clock = [day1]
    register_tools(
        ctx,
        store_factory,
        directory=tmp_path,
        route_registry_factory=reg_factory,
        now_fn=lambda: clock[0],
    )

    # Auto-scheduled non-negotiable (due_at omitted!)
    created_resp = json.loads(ctx.tools['butler_create']({
        'title': '30m mobility + walk',
        'kind': 'non_negotiable',
        'prep_hint': 'Check calendar gap',
    }))
    assert created_resp['wake'] == 'scheduled'
    nn_id = created_resp['responsibility']['id']
    assert routes.get_route('r-bound') is not None

    # Even with notificationsEnabled=False and future due_at, trigger_now fires manual check-in!
    runner = Runner(ctx, store_factory, directory=tmp_path, route_registry_factory=reg_factory, now_fn=lambda: clock[0])
    trig = runner.trigger_now()
    assert trig['ok'] is True
    assert trig['injected'] == 1
    assert '30m mobility + walk' in ctx.calls[-1][0]

    # Mark completed today via butler_manage -> stamps completed_day
    done_resp = json.loads(ctx.tools['butler_manage']({'action': 'complete', 'id': nn_id}))
    assert done_resp['responsibility']['completed_day'] == '2026-11-11'

    # Next morning (2026-11-12 09:00 CST = 15:00 UTC) with notificationsEnabled=True -> resets & wakes!
    save(tmp_path, dict(defaults(), notificationsEnabled=True, timezone='America/Chicago'))
    clock[0] = datetime(2026, 11, 12, 15, 0, 0, tzinfo=timezone.utc)
    tick_res = runner.tick()
    assert tick_res['reset_non_negotiables'] == 1
    assert tick_res['injected'] == 1
    assert len(ctx.calls) == 2


def test_tools_update_reopen_remove_and_delete_rejection(tmp_path, monkeypatch):
    now = datetime(2026, 11, 11, 15, 0, 0, tzinfo=timezone.utc)
    store_factory, reg_factory, _, _ = _make_harness(tmp_path)
    monkeypatch.setattr('butler.tools.capture_bound_turn_route', lambda: None)

    ctx = FakeHostContext()
    register_tools(
        ctx,
        store_factory,
        directory=tmp_path,
        route_registry_factory=reg_factory,
        now_fn=lambda: now,
    )
    create = ctx.tools['butler_create']
    manage = ctx.tools['butler_manage']

    # Auto-schedule with deadline_at and no due_at
    res = json.loads(create({
        'title': 'File quarterly report',
        'deadline_at': '2026-11-12T17:00:00+00:00',
        'notes': 'Include Q3 numbers',
    }))
    assert 'unavailable' in res['wake']
    item_id = res['responsibility']['id']

    # Update title and notes
    upd = json.loads(manage({'action': 'update', 'id': item_id, 'title': 'File Q3 report', 'notes': 'Updated'}))
    assert upd['responsibility']['title'] == 'File Q3 report'
    assert upd['responsibility']['metadata']['notes'] == 'Updated'

    # Stop then reopen
    assert json.loads(manage({'action': 'stop', 'id': item_id}))['responsibility']['status'] == 'stopped'
    reopened = json.loads(manage({'action': 'reopen', 'id': item_id, 'due_at': '2026-11-12T12:00:00Z'}))
    assert reopened['responsibility']['status'] == 'active'

    # 'delete' must remain rejected per existing contract; 'remove' deletes cleanly
    assert 'error' in json.loads(manage({'action': 'delete', 'id': item_id}))
    removed = json.loads(manage({'action': 'remove', 'id': item_id}))
    assert removed == {'removed': True, 'id': item_id}
    assert 'error' in json.loads(manage({'action': 'get', 'id': item_id}))


def test_awareness_route_capture_controls_status_and_event_hook_origin_capture(tmp_path, monkeypatch):
    now = datetime(2026, 11, 11, 15, 0, 0, tzinfo=timezone.utc)
    save(tmp_path, dict(defaults(), notificationsEnabled=True, timezone='America/Chicago', dailyLimit=4))
    store_factory, reg_factory, routes, _ = _make_harness(tmp_path)

    db = store_factory()
    db.create('Prepare board memo', now + timedelta(hours=1), route_id='r-live')
    db.close()

    started = []
    monkeypatch.setattr('butler.awareness.capture_bound_turn_route', lambda: {
        'route_id': 'r-live',
        'kind': 'session_key',
        'session_key': 'tui:live-1',
        'origin': None,
        'platform': 'tui',
    })

    ctx = FakeHostContext(opt_in=False)
    hooks = register_awareness_and_controls(
        ctx,
        directory=lambda: tmp_path,
        config_loader=lambda: {'timezone': 'America/Chicago'},
        system_tz_fn=lambda: 'America/Chicago',
        now_fn=lambda: now,
        on_bound_turn=lambda: started.append(True),
        store_factory=store_factory,
        route_registry_factory=reg_factory,
    )

    # Child subagent turns skip route capture, starter, and context injection
    assert hooks.pre_llm_call(parent_session_id='subagent-1', platform='tui') is None
    assert started == []
    assert routes.get_route('r-live') is None

    # Primary bound turn captures route, starts runner callback, and includes active responsibility
    injected = hooks.pre_llm_call(is_first_turn=True, platform='tui')
    assert injected is not None
    assert 'Prepare board memo' in injected['context']
    assert started == [True]
    assert routes.get_route('r-live')['session_key'] == 'tui:live-1'

    # Controls status returns cadence and quota; privacy gate still blocks notificationsEnabled
    st = json.loads(ctx.tools['butler_controls']({'action': 'status'}))
    assert st['ok'] is True
    assert 'cadence' in st and 'quota' in st
    denied = json.loads(ctx.tools['butler_controls']({
        'action': 'update',
        'settings': {'notificationsEnabled': True},
    }))
    assert denied['ok'] is False
    assert 'notificationsEnabled' in denied['error']

    # Admitted DM captures origin route when notificationsEnabled=True even with collect_event_history=False
    monkeypatch.setattr('butler.event_hooks.capture_admitted_gateway_origin', lambda **kw: {
        'route_id': 'r-dm',
        'kind': 'origin',
        'session_key': '',
        'origin': {'platform': kw['platform'], 'chat_id': 'c1', 'user_id': 'u1', 'chat_type': 'dm'},
        'platform': kw['platform'],
    })
    ev_hooks = register_event_hooks(ctx, directory=lambda: tmp_path, route_registry_factory=reg_factory)
    ev_hooks.post_gateway_admission(
        platform='telegram',
        source={'platform': 'telegram', 'chat_id': 'c1', 'user_id': 'u1', 'chat_type': 'dm'},
        text='hi',
        message_id='m1',
    )
    assert routes.get_route('r-dm') is not None
    assert not (tmp_path / 'events.sqlite3').exists()


def test_plugin_entrypoint_active_runner_lifecycle(tmp_path, monkeypatch):
    import sys
    import types

    storage = types.ModuleType('plugins.plugin_storage')
    storage.plugin_data_dir = lambda _: tmp_path
    monkeypatch.setitem(sys.modules, 'plugins', types.ModuleType('plugins'))
    monkeypatch.setitem(sys.modules, 'plugins.plugin_storage', storage)

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        'butler_lifecycle_test', root / '__init__.py', submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        ctx = FakeHostContext()
        module.register(ctx)
        assert set(ctx.tools) == {'butler_create', 'butler_manage', 'butler_expression', 'butler_controls', 'butler_imessage'}
        assert len(ctx.hooks['post_gateway_admission']) == 2
        assert len(ctx.hooks['pre_llm_call']) == 2

        # Trigger start via post_gateway_admission
        for cb in ctx.hooks['post_gateway_admission']:
            cb()
        active = module.get_active_runner()
        assert active is not None
        assert get_active_runner() is active

        ctx.cleanup()
        assert module.get_active_runner() is None
    finally:
        sys.modules.pop(spec.name, None)
