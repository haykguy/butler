import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from butler.runner import Runner
from butler.store import ResponsibilityStore
from butler.tools import register_tools
from butler.expressions import register_expression_tools


class Context:
    def __init__(self, accepted=True):
        self.tools, self.hooks, self.calls = {}, {}, []
        self.accepted = accepted
    def register_tool(self, **kw): self.tools[kw['name']] = kw['handler']
    def register_hook(self, name, callback): self.hooks.setdefault(name, []).append(callback)
    def on_unload(self, callback): self.cleanup = callback
    def inject_message(self, text, **kw):
        self.calls.append((text, kw))
        return self.accepted
    def dispatch_tool(self, *args):
        raise AssertionError('unsupported channel must not dispatch')


def test_legacy_routed_item_remains_active_without_injection(tmp_path):
    factory = lambda: ResponsibilityStore(tmp_path / 'db.sqlite')
    db = factory()
    item = db.create('Review', datetime.now(timezone.utc) - timedelta(seconds=1),
                     metadata={'session_key': 'agent:default:telegram:dm:123'})
    db.close()
    ctx = Context()
    Runner(ctx, factory).tick()
    assert not ctx.calls
    db = factory()
    assert db.get(item['id'])['status'] == 'active'
    assert db.get(item['id'])['claim_token'] is None
    db.close()


def test_missing_route_never_injects(tmp_path):
    path = tmp_path / 'db.sqlite'
    factory = lambda: ResponsibilityStore(path)
    db = factory()
    item = db.create('test', datetime.now(timezone.utc) - timedelta(seconds=1))
    db.close()
    ctx = Context()
    Runner(ctx, factory).tick()
    assert not ctx.calls
    db = factory()
    assert db.get(item['id'])['status'] == 'active'
    db.close()


def test_default_tool_route_fails_closed_even_if_environment_claims_session(tmp_path, monkeypatch):
    import sys
    import types
    package = types.ModuleType('hermes_cli')
    package.__path__ = []
    session = types.ModuleType('hermes_cli.plugin_session')
    session.session_key = lambda: 'agent:default:telegram:dm:123'
    monkeypatch.setitem(sys.modules, 'hermes_cli', package)
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_session', session)
    monkeypatch.setenv('HERMES_SESSION_KEY', 'agent:default:telegram:dm:123')
    ctx = Context()
    factory = lambda: ResponsibilityStore(tmp_path / 'db.sqlite')
    register_tools(ctx, factory)
    result = json.loads(ctx.tools['butler_create']({'title': 'Review', 'due_at': '2020-01-01T00:00:00Z'}))
    assert 'unavailable' in result['wake']
    assert not result['responsibility']['metadata'].get('session_key')
    Runner(ctx, factory).tick()
    assert not ctx.calls


def test_telegram_expression_refuses():
    ctx = Context()
    register_expression_tools(ctx, lambda: ('telegram', '123'))
    result = json.loads(ctx.tools['butler_expression']({'action': 'reply', 'chat_id': '123', 'message_id': 'x', 'text': 'hi'}))
    assert result['ok'] is False
    assert 'unsupported' in result['error']


def test_entrypoint_registers_declared_tools_and_lifecycle():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('butler_plugin_test', root / '__init__.py', submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        ctx = Context()
        module.register(ctx)
        assert set(ctx.tools) == {'butler_create', 'butler_manage', 'butler_expression', 'butler_controls', 'butler_imessage'}
        assert set(ctx.hooks) == {'post_gateway_admission', 'pre_llm_call', 'post_llm_call'}
        assert len(ctx.hooks['post_gateway_admission']) == 2
        assert len(ctx.hooks['pre_llm_call']) == 2
        ctx.cleanup()
        [callback() for callback in ctx.hooks['post_gateway_admission']]
    finally:
        sys.modules.pop(spec.name, None)


def test_manifest_declares_post_llm_call_hook():
    root = Path(__file__).resolve().parents[1]
    manifest = (root / 'plugin.yaml').read_text()
    hooks_section = manifest.split('provides_hooks:', 1)[1]
    declared_hooks = {line.strip()[2:] for line in hooks_section.splitlines() if line.startswith('  - ')}
    assert 'post_llm_call' in declared_hooks


def test_exception_does_not_acknowledge(tmp_path):
    path = tmp_path / 'db.sqlite'
    factory = lambda: ResponsibilityStore(path)
    db = factory()
    item = db.create('test', datetime.now(timezone.utc) - timedelta(seconds=1), metadata={'session_key': 's'})
    db.close()
    class Broken(Context):
        def inject_message(self, *args, **kwargs):
            raise RuntimeError('host unavailable')
    Runner(Broken(), factory).tick()
    db = factory()
    assert db.get(item['id'])['status'] == 'active'
    db.close()


def test_legacy_metadata_route_does_not_authorize_injection(tmp_path):
    factory = lambda: ResponsibilityStore(tmp_path / 'db.sqlite')
    db = factory()
    db.create('untrusted legacy route', datetime.now(timezone.utc) - timedelta(seconds=1),
              metadata={'session_key': 'agent:default:telegram:dm:123'})
    db.close()
    ctx = Context()
    Runner(ctx, factory).tick()
    assert not ctx.calls


def test_injected_session_provider_cannot_enable_scheduling(tmp_path):
    ctx = Context()
    factory = lambda: ResponsibilityStore(tmp_path / 'db.sqlite')
    register_tools(ctx, factory, lambda: 'agent:default:telegram:dm:123')
    result = json.loads(ctx.tools['butler_create']({'title': 'Review', 'due_at': '2020-01-01T00:00:00Z'}))
    assert 'unavailable' in result['wake']
    assert not result['responsibility']['metadata'].get('session_key')
