"""Agent-facing responsibility tools with smart auto-scheduling and route binding."""
from datetime import datetime, time, timedelta, timezone
import inspect
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .context import local_time, resolve_effective_timezone
from .settings import defaults, load as load_settings

_VALID_KINDS = ('commitment', 'non_negotiable', 'check_in', 'watch', 'open_loop')
_VALID_MANAGE_ACTIONS = (
    'list', 'get', 'pause', 'resume', 'stop', 'complete', 'snooze', 'reopen', 'remove', 'update',
)
_UNAVAILABLE_WAKE_MSG = 'unavailable: no authenticated session route; item will not be injected'


class _NoopRouteRegistry:
    def record_route(self, route, preferred=True):
        return None

    def close(self):
        return None


def _call_capture_bound_turn_route():
    try:
        from . import routes as _routes
        return _routes.capture_bound_turn_route()
    except ImportError:
        return None


def _open_route_registry(db_path):
    try:
        from . import routes as _routes
        return _routes.RouteRegistry(db_path)
    except ImportError:
        return _NoopRouteRegistry()


def _call_compute_initial_schedule(
    kind, now, settings, *, effective_timezone='UTC', due_at=None, deadline_at=None, interval_seconds=None,
):
    try:
        from . import followup as _followup
        return _followup.compute_initial_schedule(
            kind,
            now,
            settings,
            effective_timezone=effective_timezone,
            due_at=due_at,
            deadline_at=deadline_at,
            interval_seconds=interval_seconds,
        )
    except ImportError:
        # No built-in cadence: explicit due_at, else due now so the model is woken and chooses the time.
        resolved_interval = interval_seconds
        if kind == 'watch' and resolved_interval is None:
            resolved_interval = 900
        if due_at is not None:
            resolved_due = due_at
        elif kind == 'watch':
            resolved_due = now + timedelta(seconds=resolved_interval)
        else:
            resolved_due = now
        return {'due_at': resolved_due, 'deadline_at': deadline_at, 'interval_seconds': resolved_interval}


capture_bound_turn_route = _call_capture_bound_turn_route
RouteRegistry = _open_route_registry
compute_initial_schedule = _call_compute_initial_schedule


def _default_store():
    # Deferred: registration itself must not create data or depend on a live profile.
    from plugins.plugin_storage import plugin_data_dir
    from .store import ResponsibilityStore
    return ResponsibilityStore(plugin_data_dir('butler') / 'responsibilities.sqlite3')


def _store_db_path(store) -> Path | None:
    path_attr = getattr(store, 'path', None)
    if path_attr:
        return Path(path_attr)
    db = getattr(store, 'db', None)
    if db is not None and hasattr(db, 'execute'):
        try:
            for row in db.execute('PRAGMA database_list').fetchall():
                if row[1] == 'main' and row[2] and row[2] != ':memory:':
                    return Path(row[2])
        except Exception:
            return None
    return None


def _resolve_profile_dir(store, directory) -> Path | None:
    if directory is not None:
        return Path(directory() if callable(directory) else directory)
    db_path = _store_db_path(store)
    if db_path is not None:
        return db_path.parent
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir('butler')
    except Exception:
        return None


def _parse_aware_iso(raw, field_name: str) -> tuple[datetime | None, str | None]:
    try:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError('empty')
        dt = datetime.fromisoformat(raw.strip().replace('Z', '+00:00'))
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError('timezone required')
        return dt, None
    except (TypeError, ValueError, AttributeError):
        return None, f'{field_name} must be an ISO 8601 datetime with timezone'


def _invoke_store_create(store, title: str, when: datetime, **extra_kwargs):
    try:
        sig = inspect.signature(store.create)
        params = sig.parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        return store.create(title, when)

    filtered = {}
    for key, val in extra_kwargs.items():
        if not (has_var_kw or key in params):
            continue
        if val is None and key not in ('timezone_name',):
            continue
        filtered[key] = val
    return store.create(title, when, **filtered)


