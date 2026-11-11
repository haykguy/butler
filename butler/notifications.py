"""Notification policy, load/bloat controls, and public Hermes config sync."""
from dataclasses import dataclass
from datetime import datetime, time

from .context import local_time


@dataclass(frozen=True)
class Notification:
    text: str
    safety: bool = False


@dataclass(frozen=True)
class NotificationPreferences:
    enabled: bool = True
    quiet_start: time | None = None
    quiet_end: time | None = None
    tone: str = 'natural'
    persistence: str = 'moderate'

    def __post_init__(self):
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError('quiet_start and quiet_end must be supplied together')
        if self.quiet_start is not None:
            if self.quiet_start.tzinfo is not None or self.quiet_end.tzinfo is not None:
                raise ValueError('quiet hours must be local wall times')
            if self.quiet_start == self.quiet_end:
                raise ValueError('quiet hours start and end must differ')


def should_notify(notice: Notification, preferences: NotificationPreferences,
                  now: datetime, timezone: str) -> bool:
    """Safety notices bypass preference suppression; delivery is separate."""
    if notice.safety:
        return True
    if not preferences.enabled:
        return False
    if preferences.quiet_start is None:
        return True
    current = local_time(now, timezone).time()
    start, end = preferences.quiet_start, preferences.quiet_end
    quiet = start <= current < end if start < end else current >= start or current < end
    return not quiet


def build_hermes_config_patch(settings: dict, existing_config: dict | None = None) -> tuple[dict, set[tuple[str, ...]]]:
    """Build a partial Hermes config dict and preserve_keys set from Butler controls."""
    existing = existing_config if isinstance(existing_config, dict) else {}
    suppress_warn = bool(settings.get('suppressWarningNotifications', True))
    tool_progress = settings.get('toolProgressMode', 'off')
    interim = bool(settings.get('interimAssistantMessages', False))
    busy_ack = bool(settings.get('busyAckNotifications', False))
    long_running = bool(settings.get('longRunningNotifications', False))
    bg_proc = settings.get('backgroundProcessNotifications', 'error')
    mem_notif = settings.get('memoryNotifications', 'off')
    skill_nudges = bool(settings.get('skillNudgesEnabled', False))
    restart_banner = bool(settings.get('gatewayRestartNotifications', False))
    msg_timestamps = bool(settings.get('gatewayMessageTimestamps', True))
    natural_style = bool(settings.get('naturalStatusStyle', True))

    display_patch: dict = {
        'suppress_warning_notifications': suppress_warn,
        'credits_notices': not suppress_warn,
        'tool_progress': tool_progress,
        'interim_assistant_messages': interim,
        'show_commentary': interim,
        'busy_ack_enabled': busy_ack,
        'busy_ack_detail': busy_ack,
        'busy_steer_ack_enabled': busy_ack,
        'long_running_notifications': long_running,
        'background_process_notifications': bg_proc,
        'memory_notifications': mem_notif,
        'ephemeral_system_ttl': 15 if natural_style else 0,
    }
    preserve: set[tuple[str, ...]] = {
        ('display', key) for key in display_patch
    }

    # Keep existing per-platform display overrides aligned so platform-level
    # blocks (e.g. photon/telegram) do not contradict the user's explicit choice.
    existing_display = existing.get('display')
    existing_disp_platforms = (
        existing_display.get('platforms')
        if isinstance(existing_display, dict) and isinstance(existing_display.get('platforms'), dict)
        else {}
    )
    if existing_disp_platforms:
        plat_overrides = {}
        for plat_name, plat_cfg in existing_disp_platforms.items():
            if not isinstance(plat_cfg, dict):
                continue
            updated_plat = {
                'suppress_warning_notifications': suppress_warn,
                'tool_progress': tool_progress,
                'interim_assistant_messages': interim,
                'busy_ack_detail': busy_ack,
                'long_running_notifications': long_running,
                'memory_notifications': mem_notif,
            }
            plat_overrides[plat_name] = updated_plat
            for k in updated_plat:
                preserve.add(('display', 'platforms', plat_name, k))
        if plat_overrides:
            display_patch['platforms'] = plat_overrides

    patch: dict = {
        'display': display_patch,
        'agent': {
            'gateway_notify_interval': 180 if long_running else 0,
        },
        'skills': {
            'creation_nudge_interval': 15 if skill_nudges else 0,
        },
        'gateway': {
            'message_timestamps': {'enabled': msg_timestamps},
        },
    }
    preserve.add(('agent', 'gateway_notify_interval'))
    preserve.add(('skills', 'creation_nudge_interval'))
    preserve.add(('gateway', 'message_timestamps', 'enabled'))

    tz = (settings.get('timezone') or '').strip()
    if tz:
        patch['timezone'] = tz
        preserve.add(('timezone',))

    # Apply gateway_restart_notification across configured messaging platforms.
    existing_platforms = existing.get('platforms')
    if isinstance(existing_platforms, dict) and existing_platforms:
        platforms_patch = {}
        for plat_name, plat_cfg in existing_platforms.items():
            if isinstance(plat_cfg, dict):
                platforms_patch[plat_name] = {'gateway_restart_notification': restart_banner}
                preserve.add(('platforms', plat_name, 'gateway_restart_notification'))
        if platforms_patch:
            patch['platforms'] = platforms_patch

    return patch, preserve


