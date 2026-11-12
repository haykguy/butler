import json
import unittest

from unittest.mock import patch

from butler.tools import register_tools


class FakeContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs


class FakeStore:
    def __init__(self):
        self.items = []

    def create(self, title, due_at):
        item = {'id': '1', 'title': title, 'due_at': due_at.isoformat()}
        self.items.append(item)
        return item

    def get(self, item_id):
        return next((x for x in self.items if x['id'] == item_id), None)

    def list(self):
        return self.items

    def pause(self, item_id, *, notes=None):
        item = self.get(item_id)
        if item is None:
            return False
        item['status'] = 'paused'
        if notes is not None:
            meta = dict(item.get('metadata') or {})
            meta['notes'] = str(notes).strip()
            item['metadata'] = meta
        return True


class FakeRouteRegistry:
    def record_route(self, route, preferred=True):
        return route['route_id']

    def close(self):
        pass


class ToolTests(unittest.TestCase):
    def test_register_and_create_responsibility(self):
        ctx, store = FakeContext(), FakeStore()
        register_tools(ctx, lambda: store)
        result = json.loads(ctx.tools['butler_create']['handler'](
            {'title': 'Review report', 'due_at': '2026-12-01T09:00:00+00:00'}))
        self.assertEqual(result['responsibility']['title'], 'Review report')
        self.assertEqual(len(store.items), 1)

    def test_create_rejects_same_open_task_with_deadline_wording_variant(self):
        ctx, store = FakeContext(), FakeStore()
        register_tools(ctx, lambda: store, route_registry_factory=lambda: FakeRouteRegistry())
        create = ctx.tools['butler_create']['handler']
        with patch('butler.tools.capture_bound_turn_route', return_value={'route_id': 'route-1'}):
            first = json.loads(create({'title': 'Send the Acme invoice', 'due_at': '2026-11-12T10:00:00-06:00'}))
            store.items[0].update({'kind': 'commitment', 'route_id': 'route-1', 'status': 'paused'})
            second = json.loads(create({'title': 'Send Acme invoice before 5pm today', 'due_at': '2026-11-12T16:40:00-06:00'}))
        self.assertEqual(len(store.items), 1)
        self.assertIn('duplicate', second['error'].lower())
        self.assertEqual(second['existing_responsibility']['id'], first['responsibility']['id'])

    def test_create_allows_explicit_distinct_same_title_instance(self):
        ctx, store = FakeContext(), FakeStore()
        register_tools(ctx, lambda: store, route_registry_factory=lambda: FakeRouteRegistry())
        create = ctx.tools['butler_create']['handler']
        with patch('butler.tools.capture_bound_turn_route', return_value={'route_id': 'route-1'}):
            create({'title': 'Call dentist', 'due_at': '2026-11-12T10:00:00-06:00'})
            store.items[0].update({'kind': 'commitment', 'route_id': 'route-1', 'status': 'active'})
            result = json.loads(create({
                'title': 'Call dentist',
                'due_at': '2026-11-13T10:00:00-06:00',
                'allow_duplicate': True,
            }))
        self.assertIn('responsibility', result)
        self.assertEqual(len(store.items), 2)

    def test_unbound_create_does_not_match_items_without_route_identity(self):
        ctx, store = FakeContext(), FakeStore()
        store.items.append({'id': 'existing', 'title': 'Call dentist', 'status': 'active'})
        register_tools(ctx, lambda: store, route_registry_factory=lambda: FakeRouteRegistry())
        with patch('butler.tools.capture_bound_turn_route', return_value=None):
            result = json.loads(ctx.tools['butler_create']['handler']({
                'title': 'Call dentist', 'due_at': '2026-11-12T10:00:00-06:00',
            }))
        self.assertIn('responsibility', result)
        self.assertEqual(len(store.items), 2)

    def test_create_rejects_non_boolean_allow_duplicate(self):
        ctx, store = FakeContext(), FakeStore()
        register_tools(ctx, lambda: store)
        result = json.loads(ctx.tools['butler_create']['handler']({
            'title': 'Call dentist', 'allow_duplicate': 'yes',
        }))
        self.assertIn('error', result)
        self.assertEqual(store.items, [])

    def test_manage_list_and_pause(self):
        ctx, store = FakeContext(), FakeStore()
        store.create('Review', __import__('datetime').datetime.now(__import__('datetime').timezone.utc))
        register_tools(ctx, lambda: store)
        handler = ctx.tools['butler_manage']['handler']
        self.assertEqual(len(json.loads(handler({'action': 'list'}))['responsibilities']), 1)
        self.assertEqual(json.loads(handler({'action': 'pause', 'id': '1'}))['responsibility']['status'], 'paused')
        self.assertIn('error', json.loads(handler({'action': 'delete', 'id': '1'})))

    def test_real_store_roundtrip(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from butler.store import ResponsibilityStore
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            ctx = FakeContext()
            register_tools(ctx, lambda: ResponsibilityStore(Path(directory) / 'test.sqlite'))
            created = json.loads(ctx.tools['butler_create']['handler']({'title': 'Review', 'due_at': '2026-12-01T09:00:00Z'}))['responsibility']
            manage = ctx.tools['butler_manage']['handler']
            paused = json.loads(manage({'action': 'pause', 'id': created['id'], 'notes': 'Await user reply'}))['responsibility']
            self.assertEqual(paused['status'], 'paused')
            self.assertEqual(paused['metadata']['notes'], 'Await user reply')
            self.assertEqual(json.loads(manage({'action': 'get', 'id': created['id']}))['responsibility']['status'], 'paused')

    def test_reject_naive_datetime(self):
        ctx, store = FakeContext(), FakeStore()
        register_tools(ctx, lambda: store)
        self.assertIn('error', json.loads(ctx.tools['butler_create']['handler']({'title': 'x', 'due_at': '2026-01-01T09:00:00'})))
        self.assertEqual(store.items, [])

    def test_phone_tool_double_gate_and_owner_ring(self):
        from datetime import datetime, timezone
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from butler.phone import PhoneStore
        from butler.phone_tools import register_phone_tools
        from butler.settings import defaults as settings_defaults, save as save_settings

        now = datetime(2026, 11, 11, 16, 0, 0, tzinfo=timezone.utc)
        fake_env = {
            'LIVEKIT_URL': 'wss://example.livekit.cloud',
            'LIVEKIT_API_KEY': 'APIkeyTest',
            'LIVEKIT_API_SECRET': 'secretTestValue1234567890',
            'LIVEKIT_SIP_TRUNK_ID': 'ST_test123',
            'LIVEKIT_CALLER_NUMBER': '+14155550100',
        }
        dispatched = []

        def fake_dispatch(**kwargs):
            dispatched.append(kwargs)
            return {'id': f'disp_{len(dispatched)}'}

        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            prof = Path(directory)
            cfg = settings_defaults()
            cfg.update({
                'timezone': 'America/Chicago',
                'phoneEnabled': True,
                'ownerRingEnabled': True,
                'ownerPhone': '+14155550123',
            })
            save_settings(prof, cfg)
            ctx = FakeContext()
            register_phone_tools(
                ctx,
                directory=lambda: prof,
                now_fn=lambda: now,
                system_tz_fn=lambda: 'America/Chicago',
                env_override=fake_env,
                dispatch_fn=fake_dispatch,
            )
            handler = ctx.tools['butler_phone']['handler']

            # LLM cannot self-approve
            denied = json.loads(handler({'action': 'approve', 'plan_id': 'plan_x'}))
            self.assertFalse(denied['ok'])
            self.assertEqual(denied['error'], 'human_approval_only')

            # Draft outbound call -> pending_approval with masked number
            drafted = json.loads(handler({
                'action': 'draft_call',
                'to_number': '+14155550199',
                'recipient_label': 'Clinic Desk',
                'objective': 'Confirm Thursday 3pm appointment',
            }))
            self.assertTrue(drafted['ok'])
            plan_id = drafted['plan']['plan_id']
            self.assertEqual(drafted['plan']['status'], 'pending_approval')
            self.assertNotIn('+14155550199', json.dumps(drafted))

            # Placing before human approval is blocked
            unapproved = json.loads(handler({'action': 'place_call', 'plan_id': plan_id}))
            self.assertFalse(unapproved['ok'])
            self.assertEqual(unapproved['error'], 'approval_required')

            # Human approves via PhoneStore -> place_call succeeds
            PhoneStore(prof).approve_plan(plan_id, approved_by='human_ui', now=now)
            placed = json.loads(handler({'action': 'place_call', 'plan_id': plan_id}))
            self.assertTrue(placed['ok'])
            call_id = placed['call']['call_id']
            self.assertEqual(placed['call']['status'], 'dispatching')

            # Cancel active call releases single-active-call lock
            cancelled = json.loads(handler({'action': 'cancel_call', 'call_id': call_id, 'reason': 'Test cancel'}))
            self.assertTrue(cancelled['ok'])
            self.assertEqual(cancelled['call']['status'], 'cancelled')

            # Owner ring dispatches when policy allows
            rung = json.loads(handler({'action': 'ring_owner', 'reason': 'Flight boarding in 20 minutes'}))
            self.assertTrue(rung['ok'])
            self.assertEqual(rung['call']['direction'], 'owner_ring')

            # Status and list_calls work
            st = json.loads(handler({'action': 'call_status'}))
            self.assertTrue(st['ok'])
            self.assertEqual(st['status']['rings_last_hour'], 1)
            listed = json.loads(handler({'action': 'list_calls', 'limit': 10}))
            self.assertTrue(listed['ok'])
            self.assertEqual(len(listed['calls']), 2)

            # stop_escalation engages phone_stopped latch
            halted = json.loads(handler({'action': 'stop_escalation', 'reason': 'Owner asked to stop'}))
            self.assertTrue(halted['ok'])
            self.assertTrue(halted['stopped'])
            blocked = json.loads(handler({'action': 'ring_owner', 'reason': 'Another ping'}))
            self.assertFalse(blocked['ok'])
            self.assertEqual(blocked['error'], 'phone_stopped_kill_switch')


if __name__ == '__main__':
    unittest.main()
