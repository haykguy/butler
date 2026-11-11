"""Live time/lifetime pre_llm_call hook and butler_controls agent tool."""
from datetime import datetime, timezone
import json
import logging

from .context import (
    build_lifetime_awareness_context,
    detect_ip_timezone,
    detect_system_timezone,
    resolve_effective_timezone,
)
from .notifications import apply_system_controls_to_hermes, read_hermes_load_state
from .settings import defaults, load, quiet_human_preset, save

log = logging.getLogger(__name__)

# Privacy boundary: never allow model tool calls to silently turn on plaintext DM
# event capture (activityEnabled). Users control DM capture explicitly in UI/settings.
_MODEL_MUTABLE_KEYS = frozenset(
    set(defaults()) - {'activityEnabled', 'notificationsEnabled'}
)

_CONTROLS_SCHEMA = {
    'type': 'object',
    'properties': {
        'action': {
            'type': 'string',
            'enum': ['status', 'detect_timezone', 'update', 'apply_preset'],
            'description': 'status reads active time & notification/load controls; detect_timezone checks local OS (and optional IP) IANA timezone for onboarding; update modifies allowed settings; apply_preset applies quiet_human or default controls.',
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
            'description': 'For update: partial Butler settings to update (e.g. timezone, lifetimeAwarenessEnabled, applySystemControls, suppressWarningNotifications, toolProgressMode, interimAssistantMessages, busyAckNotifications, longRunningNotifications, backgroundProcessNotifications, memoryNotifications, skillNudgesEnabled, gatewayRestartNotifications, gatewayMessageTimestamps, naturalStatusStyle, tone, persistence, quietEnabled, quietStart, quietEnd, dailyLimit, paused).',
        },
    },
    'required': ['action'],
    'additionalProperties': False,
}


def _default_directory():
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir('butler')


class AwarenessHooks:
    def __init__(self, directory=_default_directory, load_state=read_hermes_load_state,
                 now_fn=lambda: datetime.now(timezone.utc), system_tz_fn=detect_system_timezone):
        self.directory = directory
        self.load_state = load_state
        self.now_fn = now_fn
        self.system_tz_fn = system_tz_fn

    def pre_llm_call(self, *, conversation_history=None, is_first_turn=False,
                     platform='', parent_session_id='', **kwargs):
        # Skip delegated child subagent turns so child prompts stay clean.
        if parent_session_id:
            return None
        try:
            settings = load(self.directory())
            live_cfg = self.load_state()
            cfg_tz = live_cfg.get('timezone', '') if isinstance(live_cfg, dict) else ''
            text = build_lifetime_awareness_context(
                settings,
                now=self.now_fn(),
                conversation_history=conversation_history,
                is_first_turn=bool(is_first_turn),
                platform=platform or '',
                config_timezone=cfg_tz,
                system_timezone=self.system_tz_fn(),
            )
            if not text:
                return None
            return {'context': text}
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
):
    hooks = AwarenessHooks(
        directory=directory,
        load_state=lambda: read_hermes_load_state(config_loader=config_loader),
        system_tz_fn=system_tz_fn,
    )
    ctx.register_hook('pre_llm_call', hooks.pre_llm_call)

    def controls(args, **_):
        try:
            args = args or {}
            action = args.get('action')
            prof_dir = directory()
            current = load(prof_dir)
            live_cfg = read_hermes_load_state(config_loader=config_loader)
            sys_tz = system_tz_fn()

            if action == 'status':
                eff_tz, eff_src = resolve_effective_timezone(
                    current, config_timezone=live_cfg.get('timezone', ''), system_timezone=sys_tz,
                )
                return json.dumps({
                    'ok': True,
                    'effective_timezone': eff_tz,
                    'effective_timezone_source': eff_src,
                    'detected_system_timezone': sys_tz,
                    'settings': current,
                    'hermes_live_config': live_cfg,
                    'notes': {
                        'gateway_restart_banner': 'gatewayRestartNotifications=false suppresses Hermes static Gateway online startup/restart pings on configured platforms; Hermes core has no public custom template hook for that static banner, while naturalStatusStyle governs assistant-written status messages.',
                    },
                })

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
            'Inspect or update Butler live time/timezone awareness (including local OS or opt-in IP timezone detection for onboarding) '
            'and Hermes system notification / load controls (muting provider fallback/rate-limit warnings, tool progress spam, interim narration, '
            'skill creation nudges, memory notices, and static gateway restart banners).'
        ),
    )
    return hooks
