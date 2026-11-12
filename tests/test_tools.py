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


if __name__ == '__main__':
    unittest.main()
