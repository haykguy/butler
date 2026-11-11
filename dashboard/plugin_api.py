"""Authenticated Hermes dashboard API; profile settings, timezone onboarding, and Hermes load sync."""
import importlib.util
from pathlib import Path
import sqlite3
import sys

from fastapi import APIRouter, HTTPException
from plugins.plugin_storage import plugin_data_dir


def _load_butler_modules():
    pkg_dir = Path(__file__).resolve().parents[1] / 'butler'
    pkg_name = '_butler_dashboard_pkg'
    if pkg_name not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            pkg_name, pkg_dir / '__init__.py', submodule_search_locations=[str(pkg_dir)],
        )
        pkg_mod = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg_name] = pkg_mod
        pkg_spec.loader.exec_module(pkg_mod)

    loaded = {}
    for mod_name in ('settings', 'context', 'notifications'):
        full_name = f'{pkg_name}.{mod_name}'
        if full_name not in sys.modules:
            spec = importlib.util.spec_from_file_location(full_name, pkg_dir / f'{mod_name}.py')
            mod = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = mod
            spec.loader.exec_module(mod)
        loaded[mod_name] = sys.modules[full_name]
    return loaded['settings'], loaded['context'], loaded['notifications']


_settings, _context, _notifications = _load_butler_modules()
_config_loader = None
_config_saver = None
_sys_tz_fn = None
_ip_tz_fn = None
router = APIRouter()


def _detect_sys_tz():
    return (_sys_tz_fn or _context.detect_system_timezone)()


def _detect_ip_tz():
    return (_ip_tz_fn or _context.detect_ip_timezone)()


@router.get('/state')
def state():
    directory = plugin_data_dir('butler')
    settings = _settings.load(directory)
    live_cfg = _notifications.read_hermes_load_state(config_loader=_config_loader)
    sys_tz = _detect_sys_tz()
    eff_tz, eff_src = _context.resolve_effective_timezone(
        settings,
        config_timezone=live_cfg.get('timezone', '') if isinstance(live_cfg, dict) else '',
        system_timezone=sys_tz,
    )
    path = directory / 'responsibilities.sqlite3'
    rows = []
    if path.exists():
        # Never initialize or modify the scheduler database from the dashboard.
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(
                'SELECT id,title,due_at,timezone,status FROM responsibilities ORDER BY due_at,id LIMIT 500'
            )]
    return {
        'settings': settings,
        'responsibilities': rows,
        'responsibilities_limit': 500,
        'settings_enforced': bool(settings.get('applySystemControls')),
        'lifetime_awareness_active': bool(settings.get('lifetimeAwarenessEnabled', True) and not settings.get('paused')),
        'detected_system_timezone': sys_tz,
        'effective_timezone': eff_tz,
        'effective_timezone_source': eff_src,
        'hermes_live_config': live_cfg,
    }


@router.put('/settings')
def save_settings(data: dict):
    try:
        saved = _settings.save(plugin_data_dir('butler'), data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if saved.get('applySystemControls'):
        _notifications.apply_system_controls_to_hermes(
            saved, config_loader=_config_loader, config_saver=_config_saver,
        )
    return saved


@router.post('/detect-timezone')
def detect_timezone(payload: dict | None = None):
    use_ip = bool((payload or {}).get('use_ip', False))
    sys_tz = _detect_sys_tz()
    ip_tz = _detect_ip_tz() if use_ip else ''
    return {
        'detected_system_timezone': sys_tz,
        'detected_ip_timezone': ip_tz,
        'suggested_timezone': ip_tz or sys_tz,
    }


@router.post('/preset')
def apply_preset(payload: dict):
    preset = (payload or {}).get('preset')
    directory = plugin_data_dir('butler')
    current = _settings.load(directory)
    if preset == 'quiet_human':
        updated = _settings.quiet_human_preset(current)
        sys_tz = _detect_sys_tz()
        if not updated['timezone'] and sys_tz:
            updated['timezone'] = sys_tz
    elif preset == 'defaults':
        updated = _settings.defaults()
        updated['activityEnabled'] = current['activityEnabled']
        updated['notificationsEnabled'] = current['notificationsEnabled']
        updated['timezone'] = current['timezone']
    else:
        raise HTTPException(status_code=422, detail='preset must be quiet_human or defaults')
    saved = _settings.save(directory, updated)
    sync_result = (
        _notifications.apply_system_controls_to_hermes(
            saved, config_loader=_config_loader, config_saver=_config_saver,
        )
        if saved.get('applySystemControls') else {'applied': False, 'reason': 'applySystemControls is false'}
    )
    return {
        'settings': saved,
        'hermes_sync': sync_result,
        'hermes_live_config': _notifications.read_hermes_load_state(config_loader=_config_loader),
    }