def summarize_hermes_config(config: dict | None) -> dict:
    """Extract a safe, non-secret summary of live Hermes notification/load config."""
    if not isinstance(config, dict):
        return {'available': False}
    display = config.get('display') if isinstance(config.get('display'), dict) else {}
    skills = config.get('skills') if isinstance(config.get('skills'), dict) else {}
    gateway = config.get('gateway') if isinstance(config.get('gateway'), dict) else {}
    agent = config.get('agent') if isinstance(config.get('agent'), dict) else {}
    msg_ts = gateway.get('message_timestamps') if isinstance(gateway.get('message_timestamps'), dict) else {}
    platforms = config.get('platforms') if isinstance(config.get('platforms'), dict) else {}
    restart_by_platform = {
        name: bool(pcfg.get('gateway_restart_notification', True))
        for name, pcfg in platforms.items()
        if isinstance(pcfg, dict)
    }
    return {
        'available': True,
        'timezone': config.get('timezone') or '',
        'suppress_warning_notifications': bool(display.get('suppress_warning_notifications', False)),
        'credits_notices': bool(display.get('credits_notices', True)),
        'tool_progress': display.get('tool_progress', 'all'),
        'interim_assistant_messages': bool(display.get('interim_assistant_messages', True)),
        'busy_ack_enabled': bool(display.get('busy_ack_enabled', True)),
        'long_running_notifications': bool(display.get('long_running_notifications', True)),
        'gateway_notify_interval': agent.get('gateway_notify_interval', 180),
        'background_process_notifications': display.get('background_process_notifications', 'concise'),
        'memory_notifications': display.get('memory_notifications', 'on'),
        'skill_creation_nudge_interval': skills.get('creation_nudge_interval', 15),
        'ephemeral_system_ttl': display.get('ephemeral_system_ttl', 0),
        'gateway_message_timestamps': bool(msg_ts.get('enabled', False)),
        'gateway_restart_notification_by_platform': restart_by_platform,
    }


def read_hermes_load_state(*, config_loader=None) -> dict:
    """Read live Hermes notification/load settings via the public config API."""
    loader = config_loader
    if loader is None:
        try:
            from hermes_cli.config import load_config as loader
        except ImportError:
            return {'available': False}
    try:
        return summarize_hermes_config(loader())
    except Exception:
        return {'available': False}


def apply_system_controls_to_hermes(settings: dict, *, config_loader=None, config_saver=None) -> dict:
    """Write Butler notification/load controls into the active profile's config.yaml."""
    loader, saver = config_loader, config_saver
    if loader is None or saver is None:
        try:
            from hermes_cli.config import load_config as _load, save_config as _save
            loader = loader or _load
            saver = saver or _save
        except ImportError:
            return {'applied': False, 'reason': 'hermes_cli.config unavailable', 'live_state': {'available': False}}
    current = loader()
    patch, preserve = build_hermes_config_patch(settings, current)
    saver(patch, preserve_keys=preserve, merge_existing=True)
    updated = loader()
    return {
        'applied': True,
        'updated_keys': sorted('.'.join(p) for p in preserve),
        'live_state': summarize_hermes_config(updated),
    }
