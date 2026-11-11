"""Agent-facing responsibility tools; no scheduler or outreach is started here."""
from datetime import datetime
import json


def _default_store():
    # Deferred: registration itself must not create data or depend on a live profile.
    from plugins.plugin_storage import plugin_data_dir
    from .store import ResponsibilityStore
    return ResponsibilityStore(plugin_data_dir('overseer') / 'responsibilities.sqlite3')


def register_tools(ctx, store_factory=None, session_provider=None):
    """Register synchronous Hermes tools. The factory must return a store per call."""
    factory = store_factory or _default_store

    # A caller-provided session key is never an authorization. Retain the
    # optional argument for compatibility, but do not use it for routing.

    def create(args, **kwargs):
        title, due = args.get('title'), args.get('due_at')
        if not isinstance(title, str) or not title.strip():
            return json.dumps({'error': 'title must be a non-empty string'})
        try:
            when = datetime.fromisoformat(due.replace('Z', '+00:00'))
            if when.tzinfo is None or when.utcoffset() is None:
                raise ValueError('timezone required')
        except (TypeError, ValueError, AttributeError):
            return json.dumps({'error': 'due_at must be an ISO 8601 datetime with timezone'})
        store = factory()
        try:
            item = store.create(title.strip(), when)
            return json.dumps({'responsibility': item, 'wake': 'unavailable: no authenticated session route; item will not be injected'}, default=str)
        finally:
            if hasattr(store, 'close'):
                store.close()

    schema = {'name': 'overseer_create', 'description': 'Record a responsibility the user authorized. The agent decides what to do; this does not send messages or perform the task.', 'parameters': {'type': 'object', 'properties': {'title': {'type': 'string'}, 'due_at': {'type': 'string', 'description': 'ISO 8601 datetime including UTC offset'}}, 'required': ['title', 'due_at'], 'additionalProperties': False}}
    ctx.register_tool(name='overseer_create', toolset='overseer', schema=schema, handler=create, description=schema['description'])

    def manage(args, **kwargs):
        action, item_id = args.get('action'), args.get('id')
        if action not in ('list', 'get', 'pause', 'resume', 'stop', 'complete', 'snooze'):
            return json.dumps({'error': 'unsupported action'})
        if action != 'list' and (not isinstance(item_id, str) or not item_id.strip()):
            return json.dumps({'error': 'id is required'})
        when = None
        if action == 'snooze':
            try:
                when = datetime.fromisoformat(args['until'].replace('Z', '+00:00'))
                if when.tzinfo is None or when.utcoffset() is None:
                    raise ValueError('timezone required')
            except (KeyError, TypeError, ValueError, AttributeError):
                return json.dumps({'error': 'until must be an ISO 8601 datetime with timezone'})
        store = factory()
        try:
            if action == 'list':
                result = {'responsibilities': store.list()}
            elif action == 'get':
                item = store.get(item_id)
                result = {'responsibility': item} if item else {'error': 'responsibility not found'}
            else:
                changed = store.snooze(item_id, when) if action == 'snooze' else getattr(store, action)(item_id)
                result = {'responsibility': store.get(item_id)} if changed else {'error': 'responsibility not found or transition not allowed'}
            return json.dumps(result, default=str)
        finally:
            if hasattr(store, 'close'):
                store.close()

    schema = {'name': 'overseer_manage', 'description': 'Inspect or update an existing responsibility at the user or agent’s direction. No external actions are executed.', 'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['list', 'get', 'pause', 'resume', 'stop', 'complete', 'snooze']}, 'id': {'type': 'string'}, 'until': {'type': 'string', 'description': 'Timezone-aware ISO 8601 datetime for snooze'}}, 'required': ['action'], 'additionalProperties': False}}
    ctx.register_tool(name='overseer_manage', toolset='overseer', schema=schema, handler=manage, description=schema['description'])
