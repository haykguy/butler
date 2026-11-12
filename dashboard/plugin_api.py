"""Authenticated Hermes dashboard API; profile settings, responsibilities, quick setup, and live check-ins."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from plugins.plugin_storage import plugin_data_dir

UTC = timezone.utc
_PRIVATE_META_KEYS = frozenset({'session_key', 'origin', 'token', 'claim_token'})


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
    for mod_name in (
        'settings', 'context', 'notifications', 'store',
        'routes', 'policy', 'followup', 'runner', 'prompt_review',
    ):
        full_name = f'{pkg_name}.{mod_name}'
        mod_file = pkg_dir / f'{mod_name}.py'
        if full_name not in sys.modules and mod_file.exists():
            try:
                spec = importlib.util.spec_from_file_location(full_name, mod_file)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[full_name] = mod
                spec.loader.exec_module(mod)
            except Exception:
                loaded[mod_name] = None
                continue
        loaded[mod_name] = sys.modules.get(full_name)
    return loaded


_MODS = _load_butler_modules()
_settings = _MODS.get('settings')
_context = _MODS.get('context')
_notifications = _MODS.get('notifications')
_store = _MODS.get('store')
_routes = _MODS.get('routes')
_policy = _MODS.get('policy')
_followup = _MODS.get('followup')
_runner = _MODS.get('runner')
_prompt_review = _MODS.get('prompt_review')

_config_loader = None
_config_saver = None
_sys_tz_fn = None
_ip_tz_fn = None
_now_fn = None
_state_db_path_fn = None

router = APIRouter()


def _register_route(method: str, path: str):
    def decorator(fn):
        getattr(router, method)(path)(fn)
        getattr(router, method)(f'/api/plugins/butler{path}', include_in_schema=False)(fn)
        return fn
    return decorator


@_register_route('get', '/prompt-review')
def prompt_review_get():
    if _prompt_review is None:
        raise HTTPException(status_code=503, detail='Prompt review unavailable')
    from hermes_constants import get_hermes_home
    return _prompt_review.review(Path(get_hermes_home()))


@_register_route('post', '/prompt-review/apply')
def prompt_review_apply(payload: dict):
    items = payload.get('items')
    if set(payload) != {'items'} or not isinstance(items, list) or not items:
        raise HTTPException(status_code=422, detail='Select at least one prompt review item')
    ids = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'id', 'sha256', 'proposed'}
                or not isinstance(item['id'], str) or not item['id']
                or not isinstance(item['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                or not isinstance(item['proposed'], str) or item['id'] in ids):
            raise HTTPException(status_code=422, detail='Invalid prompt review selection')
        ids.add(item['id'])
    if _prompt_review is None:
        raise HTTPException(status_code=503, detail='Prompt review unavailable')
    from hermes_constants import get_hermes_home
    try:
        return _prompt_review.apply(Path(get_hermes_home()), items)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _now() -> datetime:
    if _now_fn is not None:
        val = _now_fn()
        return val.astimezone(UTC) if val.tzinfo else val.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _detect_sys_tz() -> str:
    fn = _sys_tz_fn or getattr(_context, 'detect_system_timezone', lambda: '')
    try:
        return fn() or ''
    except Exception:
        return ''


def _detect_ip_tz() -> str:
    fn = _ip_tz_fn or getattr(_context, 'detect_ip_timezone', lambda: '')
    try:
        return fn() or ''
    except Exception:
        return ''


def _safe_zone(tz_name: str) -> ZoneInfo:
    cand = (tz_name or 'UTC').strip().lstrip(':').strip() or 'UTC'
    try:
        return ZoneInfo(cand)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo('UTC')


def _local_today_str(now_utc: datetime, eff_tz: str) -> str:
    local_fn = getattr(_context, 'local_time', None)
    if callable(local_fn):
        try:
            return local_fn(now_utc, eff_tz or 'UTC').date().isoformat()
        except Exception:
            pass
    return now_utc.astimezone(_safe_zone(eff_tz)).date().isoformat()


def _load_raw_hermes_config() -> dict | None:
    loader = _config_loader
    if loader is None:
        try:
            from hermes_cli.config import load_config as loader
        except ImportError:
            return None
    try:
        cfg = loader()
        return cfg if isinstance(cfg, dict) else None
    except Exception:
        return None


def _butler_plugin_flags(cfg: dict | None) -> tuple[bool, bool]:
    """Return (plugin_enabled, allow_gateway_injection) from Hermes config."""
    if not isinstance(cfg, dict):
        return True, False
    plugins = cfg.get('plugins')
    if not isinstance(plugins, dict):
        return True, False
    entries = plugins.get('entries')
    entry = entries.get('butler') if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        fallback = plugins.get('butler')
        entry = fallback if isinstance(fallback, dict) else {}
    enabled = entry.get('enabled', True) is not False
    allow_gw = entry.get('allow_gateway_injection') is True
    return enabled, allow_gw


def _enable_butler_in_hermes_config() -> dict:
    loader, saver = _config_loader, _config_saver
    if loader is None or saver is None:
        try:
            from hermes_cli.config import load_config as _load, save_config as _save
            loader = loader or _load
            saver = saver or _save
        except ImportError:
            return {'applied': False, 'reason': 'hermes_cli.config unavailable'}
    try:
        current = loader()
        if not isinstance(current, dict):
            current = {}
        existing_plugins = current.get('plugins') if isinstance(current.get('plugins'), dict) else {}
        existing_entries = (
            dict(existing_plugins.get('entries'))
            if isinstance(existing_plugins.get('entries'), dict) else {}
        )
        existing_butler = (
            dict(existing_entries.get('butler'))
            if isinstance(existing_entries.get('butler'), dict) else {}
        )
        existing_butler['enabled'] = True
        existing_butler['allow_gateway_injection'] = True
        existing_entries['butler'] = existing_butler
        plugins_patch = dict(existing_plugins)
        plugins_patch['entries'] = existing_entries
        patch = {'plugins': plugins_patch}
        preserve = {
            ('plugins', 'entries', 'butler', 'enabled'),
            ('plugins', 'entries', 'butler', 'allow_gateway_injection'),
        }
        try:
            saver(patch, preserve_keys=preserve, merge_existing=True)
        except TypeError:
            saver(patch)
        return {'applied': True, 'allow_gateway_injection': True, 'plugin_enabled': True}
    except Exception as exc:
        return {'applied': False, 'reason': str(exc)}


def _resolve_profile_state_db(directory: Path) -> Path | None:
    if _state_db_path_fn is not None:
        custom = _state_db_path_fn()
        return Path(custom) if custom else None
    if directory.parent.name == 'plugin-data':
        cand = directory.parent.parent / 'state.db'
        return cand if cand.exists() else None
    local_cand = directory / 'state.db'
    return local_cand if local_cand.exists() else None


def _discover_route_from_state_db(state_db: Path) -> dict | None:
    if not state_db or not state_db.exists():
        return None
    try:
        with sqlite3.connect(state_db.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            has_sessions = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            if not has_sessions:
                return None
            cols = {r['name'] for r in db.execute('PRAGMA table_info(sessions)').fetchall()}
            select_cols = [c for c in ('id', 'source', 'session_key', 'ended_at', 'last_activity_at', 'started_at') if c in cols]
            if not select_cols or ('session_key' not in cols and 'id' not in cols):
                return None
            order_parts = []
            if 'ended_at' in cols:
                order_parts.append('CASE WHEN ended_at IS NULL THEN 0 ELSE 1 END ASC')
            if 'session_key' in cols:
                order_parts.append("CASE WHEN session_key IS NOT NULL AND TRIM(session_key) != '' THEN 0 ELSE 1 END ASC")
            if 'last_activity_at' in cols and 'started_at' in cols:
                order_parts.append('COALESCE(last_activity_at, started_at, 0) DESC')
            elif 'last_activity_at' in cols:
                order_parts.append('COALESCE(last_activity_at, 0) DESC')
            elif 'started_at' in cols:
                order_parts.append('COALESCE(started_at, 0) DESC')
            order_sql = f" ORDER BY {', '.join(order_parts)}" if order_parts else ''
            rows = db.execute(f"SELECT {', '.join(select_cols)} FROM sessions{order_sql} LIMIT 15").fetchall()
            for row in rows:
                rdict = dict(row)
                source = str(rdict.get('source') or 'desktop').strip() or 'desktop'
                if source == 'cron':
                    continue
                sk = str(rdict.get('session_key') or '').strip()
                if not sk and source in ('desktop', 'tui', 'cli', 'local', 'webapp'):
                    sk = str(rdict.get('id') or '').strip()
                if not sk:
                    continue
                # Never auto-bind proactive wakes to a group/channel/thread or
                # another platform user's session: only local surfaces or DM keys.
                if source not in ('desktop', 'tui', 'cli', 'local', 'webapp') and ':dm:' not in f':{sk}:':
                    continue
                route_id = 'sk:' + hashlib.sha256(sk.encode('utf-8')).hexdigest()[:16]
                return {
                    'route_id': route_id,
                    'kind': 'session_key',
                    'session_key': sk,
                    'origin': None,
                    'platform': source,
                }
    except Exception:
        return None
    return None


def _ensure_active_route(directory: Path, reg) -> dict | None:
    if reg is None:
        return None
    try:
        pref = reg.preferred_route()
        if pref is not None:
            return pref
    except Exception:
        pref = None
    discovered = _discover_route_from_state_db(_resolve_profile_state_db(directory))
    if discovered is not None:
        try:
            reg.record_route(discovered, preferred=True)
            return reg.preferred_route() or discovered
        except Exception:
            return discovered
    return None


def _iso_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def _parse_metadata(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _serialize_responsibility(raw_item: dict, local_today_iso: str) -> dict:
    meta = _parse_metadata(raw_item.get('metadata'))
    safe_details = {k: v for k, v in meta.items() if k not in _PRIVATE_META_KEYS}
    kind = str(raw_item.get('kind') or 'commitment')
    status = str(raw_item.get('status') or 'active')
    completed_day = raw_item.get('completed_day')
    if kind == 'non_negotiable':
        done_today = bool(completed_day == local_today_iso or status == 'completed')
    else:
        done_today = bool(status == 'completed')
    return {
        'id': raw_item['id'],
        'title': raw_item['title'],
        'due_at': _iso_or_none(raw_item.get('due_at')),
        'timezone': raw_item.get('timezone') or 'UTC',
        'status': status,
        'kind': kind,
        'interval_seconds': raw_item.get('interval_seconds'),
        'route_id': raw_item.get('route_id'),
        'stage': int(-1 if raw_item.get('stage') is None else raw_item.get('stage')),
        'nudge_count': int(raw_item.get('nudge_count') or 0),
        'last_nudged_at': _iso_or_none(raw_item.get('last_nudged_at')),
        'deadline_at': _iso_or_none(raw_item.get('deadline_at')),
        'completed_day': completed_day,
        'done_today': done_today,
        'notes': str(meta.get('notes') or ''),
        'prep_hint': str(meta.get('prep_hint') or ''),
        'details': safe_details,
    }


def _sanitize_route(route: dict) -> dict:
    return {
        'route_id': route.get('route_id'),
        'kind': route.get('kind'),
        'platform': route.get('platform') or 'local',
        'updated_at': route.get('updated_at'),
        'is_preferred': bool(route.get('is_preferred')),
    }


def _parse_client_datetime(raw_val, eff_tz: str) -> datetime | None:
    if raw_val is None or raw_val == '':
        return None
    if isinstance(raw_val, datetime):
        return raw_val.astimezone(UTC) if raw_val.tzinfo else raw_val.replace(tzinfo=_safe_zone(eff_tz)).astimezone(UTC)
    if not isinstance(raw_val, str):
        raise ValueError('Datetime must be an ISO string')
    text = raw_val.strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_safe_zone(eff_tz))
    return parsed.astimezone(UTC)


def _fallback_compute_schedule(kind: str, now: datetime, settings: dict, eff_tz: str, due_at=None, deadline_at=None, interval_seconds=None) -> dict:
    fn = getattr(_followup, 'compute_initial_schedule', None)
    if callable(fn):
        return fn(
            kind, now, settings,
            effective_timezone=eff_tz or 'UTC',
            due_at=due_at, deadline_at=deadline_at,
            interval_seconds=interval_seconds,
        )
    return {
        'due_at': due_at or now,
        'deadline_at': deadline_at,
        'interval_seconds': interval_seconds,
        'schedule_reason': 'due_now_model_decides',
    }


def _fallback_describe_cadence(settings: dict) -> dict:
    fn = getattr(_followup, 'describe_cadence', None)
    if callable(fn):
        return fn(settings)
    tone = settings.get('tone', 'Balanced')
    persistence = settings.get('persistence', 'Occasional')
    return {
        'tone': tone,
        'tone_guide': f'{tone} coaching tone.',
        'persistence': persistence,
        'summary': f'{tone} tone · {persistence} preference',
        'non_negotiable_cadence': f'{persistence} preference hint; model chooses timing',
        'commitment_cadence': f'{persistence} preference hint; model chooses timing',
    }


def _fallback_quota(directory: Path, now: datetime, eff_tz: str, settings: dict) -> dict:
    policy_cls = getattr(_policy, 'Policy', None)
    if policy_cls is not None:
        try:
            return policy_cls(directory).wake_quota_status(now, fallback_timezone=eff_tz or 'UTC')
        except Exception:
            pass
    return {
        'used_today': 0,
        'daily_limit': settings.get('dailyLimit'),
        'remaining_today': settings.get('dailyLimit'),
        'in_quiet_hours': False,
        'quiet_until': None,
        'paused': bool(settings.get('paused')),
        'notifications_enabled': bool(settings.get('notificationsEnabled')),
        'allowed_now': bool(settings.get('notificationsEnabled') and not settings.get('paused')),
        'reason': 'ok',
    }


def _resolve_context(directory: Path) -> tuple[dict, dict, str, str, str, dict | None]:
    settings = _settings.load(directory)
    raw_cfg = _load_raw_hermes_config()
    live_cfg = _notifications.read_hermes_load_state(config_loader=_config_loader)
    sys_tz = _detect_sys_tz()
    eff_tz, eff_src = _context.resolve_effective_timezone(
        settings,
        config_timezone=live_cfg.get('timezone', '') if isinstance(live_cfg, dict) else '',
        system_timezone=sys_tz,
    )
    return settings, live_cfg, sys_tz, eff_tz, eff_src, raw_cfg


def _find_active_runner(directory: Path):
    pkg_runner_file = (Path(__file__).resolve().parents[1] / 'butler' / 'runner.py').resolve()
    candidates = []
    if _runner is not None:
        candidates.append(_runner)
    for mod in list(sys.modules.values()):
        mod_file = getattr(mod, '__file__', None)
        if not mod_file:
            continue
        try:
            if Path(mod_file).resolve() == pkg_runner_file and mod not in candidates:
                candidates.append(mod)
        except OSError:
            continue
    for mod in candidates:
        getter = getattr(mod, 'get_active_runner', None)
        if callable(getter):
            inst = getter()
            if inst is not None:
                inst_dir = getattr(inst, 'directory', None)
                if inst_dir is None or Path(inst_dir).resolve() == directory.resolve():
                    return inst
    return None


@_register_route('get', '/state')
def state():
    directory = Path(plugin_data_dir('butler'))
    settings, live_cfg, sys_tz, eff_tz, eff_src, raw_cfg = _resolve_context(directory)
    now = _now()
    tz_for_calc = eff_tz or 'UTC'
    local_today_iso = _local_today_str(now, tz_for_calc)
    plugin_enabled, allow_gw = _butler_plugin_flags(raw_cfg)

    db_path = directory / 'responsibilities.sqlite3'
    rows = []
    wake_logs = []
    routes_list = []

    if db_path.exists():
        needs_reset = False
        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as ro_db:
            ro_db.row_factory = sqlite3.Row
            resp_cols = {r['name'] for r in ro_db.execute('PRAGMA table_info(responsibilities)').fetchall()}
            if {'kind', 'completed_day', 'status'}.issubset(resp_cols):
                stale = ro_db.execute(
                    """
                    SELECT 1 FROM responsibilities
                    WHERE kind='non_negotiable'
                      AND status IN ('completed', 'submitted')
                      AND (completed_day IS NULL OR completed_day != ?)
                    LIMIT 1
                    """,
                    (local_today_iso,),
                ).fetchone()
                needs_reset = stale is not None

        if needs_reset and _store is not None:
            morning_due = now  # no fixed morning time; the model picks the next touch
            with _store.ResponsibilityStore(db_path) as st:
                reset_count = st.reset_daily_non_negotiables(local_today_iso, morning_due)
                if reset_count > 0:
                    st.record_wake_log(
                        None, now, 'daily_reset', 'ok',
                        f'Reset {reset_count} daily non-negotiable(s) for {local_today_iso}',
                    )

        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            tables = {
                r['name'] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if 'responsibilities' in tables:
                raw_rows = [
                    dict(row) for row in db.execute(
                        'SELECT * FROM responsibilities ORDER BY due_at, id LIMIT 500'
                    )
                ]
                rows = [_serialize_responsibility(r, local_today_iso) for r in raw_rows]
            title_by_id = {r['id']: r['title'] for r in rows}
            if 'wake_log' in tables:
                for log_row in db.execute(
                    'SELECT id, item_id, timestamp, action, outcome, detail FROM wake_log ORDER BY timestamp DESC, id DESC LIMIT 25'
                ):
                    entry = dict(log_row)
                    entry['item_title'] = title_by_id.get(entry.get('item_id')) or ''
                    wake_logs.append(entry)
            if 'admitted_routes' in tables:
                for r_row in db.execute(
                    'SELECT route_id, kind, platform, updated_at, is_preferred FROM admitted_routes ORDER BY is_preferred DESC, updated_at DESC, route_id ASC'
                ):
                    routes_list.append(_sanitize_route(dict(r_row)))

    non_negotiables = [r for r in rows if r.get('kind') == 'non_negotiable']
    commitments = [r for r in rows if r.get('kind') != 'non_negotiable']
    quota = _fallback_quota(directory, now, tz_for_calc, settings)
    cadence_info = _fallback_describe_cadence(settings)

    return {
        'settings': settings,
        'responsibilities': rows,
        'non_negotiables': non_negotiables,
        'commitments': commitments,
        'responsibilities_limit': 500,
        'settings_enforced': bool(settings.get('applySystemControls')),
        'lifetime_awareness_active': bool(settings.get('lifetimeAwarenessEnabled', True) and not settings.get('paused')),
        'detected_system_timezone': sys_tz,
        'effective_timezone': eff_tz,
        'effective_timezone_source': eff_src,
        'local_today': local_today_iso,
        'quota': quota,
        'routes': routes_list,
        'recent_activity': wake_logs,
        'cadence_info': cadence_info,
        'allow_gateway_injection': allow_gw,
        'plugin_enabled': plugin_enabled,
        'hermes_live_config': live_cfg,
    }


@_register_route('put', '/settings')
def save_settings(data: dict):
    directory = Path(plugin_data_dir('butler'))
    try:
        saved = _settings.save(directory, data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if saved.get('applySystemControls'):
        _notifications.apply_system_controls_to_hermes(
            saved, config_loader=_config_loader, config_saver=_config_saver,
        )
    return saved


@_register_route('post', '/detect-timezone')
def detect_timezone(payload: dict | None = None):
    use_ip = bool((payload or {}).get('use_ip', False))
    sys_tz = _detect_sys_tz()
    ip_tz = _detect_ip_tz() if use_ip else ''
    return {
        'detected_system_timezone': sys_tz,
        'detected_ip_timezone': ip_tz,
        'suggested_timezone': ip_tz or sys_tz,
    }


@_register_route('post', '/preset')
def apply_preset(payload: dict):
    preset = (payload or {}).get('preset')
    directory = Path(plugin_data_dir('butler'))
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


@_register_route('post', '/responsibilities')
def create_responsibility(payload: dict):
    if _store is None:
        raise HTTPException(status_code=503, detail='Responsibility store unavailable')
    body = payload or {}
    raw_title = body.get('title')
    if not isinstance(raw_title, str) or not raw_title.strip():
        raise HTTPException(status_code=422, detail='title is required')
    title = raw_title.strip()
    kind = str(body.get('kind') or 'commitment').strip() or 'commitment'

    directory = Path(plugin_data_dir('butler'))
    settings, _live_cfg, _sys_tz, eff_tz, _eff_src, _raw_cfg = _resolve_context(directory)
    tz_name = eff_tz or 'UTC'
    now = _now()
    local_today_iso = _local_today_str(now, tz_name)

    try:
        parsed_due = _parse_client_datetime(body.get('due_at'), tz_name)
        parsed_deadline = _parse_client_datetime(body.get('deadline_at'), tz_name)
        interval_raw = body.get('interval_seconds')
        interval_seconds = int(interval_raw) if interval_raw not in (None, '') else None
        sched = _fallback_compute_schedule(
            kind, now, settings, tz_name,
            due_at=parsed_due, deadline_at=parsed_deadline,
            interval_seconds=interval_seconds,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    meta = {}
    if isinstance(body.get('metadata'), dict):
        meta.update({k: v for k, v in body['metadata'].items() if k not in _PRIVATE_META_KEYS})
    notes = str(body.get('notes') or '').strip()
    prep_hint = str(body.get('prep_hint') or '').strip()
    if notes:
        meta['notes'] = notes
    if prep_hint:
        meta['prep_hint'] = prep_hint

    db_path = directory / 'responsibilities.sqlite3'
    directory.mkdir(parents=True, exist_ok=True)
    route_id = None
    reg_cls = getattr(_routes, 'RouteRegistry', None)
    if reg_cls is not None:
        with reg_cls(db_path) as reg:
            pref = _ensure_active_route(directory, reg)
            if pref:
                route_id = pref.get('route_id')

    try:
        with _store.ResponsibilityStore(db_path) as st:
            created = st.create(
                title,
                sched['due_at'],
                timezone_name=tz_name,
                interval_seconds=sched.get('interval_seconds'),
                metadata=meta,
                kind=kind,
                route_id=route_id,
                deadline_at=sched.get('deadline_at'),
            )
            label = 'daily non-negotiable' if kind == 'non_negotiable' else kind.replace('_', ' ')
            st.record_wake_log(created['id'], now, 'created', 'ok', f'Added {label}: {title}')
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        'ok': True,
        'item': _serialize_responsibility(created, local_today_iso),
        'schedule_reason': sched.get('schedule_reason', ''),
    }


@_register_route('post', '/responsibilities/{item_id}/action')
def responsibility_action(item_id: str, payload: dict):
    if _store is None:
        raise HTTPException(status_code=503, detail='Responsibility store unavailable')
    body = payload or {}
    action = str(body.get('action') or '').strip().lower()
    if not action:
        raise HTTPException(status_code=422, detail='action is required')

    directory = Path(plugin_data_dir('butler'))
    db_path = directory / 'responsibilities.sqlite3'
    if not db_path.exists():
        raise HTTPException(status_code=404, detail='Responsibility not found')

    settings, _live_cfg, _sys_tz, eff_tz, _eff_src, _raw_cfg = _resolve_context(directory)
    tz_name = eff_tz or 'UTC'
    now = _now()
    local_today_iso = _local_today_str(now, tz_name)

    with _store.ResponsibilityStore(db_path) as st:
        existing = st.get(item_id)
        if existing is None:
            raise HTTPException(status_code=404, detail='Responsibility not found')

        try:
            if action == 'complete':
                if existing.get('kind') == 'non_negotiable':
                    st.mark_non_negotiable_done(item_id, local_today_iso)
                else:
                    if existing.get('status') not in ('active', 'paused', 'completed'):
                        st.reopen(item_id)
                    if existing.get('status') != 'completed':
                        st.complete(item_id)
                st.record_wake_log(item_id, now, 'completed', 'ok', f"Completed: {existing['title']}")

            elif action == 'reopen':
                explicit_due = _parse_client_datetime(body.get('due_at'), tz_name)
                target_due = explicit_due
                if target_due is None and existing.get('due_at') and existing['due_at'] < now:
                    target_due = _fallback_compute_schedule(
                        existing.get('kind') or 'commitment', now, settings, tz_name,
                    )['due_at']
                st.reopen(item_id, due_at=target_due)
                st.record_wake_log(item_id, now, 'reopened', 'ok', f"Reopened: {existing['title']}")

            elif action == 'snooze':
                raw_until = body.get('until')
                raw_mins = body.get('snooze_minutes')
                if raw_until == 'tomorrow_morning' or raw_mins == 'tomorrow_morning':
                    local_now = now.astimezone(_safe_zone(tz_name))
                    tmr = local_now.date() + timedelta(days=1)
                    # user explicitly asked for 'tomorrow morning' in the UI: start of day, quiet-hours aware
                    cand = datetime(tmr.year, tmr.month, tmr.day, 9, 0, 0, tzinfo=_safe_zone(tz_name)).astimezone(UTC)
                    shift_fn = getattr(_followup, 'shift_out_of_quiet_hours', None)
                    target_dt = shift_fn(cand, settings, tz_name) if callable(shift_fn) else cand
                elif raw_mins not in (None, ''):
                    mins = int(raw_mins)
                    if mins <= 0:
                        raise ValueError('snooze_minutes must be positive')
                    target_dt = now + timedelta(minutes=mins)
                elif raw_until not in (None, ''):
                    target_dt = _parse_client_datetime(raw_until, tz_name)
                else:
                    target_dt = now + timedelta(minutes=60)

                if existing.get('status') != 'active':
                    st.reopen(item_id, due_at=target_dt)
                else:
                    st.snooze(item_id, target_dt)
                local_target = target_dt.astimezone(_safe_zone(tz_name)).strftime('%b %d %H:%M')
                st.record_wake_log(item_id, now, 'snoozed', 'ok', f"Snoozed '{existing['title']}' until {local_target}")

            elif action == 'pause':
                if existing.get('status') not in ('active', 'paused'):
                    st.reopen(item_id)
                if existing.get('status') != 'paused':
                    st.pause(item_id)
                st.record_wake_log(item_id, now, 'paused', 'ok', f"Paused: {existing['title']}")

            elif action == 'resume':
                if existing.get('status') == 'paused':
                    st.resume(item_id)
                elif existing.get('status') != 'active':
                    st.reopen(item_id)
                st.record_wake_log(item_id, now, 'resumed', 'ok', f"Resumed: {existing['title']}")

            elif action == 'stop':
                if existing.get('status') not in ('active', 'paused', 'stopped'):
                    st.reopen(item_id)
                if existing.get('status') != 'stopped':
                    st.stop(item_id)
                st.record_wake_log(item_id, now, 'stopped', 'ok', f"Stopped: {existing['title']}")

            elif action == 'delete':
                st.record_wake_log(item_id, now, 'deleted', 'ok', f"Deleted: {existing['title']}")
                st.delete(item_id)
                return {'ok': True, 'action': 'delete', 'deleted': True, 'id': item_id}

            elif action == 'update':
                kwargs = {}
                if 'title' in body and body['title'] is not None:
                    kwargs['title'] = str(body['title']).strip()
                if 'kind' in body and body['kind']:
                    kwargs['kind'] = str(body['kind']).strip()
                if 'due_at' in body and body['due_at'] not in (None, ''):
                    kwargs['due_at'] = _parse_client_datetime(body['due_at'], tz_name)
                if 'deadline_at' in body:
                    kwargs['deadline_at'] = _parse_client_datetime(body['deadline_at'], tz_name)
                if 'interval_seconds' in body:
                    iv = body['interval_seconds']
                    kwargs['interval_seconds'] = int(iv) if iv not in (None, '') else None
                if 'notes' in body or 'prep_hint' in body:
                    merged_meta = dict(_parse_metadata(existing.get('metadata')))
                    if 'notes' in body:
                        notes_val = str(body.get('notes') or '').strip()
                        if notes_val:
                            merged_meta['notes'] = notes_val
                        else:
                            merged_meta.pop('notes', None)
                    if 'prep_hint' in body:
                        hint_val = str(body.get('prep_hint') or '').strip()
                        if hint_val:
                            merged_meta['prep_hint'] = hint_val
                        else:
                            merged_meta.pop('prep_hint', None)
                    kwargs['metadata'] = merged_meta
                if kwargs:
                    st.update_item(item_id, **kwargs)
                st.record_wake_log(item_id, now, 'updated', 'ok', f"Updated: {kwargs.get('title') or existing['title']}")

            else:
                raise HTTPException(
                    status_code=422,
                    detail='Unsupported action; expected complete, reopen, snooze, pause, resume, stop, delete, or update',
                )
        except HTTPException:
            raise
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        updated = st.get(item_id)
        return {
            'ok': True,
            'action': action,
            'item': _serialize_responsibility(updated, local_today_iso) if updated else None,
        }


@_register_route('post', '/enable-proactive')
def enable_proactive(payload: dict | None = None):
    body = payload or {}
    directory = Path(plugin_data_dir('butler'))
    current = _settings.load(directory)
    updated = (
        _settings.quiet_human_preset(current)
        if body.get('applyQuietPreset')
        else dict(current)
    )
    updated['notificationsEnabled'] = True
    updated['paused'] = False

    chosen_tz = str(body.get('timezone') or '').strip() or updated.get('timezone') or _detect_sys_tz() or 'UTC'
    updated['timezone'] = chosen_tz

    if 'tone' in body and body['tone']:
        updated['tone'] = body['tone']
    if 'persistence' in body and body['persistence']:
        updated['persistence'] = body['persistence']
    if 'dailyLimit' in body:
        dl = body['dailyLimit']
        if dl in (None, ''):
            updated['dailyLimit'] = None
        elif isinstance(dl, bool) or not isinstance(dl, (int, str)):
            raise HTTPException(status_code=422, detail='Daily limit must be 0–1000 or unset')
        else:
            try:
                updated['dailyLimit'] = int(dl)
            except ValueError:
                raise HTTPException(status_code=422, detail='Daily limit must be 0–1000 or unset') from None
    if 'quietEnabled' in body and isinstance(body['quietEnabled'], bool):
        updated['quietEnabled'] = body['quietEnabled']
    if 'quietStart' in body and body['quietStart'] is not None:
        updated['quietStart'] = str(body['quietStart'])
    if 'quietEnd' in body and body['quietEnd'] is not None:
        updated['quietEnd'] = str(body['quietEnd'])
    if updated['quietEnabled'] and (not updated['quietStart'] or not updated['quietEnd']):
        updated['quietStart'] = updated['quietStart'] or '22:00'
        updated['quietEnd'] = updated['quietEnd'] or '08:00'

    try:
        saved = _settings.save(directory, updated)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    plugin_sync = _enable_butler_in_hermes_config()
    hermes_sync = (
        _notifications.apply_system_controls_to_hermes(
            saved, config_loader=_config_loader, config_saver=_config_saver,
        )
        if saved.get('applySystemControls') else {'applied': False, 'reason': 'applySystemControls is false'}
    )

    now = _now()
    eff_tz = saved.get('timezone') or 'UTC'
    local_today_iso = _local_today_str(now, eff_tz)
    db_path = directory / 'responsibilities.sqlite3'

    route_id = None
    reg_cls = getattr(_routes, 'RouteRegistry', None)
    if reg_cls is not None:
        with reg_cls(db_path) as reg:
            pref = _ensure_active_route(directory, reg)
            if pref:
                route_id = pref.get('route_id')

    seeded_items = []
    raw_nn = body.get('nonNegotiables')
    if isinstance(raw_nn, list) and _store is not None:
        clean_titles = []
        seen_input = set()
        for entry in raw_nn:
            if isinstance(entry, str) and entry.strip():
                norm = entry.strip()
                if norm.lower() not in seen_input:
                    seen_input.add(norm.lower())
                    clean_titles.append(norm)
        if clean_titles:
            with _store.ResponsibilityStore(db_path) as st:
                existing_titles = {
                    str(item.get('title') or '').strip().lower()
                    for item in st.list()
                    if item.get('kind') == 'non_negotiable' and item.get('status') != 'stopped'
                }
                for title in clean_titles:
                    if title.lower() in existing_titles:
                        continue
                    sched = _fallback_compute_schedule('non_negotiable', now, saved, eff_tz)
                    created = st.create(
                        title,
                        sched['due_at'],
                        timezone_name=eff_tz,
                        kind='non_negotiable',
                        route_id=route_id,
                        deadline_at=sched.get('deadline_at'),
                    )
                    existing_titles.add(title.lower())
                    seeded_items.append(_serialize_responsibility(created, local_today_iso))
                st.record_wake_log(
                    None, now, 'proactive_enabled', 'ok',
                    f'Enabled proactive assistant ({saved["tone"]} · {saved["persistence"]}) with {len(seeded_items)} seeded non-negotiable(s)',
                )
    elif _store is not None and db_path.exists():
        with _store.ResponsibilityStore(db_path) as st:
            if route_id:
                for item in st.list():
                    if not item.get('route_id') and item.get('status') in ('active', 'paused'):
                        st.update_item(item['id'], route_id=route_id)
            st.record_wake_log(
                None, now, 'proactive_enabled', 'ok',
                f'Enabled proactive assistant ({saved["tone"]} · {saved["persistence"]})',
            )

    refreshed = state()
    refreshed.update({
        'ok': True,
        'seeded_count': len(seeded_items),
        'seeded_items': seeded_items,
        'plugin_sync': plugin_sync,
        'hermes_sync': hermes_sync,
    })
    return refreshed


@_register_route('post', '/check-in-now')
def check_in_now(payload: dict | None = None):
    body = payload or {}
    item_id = str(body.get('item_id') or '').strip() or None
    directory = Path(plugin_data_dir('butler'))
    db_path = directory / 'responsibilities.sqlite3'
    now = _now()

    live_runner = _find_active_runner(directory)
    route_id = None
    reg_cls = getattr(_routes, 'RouteRegistry', None)
    if reg_cls is not None and (db_path.exists() or live_runner is not None):
        directory.mkdir(parents=True, exist_ok=True)
        with reg_cls(db_path) as reg:
            pref = _ensure_active_route(directory, reg)
            if pref:
                route_id = pref.get('route_id')

    if live_runner is not None and hasattr(live_runner, 'trigger_now'):
        res = live_runner.trigger_now(item_id=item_id)
        if res.get('triggered'):
            return {
                'ok': True,
                'triggered': True,
                'mode': 'live_runner',
                'detail': f"Check-in sent now ({res.get('woken_items', 1)} item(s)).",
                'result': res,
            }
        reason = res.get('reason') or 'not_triggered'
        if reason in ('policy_blocked', 'route_missing', 'injection_failed'):
            detail_map = {
                'policy_blocked': 'Blocked by wake guardrails (check pause, quiet hours, daily limit, or notifications switch).',
                'route_missing': 'No active chat session route registered yet — send one message in chat first.',
                'injection_failed': 'Gateway injection is disabled or unavailable — click Enable Background Wakes first.',
            }
            return {
                'ok': False,
                'triggered': False,
                'mode': 'live_runner',
                'reason': reason,
                'detail': detail_map.get(reason, reason),
                'result': res,
            }

    if _store is None or not db_path.exists():
        return {
            'ok': False,
            'triggered': False,
            'mode': 'none',
            'detail': 'No tracked daily non-negotiables or commitments yet. Add one first!',
        }

    with _store.ResponsibilityStore(db_path) as st:
        if item_id:
            target = st.get(item_id)
            if target is None:
                raise HTTPException(status_code=404, detail='Responsibility not found')
            targets = [target]
        else:
            all_items = st.list()
            targets = [
                it for it in all_items
                if it.get('kind') == 'non_negotiable' and it.get('status') == 'active'
            ]
            if not targets:
                targets = [
                    it for it in all_items
                    if it.get('status') == 'active'
                ]
        if not targets:
            return {
                'ok': False,
                'triggered': False,
                'mode': 'queued_due_now',
                'detail': 'All daily non-negotiables and commitments are already completed or paused.',
            }

        for it in targets:
            if it.get('status') != 'active':
                st.reopen(it['id'], due_at=now)
            else:
                st.snooze(it['id'], now)
            if route_id and not it.get('route_id'):
                st.update_item(it['id'], route_id=route_id)
        st.record_wake_log(
            item_id if item_id else targets[0]['id'],
            now,
            'manual_check_in',
            'queued',
            f'Queued immediate check-in for {len(targets)} item(s)',
        )
        return {
            'ok': True,
            'triggered': True,
            'mode': 'queued_due_now',
            'queued_count': len(targets),
            'detail': f'Scheduled immediate check-in for {len(targets)} item(s) — background runner will pick it up momentarily.',
        }
