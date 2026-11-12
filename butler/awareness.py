"""Live time/lifetime pre_llm_call hook, live turn route capture, and butler_controls tool."""
from datetime import datetime, timezone
import inspect
import json
import logging
import threading
from pathlib import Path

from .context import (
    build_lifetime_awareness_context,
    detect_ip_timezone,
    detect_system_timezone,
    resolve_effective_timezone,
)
from .notifications import apply_system_controls_to_hermes, read_hermes_load_state
from .policy import Policy
from .followup import VISIBLE_WAKE_MARKER, WAKE_MARKER
from .settings import defaults, load, quiet_human_preset, save

log = logging.getLogger(__name__)

# Privacy boundary: never allow model tool calls to silently turn on plaintext DM
# event capture (activityEnabled) or background outreach (notificationsEnabled).
# Users control capture/wake switches explicitly in Butler Preferences.
_MODEL_MUTABLE_KEYS = frozenset(
    set(defaults()) - {'activityEnabled', 'notificationsEnabled', 'maxUnanswered', 'minWakeGapMinutes'}
)


def _setting_value_schema(key, value):
    """Expose validated model-mutable settings precisely to JSON-schema tool consumers."""
    if key == 'dailyLimit':
        return {'anyOf': [{'type': 'integer', 'minimum': 0, 'maximum': 1000}, {'type': 'null'}]}
    enums = {
        'tone': ['Gentle', 'Supportive', 'Balanced', 'Direct', 'Strict'],
        'persistence': ['Minimal', 'Occasional', 'Regular', 'Persistent'],
        'toolProgressMode': ['off', 'new', 'all'],
        'backgroundProcessNotifications': ['off', 'error', 'concise', 'result', 'all'],
        'memoryNotifications': ['off', 'on', 'verbose'],
    }
    if key in enums:
        return {'type': 'string', 'enum': enums[key]}
    if key in ('quietStart', 'quietEnd'):
        return {'type': 'string', 'pattern': r'^(?:|(?:[01][0-9]|2[0-3]):[0-5][0-9])$'}
    if key == 'timezone':
        return {'type': 'string', 'maxLength': 128}
    if isinstance(value, bool):
        return {'type': 'boolean'}
    if isinstance(value, int):
        return {'type': 'integer'}
    if isinstance(value, str):
        return {'type': 'string'}
    raise TypeError(f'No JSON schema for Butler setting {key}')


_SETTINGS_SCHEMA_PROPERTIES = {
    key: _setting_value_schema(key, value)
    for key, value in defaults().items()
    if key in _MODEL_MUTABLE_KEYS
}

_CONTROLS_SCHEMA = {
    'type': 'object',
    'properties': {
        'action': {
            'type': 'string',
            'enum': ['status', 'detect_timezone', 'update', 'apply_preset'],
            'description': 'status reads active time, cadence, quota & notification/load controls; detect_timezone checks local OS (and optional IP) IANA timezone for onboarding; update modifies allowed settings; apply_preset applies quiet_human or default controls.',
        },
        'use_ip': {
            'type': 'boolean',
            'description': 'For detect_timezone only: also query public IP timezone lookup when requested by the user during onboarding (default false).',
        },
        'preset': {
            'type': 'string',
            'enum': ['quiet_human', 'defaults'],
            'description': 'For apply_preset: quiet_human mutes noisy warnings, tool progress, interim narration, skill nudges, and restart banners while keeping live time awareness on.',
        },
        'settings': {
            'type': 'object',
            'description': 'For update: partial Butler settings to update.',
            'properties': _SETTINGS_SCHEMA_PROPERTIES,
            'additionalProperties': False,
            'minProperties': 1,
        },
    },
    'required': ['action'],
    'additionalProperties': False,
}


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


def _call_describe_cadence(settings: dict) -> dict:
    try:
        from . import followup as _followup
        return _followup.describe_cadence(settings)
    except ImportError:
        return {
            'tone': settings.get('tone', 'Balanced'),
            'persistence': settings.get('persistence', 'Occasional'),
            'quiet_enabled': bool(settings.get('quietEnabled', False)),
            'quiet_window': (
                f"{settings.get('quietStart', '')}-{settings.get('quietEnd', '')}"
                if settings.get('quietEnabled') else None
            ),
            'daily_limit': settings.get('dailyLimit'),
        }