def _title_identity(title: str) -> frozenset[str]:
    """Normalize punctuation and transient timing words for duplicate-open-task checks."""
    ignored = {
        'a', 'an', 'the', 'to', 'for', 'by', 'at', 'on', 'in', 'before', 'after',
        'from', 'this', 'next', 'today', 'tomorrow', 'tonight', 'now', 'am', 'pm',
    }
    return frozenset(token for token in re.findall(r'[a-z]+', title.casefold()) if len(token) > 1 and token not in ignored)


def _find_duplicate_open_responsibility(store, title, kind, route_id):
    identity = _title_identity(title)
    if not identity:
        return None
    try:
        items = store.list()
    except Exception:
        return None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get('status', 'active') not in ('active', 'queued', 'submitted'):
            continue
        if item.get('kind', kind) != kind or item.get('route_id') != route_id:
            continue
        existing_title = item.get('title')
        if isinstance(existing_title, str) and _title_identity(existing_title) == identity:
            return item
    return None


def register_tools(
    ctx,
    store_factory=None,
    session_provider=None,
    *,
    directory=None,
    route_registry_factory=None,
    now_fn=None,
):
    """Register synchronous Hermes tools. The factory must return a store per call."""
    factory = store_factory or _default_store
    clock = now_fn or (lambda: datetime.now(timezone.utc))

    # A caller-provided session_provider or process env is never an authorization.
    # Retain session_provider in the signature for backwards compatibility only.
    del session_provider

    def _bind_authenticated_route(store, prof_dir: Path | None) -> str | None:
        route = capture_bound_turn_route()
        if not isinstance(route, dict) or not route.get('route_id'):
            return None
        reg = None
        try:
            if route_registry_factory is not None:
                reg = route_registry_factory()
            else:
                db_path = _store_db_path(store) or (
                    (prof_dir / 'responsibilities.sqlite3') if prof_dir is not None else None
                )
                if db_path is None:
                    return None
                db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                reg = RouteRegistry(db_path)
            if reg is None:
                return None
            recorded_id = reg.record_route(route, preferred=True)
            return recorded_id or route['route_id']
        finally:
            if reg is not None and hasattr(reg, 'close'):
                reg.close()

    def create(args, **kwargs):
        args = args or {}
        title = args.get('title')
        if not isinstance(title, str) or not title.strip():
            return json.dumps({'error': 'title must be a non-empty string'})
        allow_duplicate = args.get('allow_duplicate', False)
        if not isinstance(allow_duplicate, bool):
            return json.dumps({'error': 'allow_duplicate must be a boolean'})

        watch_file = args.get('watch_file')
        if watch_file is not None:
            if not isinstance(watch_file, str) or not watch_file.strip() or not os.path.isabs(watch_file.strip()):
                return json.dumps({'error': 'watch_file must be an absolute file path'})
            watch_file = watch_file.strip()

        kind = args.get('kind') or ('watch' if watch_file else 'commitment')
        if kind not in _VALID_KINDS:
            return json.dumps({'error': f'kind must be one of {", ".join(_VALID_KINDS)}'})

        when = None
        if 'due_at' in args and args['due_at'] is not None:
            when, err = _parse_aware_iso(args['due_at'], 'due_at')
            if err:
                return json.dumps({'error': err})

        deadline_dt = None
        if 'deadline_at' in args and args['deadline_at'] is not None:
            deadline_dt, err = _parse_aware_iso(args['deadline_at'], 'deadline_at')
            if err:
                return json.dumps({'error': err})

        interval_seconds = args.get('interval_seconds')
        if interval_seconds is not None:
            if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds <= 0:
                return json.dumps({'error': 'interval_seconds must be a positive integer'})

        notes = args.get('notes')
        if notes is not None and not isinstance(notes, str):
            return json.dumps({'error': 'notes must be a string'})

        prep_hint = args.get('prep_hint')
        if prep_hint is not None and not isinstance(prep_hint, str):
            return json.dumps({'error': 'prep_hint must be a string'})

        store = factory()
        try:
            prof_dir = _resolve_profile_dir(store, directory)
            try:
                prof_settings = load_settings(prof_dir) if prof_dir is not None else defaults()
            except Exception:
                prof_settings = defaults()
            eff_tz, _ = resolve_effective_timezone(prof_settings)
            now = clock()

            if when is None or deadline_dt is not None or (interval_seconds is None and kind in ('watch', 'check_in')):
                sched = compute_initial_schedule(
                    kind,
                    now,
                    prof_settings,
                    effective_timezone=eff_tz,
                    due_at=when,
                    deadline_at=deadline_dt,
                    interval_seconds=interval_seconds,
                )
                if when is None:
                    when = sched['due_at']
                if deadline_dt is None and sched.get('deadline_at') is not None:
                    deadline_dt = sched['deadline_at']
                if interval_seconds is None and sched.get('interval_seconds') is not None:
                    interval_seconds = sched['interval_seconds']

            meta = {}
            if notes and notes.strip():
                meta['notes'] = notes.strip()
            if prep_hint and prep_hint.strip():
                meta['prep_hint'] = prep_hint.strip()
            if watch_file:
                meta['watch'] = {'kind': 'file', 'path': watch_file}

            route_id = _bind_authenticated_route(store, prof_dir)
            duplicate = None
            if route_id and not allow_duplicate:
                duplicate = _find_duplicate_open_responsibility(store, title.strip(), kind, route_id)
            if duplicate:
                return json.dumps({
                    'error': 'duplicate open responsibility; update or reschedule the existing item instead of creating a second copy',
                    'existing_responsibility': duplicate,
                }, default=str)
            wake_status = 'scheduled' if route_id else _UNAVAILABLE_WAKE_MSG

            item = _invoke_store_create(
                store,
                title.strip(),
                when,
                timezone_name=eff_tz,
                interval_seconds=interval_seconds,
                metadata=meta if meta else None,
                kind=kind if kind != 'commitment' or route_id or deadline_dt else 'commitment',
                route_id=route_id,
                deadline_at=deadline_dt,
            )
            return json.dumps({'responsibility': item, 'wake': wake_status}, default=str)
        except ValueError as exc:
            return json.dumps({'error': str(exc)})
        finally:
            if hasattr(store, 'close'):
                store.close()

    create_schema = {
        'name': 'butler_create',
        'description': (
            'Record a responsibility, daily non-negotiable, proactive check-in, file watch, or open loop '
            'authorized by the user. Before creating, check the open list and reuse/update an item for the same outcome instead of duplicating it just to change its date or wording. If the user clearly requests a separate instance with the same title/outcome, set allow_duplicate=true. Omit due_at to be woken right away and choose the time yourself; you own follow-up timing (reschedule with butler_manage snooze). Persistence settings are preference hints, not schedules.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'description': 'Clear short description of the responsibility.'},
                'allow_duplicate': {
                    'type': 'boolean',
                    'description': 'Set true only when the user explicitly wants a separate instance with the same title/outcome; false by default.',
                },
                'due_at': {
                    'type': 'string',
                    'description': 'Optional ISO 8601 datetime with UTC offset for the first check-in/wake.',
                },
                'kind': {
                    'type': 'string',
                    'enum': list(_VALID_KINDS),
                    'description': 'Category: commitment (default), non_negotiable (daily reset), check_in, watch, or open_loop.',
                },
                'interval_seconds': {
                    'type': 'integer',
                    'description': 'Optional positive recurring interval in seconds.',
                },
                'deadline_at': {
                    'type': 'string',
                    'description': 'Optional ISO 8601 hard deadline with UTC offset; schedules proactive heads-up check-ins beforehand.',
                },
                'notes': {'type': 'string', 'description': 'Optional context notes.'},
                'prep_hint': {
                    'type': 'string',
                    'description': 'Optional concrete action/prep the assistant should perform before messaging the user.',
                },
                'watch_file': {
                    'type': 'string',
                    'description': 'Optional absolute file path to monitor for changes (sets kind=watch when omitted).',
                },
            },
            'required': ['title'],
            'additionalProperties': False,
        },
    }
    ctx.register_tool(
        name='butler_create',
        toolset='butler',
        schema=create_schema,
        handler=create,
        description=create_schema['description'],
    )

    def manage(args, **kwargs):
        args = args or {}
        action, item_id = args.get('action'), args.get('id')
        if action not in _VALID_MANAGE_ACTIONS:
            return json.dumps({'error': 'unsupported action'})
        if action != 'list' and (not isinstance(item_id, str) or not item_id.strip()):
            return json.dumps({'error': 'id is required'})

        when = None
        if action == 'snooze':
            if 'until' not in args:
                return json.dumps({'error': 'until must be an ISO 8601 datetime with timezone'})
            when, err = _parse_aware_iso(args.get('until'), 'until')
            if err:
                return json.dumps({'error': err})

        reopen_due = None
        if action == 'reopen' and args.get('due_at') is not None:
            reopen_due, err = _parse_aware_iso(args.get('due_at'), 'due_at')
            if err:
                return json.dumps({'error': err})

        store = factory()
        try:
            if action == 'list':
                items = store.list()
                status_filter = args.get('status_filter')
                kind_filter = args.get('kind_filter')
                if status_filter:
                    items = [x for x in items if x.get('status') == status_filter]
                if kind_filter:
                    items = [x for x in items if (x.get('kind') or 'commitment') == kind_filter]
                return json.dumps({'responsibilities': items}, default=str)

            if action == 'get':
                item = store.get(item_id)
                res = {'responsibility': item} if item else {'error': 'responsibility not found'}
                return json.dumps(res, default=str)

            if action == 'remove':
                if not hasattr(store, 'delete'):
                    return json.dumps({'error': 'remove not supported'})
                removed = bool(store.delete(item_id))
                res = {'removed': True, 'id': item_id} if removed else {'error': 'responsibility not found'}
                return json.dumps(res, default=str)

            if action == 'reopen':
                if not hasattr(store, 'reopen'):
                    return json.dumps({'error': 'reopen not supported'})
                changed = (
                    store.reopen(item_id, due_at=reopen_due)
                    if reopen_due is not None
                    else store.reopen(item_id)
                )
                res = (
                    {'responsibility': store.get(item_id)}
                    if changed
                    else {'error': 'responsibility not found or transition not allowed'}
                )
                return json.dumps(res, default=str)

            if action == 'complete':
                existing = store.get(item_id)
                if existing is None:
                    return json.dumps({'error': 'responsibility not found or transition not allowed'})
                if existing.get('kind') == 'non_negotiable' and hasattr(store, 'mark_non_negotiable_done'):
                    prof_dir = _resolve_profile_dir(store, directory)
                    try:
                        prof_settings = load_settings(prof_dir) if prof_dir is not None else defaults()
                    except Exception:
                        prof_settings = defaults()
                    eff_tz = existing.get('timezone') or resolve_effective_timezone(prof_settings)[0]
                    local_date_str = local_time(clock(), eff_tz).date().isoformat()
                    changed = bool(store.mark_non_negotiable_done(item_id, local_date_str))
                else:
                    changed = bool(store.complete(item_id))
                res = (
                    {'responsibility': store.get(item_id)}
                    if changed
                    else {'error': 'responsibility not found or transition not allowed'}
                )
                return json.dumps(res, default=str)

            if action == 'update':
                if not hasattr(store, 'update_item'):
                    return json.dumps({'error': 'update not supported'})
                existing = store.get(item_id)
                if existing is None:
                    return json.dumps({'error': 'responsibility not found'})
                fields = {}
                if 'title' in args:
                    if not isinstance(args['title'], str) or not args['title'].strip():
                        return json.dumps({'error': 'title must be a non-empty string'})
                    fields['title'] = args['title'].strip()
                if 'due_at' in args and args['due_at'] is not None:
                    parsed_due, err = _parse_aware_iso(args['due_at'], 'due_at')
                    if err:
                        return json.dumps({'error': err})
                    fields['due_at'] = parsed_due
                if 'deadline_at' in args:
                    if args['deadline_at'] in (None, ''):
                        fields['deadline_at'] = None
                    else:
                        parsed_dl, err = _parse_aware_iso(args['deadline_at'], 'deadline_at')
                        if err:
                            return json.dumps({'error': err})
                        fields['deadline_at'] = parsed_dl
                if 'kind' in args and args['kind'] is not None:
                    if args['kind'] not in _VALID_KINDS:
                        return json.dumps({'error': f'kind must be one of {", ".join(_VALID_KINDS)}'})
                    fields['kind'] = args['kind']
                if 'interval_seconds' in args:
                    iv = args['interval_seconds']
                    if iv is not None and (isinstance(iv, bool) or not isinstance(iv, int) or iv <= 0):
                        return json.dumps({'error': 'interval_seconds must be a positive integer or null'})
                    fields['interval_seconds'] = iv
                if 'notes' in args or 'prep_hint' in args:
                    meta = dict(existing.get('metadata') or {})
                    if 'notes' in args:
                        if args['notes']:
                            meta['notes'] = str(args['notes']).strip()
                        else:
                            meta.pop('notes', None)
                    if 'prep_hint' in args:
                        if args['prep_hint']:
                            meta['prep_hint'] = str(args['prep_hint']).strip()
                        else:
                            meta.pop('prep_hint', None)
                    fields['metadata'] = meta
                if not existing.get('route_id'):
                    prof_dir = _resolve_profile_dir(store, directory)
                    bound_id = _bind_authenticated_route(store, prof_dir)
                    if bound_id:
                        fields['route_id'] = bound_id
                if not fields:
                    return json.dumps({'error': 'no update fields provided'})
                changed = bool(store.update_item(item_id, **fields))
                res = (
                    {'responsibility': store.get(item_id)}
                    if changed
                    else {'error': 'responsibility not found or update rejected'}
                )
                return json.dumps(res, default=str)

            changed = store.snooze(item_id, when, notes=args.get('notes')) if action == 'snooze' else getattr(store, action)(item_id)
            res = (
                {'responsibility': store.get(item_id)}
                if changed
                else {'error': 'responsibility not found or transition not allowed'}
            )
            return json.dumps(res, default=str)
        except ValueError as exc:
            return json.dumps({'error': str(exc)})
        finally:
            if hasattr(store, 'close'):
                store.close()

    manage_schema = {
        'name': 'butler_manage',
        'description': 'Inspect or update responsibilities, non-negotiables, watches, or open loops at user/agent direction.',
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': list(_VALID_MANAGE_ACTIONS)},
                'id': {'type': 'string'},
                'until': {'type': 'string', 'description': 'Timezone-aware ISO 8601 datetime for snooze.'},
                'notes': {'type': 'string', 'description': 'For snooze: self-contained follow-up context (what is open, what happened, and what you will do/say next). For update: replace notes.'},
                'due_at': {'type': 'string', 'description': 'Optional timezone-aware ISO 8601 datetime for reopen or update.'},
                'deadline_at': {'type': 'string', 'description': 'Optional timezone-aware ISO 8601 deadline (or empty string to clear) for update.'},
                'title': {'type': 'string', 'description': 'Updated title for update.'},
                'kind': {'type': 'string', 'enum': list(_VALID_KINDS)},
                'interval_seconds': {'type': ['integer', 'null'], 'description': 'Updated recurring interval seconds for update.'},
                'prep_hint': {'type': 'string', 'description': 'Updated prep hint for update.'},
                'status_filter': {'type': 'string', 'description': 'Optional status filter for list.'},
                'kind_filter': {'type': 'string', 'description': 'Optional kind filter for list.'},
            },
            'required': ['action'],
            'additionalProperties': False,
        },
    }
    ctx.register_tool(
        name='butler_manage',
        toolset='butler',
        schema=manage_schema,
        handler=manage,
        description=manage_schema['description'],
    )
