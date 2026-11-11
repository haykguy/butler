import json
import unittest

from overseer.tools import register_tools


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

    def pause(self, item_id):
        item = self.get(item_id)
        if item is None:
            return False
        item['status'] = 'paused'
        return True


class ToolTests(unittest.TestCase):
    def test_register_and_create_responsibility(self):
        ctx, store = FakeContext(), FakeStore()
        register_tools(ctx, lambda: store)
        result = json.loads(ctx.tools['overseer_create']['handler'](
            {'title': 'Review report', 'due_at': '2026-12-01T09:00:00+00:00'}))
        self.assertEqual(result['responsibility']['title'], 'Review report')
        self.assertEqual(len(store.items), 1)

    def test_manage_list_and_pause(self):
        ctx, store = FakeContext(), FakeStore()
        store.create('Review', __import__('datetime').datetime.now(__import__('datetime').timezone.utc))
        register_tools(ctx, lambda: store)
        handler = ctx.tools['overseer_manage']['handler']
        self.assertEqual(len(json.loads(handler({'action': 'list'}))['responsibilities']), 1)
        self.assertEqual(json.loads(handler({'action': 'pause', 'id': '1'}))['responsibility']['status'], 'paused')
        self.assertIn('error', json.loads(handler({'action': 'delete', 'id': '1'})))

    def test_real_store_roundtrip(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from overseer.store import ResponsibilityStore
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            ctx = FakeContext()
            register_tools(ctx, lambda: ResponsibilityStore(Path(directory) / 'test.sqlite'))
            created = json.loads(ctx.tools['overseer_create']['handler']({'title': 'Review', 'due_at': '2026-12-01T09:00:00Z'}))['responsibility']
            manage = ctx.tools['overseer_manage']['handler']
            self.assertEqual(json.loads(manage({'action': 'pause', 'id': created['id']}))['responsibility']['status'], 'paused')
            self.assertEqual(json.loads(manage({'action': 'get', 'id': created['id']}))['responsibility']['status'], 'paused')

    def test_reject_naive_datetime(self):
        ctx, store = FakeContext(), FakeStore()
        register_tools(ctx, lambda: store)
        self.assertIn('error', json.loads(ctx.tools['overseer_create']['handler']({'title': 'x', 'due_at': '2026-01-01T09:00:00'})))
        self.assertEqual(store.items, [])


if __name__ == '__main__':
    unittest.main()