def _quota_status(prof_dir: Path, settings: dict, now: datetime, effective_timezone: str) -> dict:
    policy = Policy(prof_dir)
    if hasattr(policy, 'wake_quota_status'):
        try:
            return policy.wake_quota_status(now, fallback_timezone=effective_timezone)
        except Exception:
            log.debug('Butler could not read wake quota status', exc_info=True)
    return {
        'notifications_enabled': bool(settings.get('notificationsEnabled', False)),
        'paused': bool(settings.get('paused', False)),
        'daily_limit': settings.get('dailyLimit'),
        'used_today': 0,
    }


capture_bound_turn_route = _call_capture_bound_turn_route
RouteRegistry = _open_route_registry
describe_cadence = _call_describe_cadence


def _default_directory():
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir('butler')


def _format_responsibilities_fallback(items: list[dict]) -> str:
    lines = [f'[Butler Active Responsibilities ({len(items)})]']
    for it in items[:8]:
        kind = it.get('kind') or 'commitment'
        due = it.get('due_at')
        due_str = due.isoformat() if hasattr(due, 'isoformat') else str(due or '')
        lines.append(f"- [{kind}] {it.get('title', '')} (id={it.get('id', '')}, due={due_str})")
    if len(items) > 8:
        lines.append(f'- ...and {len(items) - 8} more active items (use butler_manage action=list).')
    return '\n'.join(lines)


class AwarenessHooks:
    def __init__(
        self,
        directory=_default_directory,
        load_state=read_hermes_load_state,
        now_fn=lambda: datetime.now(timezone.utc),
        system_tz_fn=detect_system_timezone,
        *,
        on_bound_turn=None,
        store_factory=None,
        route_registry_factory=None,
    ):
        self.directory = directory
        self.load_state = load_state
        self.now_fn = now_fn
        self.system_tz_fn = system_tz_fn
        self.on_bound_turn = on_bound_turn
        self.store_factory = store_factory
        self.route_registry_factory = route_registry_factory
        self._wake_claims = {}
        self._wake_claims_lock = threading.Lock()

    def _capture_turn_route_and_start(self, prof_dir: Path) -> dict | None:
        route = capture_bound_turn_route()
        if not isinstance(route, dict) or not route.get('route_id'):
            return None
        try:
            prof_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            reg = (
                self.route_registry_factory()
                if self.route_registry_factory is not None
                else RouteRegistry(prof_dir / 'responsibilities.sqlite3')
            )
            if reg is not None:
                try:
                    reg.record_route(route, preferred=True)
                finally:
                    if hasattr(reg, 'close'):
                        reg.close()
        except Exception:
            log.debug('Butler could not record bound turn route', exc_info=True)
        if callable(self.on_bound_turn):
            try:
                self.on_bound_turn()
            except Exception:
                log.warning('Butler bound-turn starter failed', exc_info=True)
        return route

    def _load_active_responsibilities(self, prof_dir: Path, *, route_id=None) -> list[dict]:
        db_file = prof_dir / 'responsibilities.sqlite3'
        if self.store_factory is None and not db_file.exists():
            return []
        try:
            from .store import ResponsibilityStore
            store = self.store_factory() if self.store_factory is not None else ResponsibilityStore(db_file)
            try:
                return [
                    it for it in store.list()
                    if isinstance(it, dict)
                    and it.get('status') in ('active', 'submitted', 'paused')
                    and (it.get('route_id') == route_id if route_id else not it.get('route_id'))
                ]
            finally:
                if hasattr(store, 'close'):
                    store.close()
        except Exception:
            log.debug('Butler could not read active responsibilities', exc_info=True)
            return []

    def _track_engagement(self, prof_dir: Path, user_message) -> dict | None:
        """A real user message resets the unanswered-nudge counter; Butler's own wake prompts do not."""
        db_file = prof_dir / 'responsibilities.sqlite3'
        if self.store_factory is None and not db_file.exists():
            return None
        try:
            from .store import ResponsibilityStore
            store = self.store_factory() if self.store_factory is not None else ResponsibilityStore(db_file)
            try:
                text = user_message if isinstance(user_message, str) else ''
                if text.strip() and text.strip() != VISIBLE_WAKE_MARKER and not text.lstrip().startswith(WAKE_MARKER):
                    store.note_user_reply(self.now_fn())
                return store.get_engagement()
            finally:
                if hasattr(store, 'close'):
                    store.close()
        except Exception:
            log.debug('Butler could not track engagement', exc_info=True)
            return None

    @staticmethod
    def _wake_claim_key(session_id, turn_id, route_id):
        if turn_id:
            return f'turn:{turn_id}'
        return str(session_id or (f'route:{route_id}' if route_id else ''))

    def post_llm_call(self, *, user_message=None, assistant_response=None, session_id=None, turn_id=None, **kwargs):
        """Finalize a hidden wake after its model turn; count only non-silent assistant output."""
        text = user_message if isinstance(user_message, str) else ''
        if text.strip() != VISIBLE_WAKE_MARKER:
            return None
        route = capture_bound_turn_route()
        route_id = route.get('route_id') if isinstance(route, dict) else None
        claim_key = self._wake_claim_key(session_id, turn_id, route_id)
        if not claim_key:
            return None
        with self._wake_claims_lock:
            claim = self._wake_claims.pop(claim_key, None)
        if not claim:
            return None
        response = assistant_response if isinstance(assistant_response, str) else ''
        visible = bool(response.strip()) and response.strip().upper() != '[SILENT]'
        try:
            from .store import ResponsibilityStore
            prof_dir = Path(self.directory())
            store = self.store_factory() if self.store_factory is not None else ResponsibilityStore(prof_dir / 'responsibilities.sqlite3')
            try:
                store.finish_pending_wake(claim['id'], claim['claim_token'], self.now_fn(), visible_response=visible)
            finally:
                if hasattr(store, 'close'):
                    store.close()
        except Exception:
            log.debug('Butler could not finalize its proactive response', exc_info=True)
        return None

    def pre_llm_call(
        self,
        *,
        conversation_history=None,
        is_first_turn=False,
        platform='',
        parent_session_id='',
        user_message=None,
        session_id=None,
        turn_id=None,
        **kwargs,
    ):
        # Skip delegated child subagent turns so child prompts stay clean.
        if parent_session_id:
            return None
        try:
            prof_dir = Path(self.directory())
            current_route = self._capture_turn_route_and_start(prof_dir)
            route_id = current_route.get('route_id') if isinstance(current_route, dict) else None
            settings = load(prof_dir)
            live_cfg = self.load_state()
            cfg_tz = live_cfg.get('timezone', '') if isinstance(live_cfg, dict) else ''
            active_items = self._load_active_responsibilities(prof_dir, route_id=route_id)
            engagement = self._track_engagement(prof_dir, user_message)
            pending = None
            if isinstance(user_message, str) and user_message.strip() == VISIBLE_WAKE_MARKER and route_id:
                from .store import ResponsibilityStore
                store = self.store_factory() if self.store_factory is not None else ResponsibilityStore(prof_dir / 'responsibilities.sqlite3')
                try:
                    pending = store.claim_pending_wake(route_id, self.now_fn())
                finally:
                    if hasattr(store, 'close'):
                        store.close()
                if pending:
                    claim_key = self._wake_claim_key(session_id, turn_id, route_id)
                    if claim_key:
                        with self._wake_claims_lock:
                            self._wake_claims[claim_key] = {
                                'id': pending['id'],
                                'claim_token': pending['claim_token'],
                            }

            ctx_kwargs = dict(
                now=self.now_fn(),
                conversation_history=conversation_history,
                is_first_turn=bool(is_first_turn),
                platform=platform or '',
                config_timezone=cfg_tz,
                system_timezone=self.system_tz_fn(),
            )
            accepts_active = False
            try:
                sig = inspect.signature(build_lifetime_awareness_context)
                accepts_active = (
                    'active_responsibilities' in sig.parameters
                    or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                )
            except (TypeError, ValueError):
                accepts_active = False
            if accepts_active:
                # Always pass even an empty list: this injects the after-every-reply
                # directive, and lets submitted items surface as awaiting a decision.
                ctx_kwargs['active_responsibilities'] = active_items
                ctx_kwargs['engagement'] = engagement

            text = build_lifetime_awareness_context(settings, **ctx_kwargs) or ''
            if active_items and not accepts_active:
                text = (text + '\n' if text else '') + _format_responsibilities_fallback(active_items)
            if pending:
                text = (text + '\n\n' if text else '') + pending['payload']
            return {'context': text} if text else None
        except Exception:
            log.exception('Butler could not build lifetime awareness context')
            return None


def register_awareness_and_controls(
    ctx,
    *,
    directory=_default_directory,
    config_loader=None,
    config_saver=None,
    ip_tz_fn=detect_ip_timezone,
    system_tz_fn=detect_system_timezone,
    now_fn=lambda: datetime.now(timezone.utc),
    on_bound_turn=None,
    store_factory=None,
    route_registry_factory=None,
):
    hooks = AwarenessHooks(
        directory=directory,
        load_state=lambda: read_hermes_load_state(config_loader=config_loader),
        now_fn=now_fn,
        system_tz_fn=system_tz_fn,
        on_bound_turn=on_bound_turn,
        store_factory=store_factory,
        route_registry_factory=route_registry_factory,
    )
    ctx.register_hook('pre_llm_call', hooks.pre_llm_call)
    ctx.register_hook('post_llm_call', hooks.post_llm_call)

    def controls(args, **_):
        try:
            args = args or {}
            action = args.get('action')
            prof_dir = Path(directory())
            current = load(prof_dir)
            live_cfg = read_hermes_load_state(config_loader=config_loader)
            sys_tz = system_tz_fn()

            if action == 'status':
                eff_tz, eff_src = resolve_effective_timezone(
                    current, config_timezone=live_cfg.get('timezone', ''), system_timezone=sys_tz,
                )
                now = now_fn()
                return json.dumps({
                    'ok': True,
                    'effective_timezone': eff_tz,
                    'effective_timezone_source': eff_src,
                    'detected_system_timezone': sys_tz,
                    'settings': current,
                    'cadence': describe_cadence(current),
                    'quota': _quota_status(prof_dir, current, now, eff_tz),
                    'hermes_live_config': live_cfg,
                    'notes': {
                        'gateway_restart_banner': 'gatewayRestartNotifications=false suppresses Hermes static Gateway online startup/restart pings on configured platforms; Hermes core has no public custom template hook for that static banner, while naturalStatusStyle governs assistant-written status messages.',
                    },
                }, default=str)

            if action == 'detect_timezone':
                use_ip = bool(args.get('use_ip', False))
                ip_tz = ip_tz_fn() if use_ip else ''
                eff_tz, eff_src = resolve_effective_timezone(
                    current, config_timezone=live_cfg.get('timezone', ''), system_timezone=sys_tz,
                )
                return json.dumps({
                    'ok': True,
                    'configured_timezone': current.get('timezone', ''),
                    'detected_system_timezone': sys_tz,
                    'detected_ip_timezone': ip_tz,
                    'effective_timezone': eff_tz,
                    'effective_timezone_source': eff_src,
                })

            if action == 'apply_preset':
                preset = args.get('preset')
                if preset == 'quiet_human':
                    updated = quiet_human_preset(current)
                    if not updated['timezone'] and sys_tz:
                        updated['timezone'] = sys_tz
                elif preset == 'defaults':
                    updated = defaults()
                    updated['activityEnabled'] = current['activityEnabled']
                    updated['notificationsEnabled'] = current['notificationsEnabled']
                    updated['timezone'] = current['timezone']
                else:
                    return json.dumps({'ok': False, 'error': 'preset must be quiet_human or defaults'})
                saved = save(prof_dir, updated)
                sync_result = (
                    apply_system_controls_to_hermes(saved, config_loader=config_loader, config_saver=config_saver)
                    if saved['applySystemControls'] else {'applied': False, 'reason': 'applySystemControls is false'}
                )
                return json.dumps({'ok': True, 'settings': saved, 'hermes_sync': sync_result})

            if action == 'update':
                patch = args.get('settings')
                if not isinstance(patch, dict) or not patch:
                    return json.dumps({'ok': False, 'error': 'settings must be a non-empty object'})
                forbidden = sorted(set(patch) - _MODEL_MUTABLE_KEYS)
                if forbidden:
                    return json.dumps({
                        'ok': False,
                        'error': f'Disallowed or unknown setting(s): {", ".join(forbidden)}. Privacy-gated capture/wake switches must be changed by the user in Butler Preferences.',
                    })
                candidate = dict(current)
                candidate.update(patch)
                saved = save(prof_dir, candidate)
                sync_result = (
                    apply_system_controls_to_hermes(saved, config_loader=config_loader, config_saver=config_saver)
                    if saved['applySystemControls'] else {'applied': False, 'reason': 'applySystemControls is false (set applySystemControls=true to write to Hermes config.yaml)'}
                )
                return json.dumps({'ok': True, 'settings': saved, 'hermes_sync': sync_result})

            return json.dumps({'ok': False, 'error': 'Unsupported action'})
        except ValueError as exc:
            return json.dumps({'ok': False, 'error': str(exc)})

    ctx.register_tool(
        name='butler_controls',
        toolset='butler',
        schema=_CONTROLS_SCHEMA,
        handler=controls,
        description=(
            'Inspect or update Butler live time/timezone awareness (including local OS or opt-in IP timezone detection for onboarding), '
            'proactive follow-up cadence/quota, and Hermes system notification / load controls (muting provider fallback/rate-limit warnings, '
            'tool progress spam, interim narration, skill creation nudges, memory notices, and static gateway restart banners).'
        ),
    )
    return hooks
