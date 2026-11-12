"""Background wake runner, watcher polling, and immediate check-in triggers.

Injection is acceptance by the host session queue/gateway, not delivery proof.
A persisted metadata session_key never authorizes outreach; every wake requires
an authenticated route in RouteRegistry.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import inspect
import logging
from pathlib import Path
import sys
import threading
from zoneinfo import ZoneInfo

from .context import local_time, resolve_effective_timezone
from .policy import Policy
from .scheduler import Scheduler
from .settings import defaults, load as load_settings
from .watchers import WatchPolicy, WatcherEngine

log = logging.getLogger(__name__)

try:
    from .followup import VISIBLE_WAKE_MARKER
except ImportError:
    VISIBLE_WAKE_MARKER = 'Butler check-in.'

_SHARED_RUNNER_STATE = sys.modules.setdefault(
    '_butler_active_runner_state',
    {'lock': threading.Lock(), 'runner': None},
)


def set_active_runner(runner):
    with _SHARED_RUNNER_STATE['lock']:
        _SHARED_RUNNER_STATE['runner'] = runner


def get_active_runner():
    with _SHARED_RUNNER_STATE['lock']:
        return _SHARED_RUNNER_STATE['runner']


@dataclass(frozen=True)
class _FallbackFollowupDecision:
    should_wake: bool
    actions: tuple[str, ...] = ('check_in',)
    next_stage: int = 1
    next_due_at: datetime | None = None
    next_status: str = 'submitted'
    urgency: str = 'normal'
    heads_up: bool = False
    reason: str = 'due'


@dataclass(frozen=True)
class _FallbackWakeBatch:
    route_id: str
    items: list
    decisions: dict


class _NoopRouteRegistry:
    def record_route(self, route, preferred=True):
        return None

    def get_route(self, route_id):
        return None

    def preferred_route(self):
        return None

    def list_routes(self):
        return []

    def close(self):
        return None


def _open_default_route_registry(db_path):
    try:
        from . import routes as _routes
        return _routes.RouteRegistry(db_path)
    except ImportError:
        return _NoopRouteRegistry()


RouteRegistry = _open_default_route_registry


def _call_next_followup_step(
    item, now, settings, *, effective_timezone='UTC', available_channels=('check_in', 'push'),
):
    try:
        from . import followup as _followup
        return _followup.next_followup_step(
            item,
            now,
            settings,
            effective_timezone=effective_timezone,
            available_channels=available_channels,
        )
    except ImportError:
        # No built-in cadence: wake now, then wait for the model to reschedule or close the item.
        raw_stage = item.get('stage', item.get('followup_stage', -1))
        return _FallbackFollowupDecision(
            should_wake=True, actions=('check_in',),
            next_stage=int(-1 if raw_stage is None else raw_stage) + 1,
            next_due_at=None, next_status='submitted', urgency='normal', heads_up=False,
            reason='awaiting_model_reschedule',
        )


def _call_coalesce_due_batch(items_and_decisions):
    try:
        from . import followup as _followup
        return _followup.coalesce_due_batch(items_and_decisions)
    except ImportError:
        grouped = {}
        order = []
        for item, dec in items_and_decisions:
            if not dec or not dec.should_wake:
                continue
            rid = str(item.get('route_id') or '')
            if rid not in grouped:
                grouped[rid] = ([], {})
                order.append(rid)
            grouped[rid][0].append(item)
            item_id = str(item.get('id') or len(grouped[rid][0]))
            grouped[rid][1][item_id] = dec
        return [_FallbackWakeBatch(rid, grouped[rid][0], grouped[rid][1]) for rid in order]


def _call_build_action_first_wake_prompt(items, decisions, settings, *, now, effective_timezone='UTC', engagement=None):
    try:
        from . import followup as _followup
        return _followup.build_action_first_wake_prompt(
            items, decisions, settings, now=now, effective_timezone=effective_timezone,
            engagement=engagement,
        )
    except ImportError:
        tone = (settings or {}).get('tone', 'Balanced')
        lines = [
            f'[Butler Proactive Wake — tone={tone}, tz={effective_timezone}]',
            'Action-first instruction: inspect context or prepare concrete help first, then message the user concisely.',
        ]
        for idx, item in enumerate(items, start=1):
            item_id = str(item.get('id') or f'item-{idx}')
            dec = decisions.get(item_id) if isinstance(decisions, dict) else decisions[idx - 1]
            kind = item.get('kind') or 'commitment'
            meta = item.get('metadata') or {}
            detail_parts = [f"[{kind}] {item.get('title', '')}"]
            if meta.get('prep_hint'):
                detail_parts.append(f"prep: {meta['prep_hint']}")
            if meta.get('notes'):
                detail_parts.append(f"notes: {meta['notes']}")
            change = item.get('watch_change') or meta.get('watch_change')
            if change:
                detail_parts.append(f"watch changed: {change.get('target', '')}")
            if dec is not None:
                detail_parts.append(f'stage={dec.next_stage}')
            lines.append('- ' + ' | '.join(detail_parts))
        return '\n'.join(lines)


next_followup_step = _call_next_followup_step
coalesce_due_batch = _call_coalesce_due_batch
build_action_first_wake_prompt = _call_build_action_first_wake_prompt


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


def _default_directory() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir('butler')


def _route_is_usable(route) -> bool:
    if not isinstance(route, dict) or not route.get('route_id'):
        return False
    kind = route.get('kind')
    if kind == 'session_key':
        sk = route.get('session_key')
        return isinstance(sk, str) and bool(sk.strip())
    if kind == 'origin':
        origin = route.get('origin')
        return isinstance(origin, dict) and bool(origin)
    return False


def _evaluate_policy_fallback(prof_dir: Path, settings: dict, now: datetime, effective_timezone: str) -> dict:
    if settings.get('paused'):
        return {'allowed': False, 'reason': 'paused', 'defer_until': None, 'in_quiet_hours': False}
    if not settings.get('notificationsEnabled'):
        return {'allowed': False, 'reason': 'notifications_disabled', 'defer_until': None, 'in_quiet_hours': False}
    tz_name = settings.get('timezone') or effective_timezone or 'UTC'
    if (settings.get('quietEnabled') or settings.get('dailyLimit') is not None) and not settings.get('timezone') and not effective_timezone:
        return {'allowed': False, 'reason': 'timezone_required', 'defer_until': None, 'in_quiet_hours': False}
    local_dt = now.astimezone(ZoneInfo(tz_name))
    if settings.get('quietEnabled') and settings.get('quietStart') and settings.get('quietEnd'):
        start = time.fromisoformat(settings['quietStart'])
        end = time.fromisoformat(settings['quietEnd'])
        cur = local_dt.time().replace(tzinfo=None)
        in_quiet = start <= cur < end if start < end else cur >= start or cur < end
        if in_quiet:
            target_date = local_dt.date()
            if start >= end and cur >= start:
                target_date = target_date + timedelta(days=1)
            defer_local = datetime.combine(target_date, end, tzinfo=ZoneInfo(tz_name))
            return {
                'allowed': False,
                'reason': 'quiet_hours',
                'defer_until': defer_local.astimezone(timezone.utc),
                'in_quiet_hours': True,
            }
    return {'allowed': True, 'reason': 'ok', 'defer_until': None, 'in_quiet_hours': False}


def _evaluate_wake(policy: Policy, prof_dir: Path, settings: dict, now: datetime, effective_timezone: str) -> dict:
    if hasattr(policy, 'evaluate_wake'):
        return policy.evaluate_wake(now, fallback_timezone=effective_timezone)
    return _evaluate_policy_fallback(prof_dir, settings, now, effective_timezone)


def _reserve_wake(policy: Policy, now: datetime, effective_timezone: str) -> bool:
    try:
        sig = inspect.signature(policy.reserve_wake)
        if 'fallback_timezone' in sig.parameters:
            return bool(policy.reserve_wake(now, fallback_timezone=effective_timezone))
    except (TypeError, ValueError):
        pass
    return bool(policy.reserve_wake(now))


class Runner:
    def __init__(
        self,
        ctx,
        store_factory,
        *,
        directory=None,
        route_registry_factory=None,
        poll_seconds=30,
        now_fn=lambda: datetime.now(timezone.utc),
    ):
        self.ctx = ctx
        self.store_factory = store_factory
        self._directory = directory
        self.route_registry_factory = route_registry_factory
        self.poll_seconds = poll_seconds
        self.now_fn = now_fn
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._tick_lock = threading.RLock()
        self._thread = None

    def _resolve_directory(self, store) -> Path:
        if self._directory is not None:
            raw = self._directory() if callable(self._directory) else self._directory
            return Path(raw)
        db_path = _store_db_path(store)
        if db_path is not None:
            return db_path.parent
        return _default_directory()

    def _open_route_registry(self, store, prof_dir: Path):
        if self.route_registry_factory is not None:
            return self.route_registry_factory()
        db_path = _store_db_path(store) or (prof_dir / 'responsibilities.sqlite3')
        if not db_path.exists():
            return _NoopRouteRegistry()
        return RouteRegistry(db_path)

    @staticmethod
    def _load_profile_settings(prof_dir: Path) -> dict:
        try:
            return load_settings(prof_dir)
        except Exception:
            return defaults()

    @staticmethod
    def _record_log(store, item_id: str, now: datetime, action: str, outcome: str, detail: str = '') -> None:
        if hasattr(store, 'record_wake_log'):
            try:
                store.record_wake_log(item_id, now, action, outcome, detail)
            except Exception:
                log.debug('Butler could not record wake log for %s', item_id, exc_info=True)

    @staticmethod
    def _engagement_gate(store, settings: dict, now: datetime) -> str | None:
        """Owner-set safety gates (not model decisions): stop piling nudges on someone who is not answering,
        and keep a minimum gap between proactive wakes. 0 disables a gate."""
        eng = store.get_engagement()
        cap = int(settings.get('maxUnanswered', 0) or 0)
        if cap and eng['unanswered_wakes'] >= cap:
            return 'max_unanswered'
        gap = int(settings.get('minWakeGapMinutes', 0) or 0)
        last = eng.get('last_wake_at')
        if gap and last is not None and (now - last) < timedelta(minutes=gap):
            return 'min_wake_gap'
        return None

    @staticmethod
    def _defer_or_snooze(store, item_id: str, token: str, now: datetime, until: datetime) -> None:
        if hasattr(store, 'defer_claim'):
            store.defer_claim(item_id, token, now, until=until)
        else:
            store.snooze(item_id, until)

    @staticmethod
    def _advance_item(store, item_id: str, token: str, now: datetime, decision) -> None:
        if hasattr(store, 'advance_followup'):
            store.advance_followup(
                item_id,
                token,
                now,
                next_due_at=decision.next_due_at,
                stage=decision.next_stage,
                next_status=decision.next_status,
            )
        else:
            store.finish_claim(item_id, token, now)

    def _check_watch_item(self, store, item: dict, now: datetime) -> tuple[bool, dict]:
        """Return (should_continue_to_wake, updated_item)."""
        watch_spec = (item.get('metadata') or {}).get('watch')
        if item.get('kind') != 'watch' or not isinstance(watch_spec, dict):
            return True, item
        files = frozenset([watch_spec['path']]) if watch_spec.get('kind') == 'file' and isinstance(watch_spec.get('path'), str) else frozenset()
        policy = WatchPolicy(files=files)
        db_conn = getattr(store, 'db', None)
        try:
            engine = WatcherEngine(policy, db=db_conn)
            watch_res = engine.check(item['id'], watch_spec)
        except Exception as exc:
            log.warning('Butler watch check failed for %s: %s', item['id'], exc)
            if item.get('interval_seconds') is not None:
                store.finish_claim(item['id'], item['claim_token'], now)
            else:
                self._defer_or_snooze(store, item['id'], item['claim_token'], now, now + timedelta(seconds=300))
            self._record_log(store, item['id'], now, 'watch_check', 'error', str(exc))
            return False, item

        if not watch_res.changed:
            if item.get('interval_seconds') is not None:
                store.finish_claim(item['id'], item['claim_token'], now)
            else:
                self._defer_or_snooze(store, item['id'], item['claim_token'], now, now + timedelta(seconds=300))
            outcome = 'baseline' if watch_res.previous_digest is None else 'unchanged'
            self._record_log(store, item['id'], now, 'watch_check', outcome, watch_res.digest[:12])
            return False, item

        enriched = dict(item)
        meta = dict(item.get('metadata') or {})
        change_summary = {
            'kind': watch_spec.get('kind', 'file'),
            'target': watch_spec.get('path') or watch_spec.get('url') or watch_spec.get('name') or '',
            'digest': watch_res.digest,
            'previous_digest': watch_res.previous_digest,
        }
        meta['watch_change'] = change_summary
        enriched['metadata'] = meta
        enriched['watch_change'] = change_summary
        return True, enriched

    def _deliver_batches(
        self,
        *,
        store,
        registry,
        prof_dir: Path,
        settings: dict,
        effective_timezone: str,
        now: datetime,
        waking_pairs: list,
        routes_by_id: dict,
        force_wake: bool,
    ) -> tuple[int, int, int]:
        """Deliver coalesced wake batches. Returns (injected_batches, woken_items, failed_items)."""
        batches = coalesce_due_batch(waking_pairs) if waking_pairs else []
        injected_batches = 0
        woken_items = 0
        failed_items = 0
        policy = Policy(prof_dir)

        for idx, batch in enumerate(batches):
            cur_items = []
            cur_decisions_map = {}
            cur_pairs = []
            raw_decisions = batch.decisions
            for idx_it, it in enumerate(batch.items):
                item_id = str(it.get('id') or (idx_it + 1))
                dec = raw_decisions.get(item_id) if isinstance(raw_decisions, dict) else raw_decisions[idx_it]
                if dec is None:
                    continue
                if store.claim_is_current(it['id'], it['claim_token'], now):
                    cur_items.append(it)
                    cur_decisions_map[item_id] = dec
                    cur_pairs.append((it, dec))
            if not cur_items:
                continue

            route = routes_by_id.get(batch.route_id) or (
                registry.get_route(batch.route_id) if registry is not None and batch.route_id else None
            )
            if not _route_is_usable(route):
                for it, _ in cur_pairs:
                    store.release_claim(it['id'], it['claim_token'], now)
                continue

            if not force_wake:
                if not _reserve_wake(policy, now, effective_timezone):
                    eval_after = _evaluate_wake(policy, prof_dir, settings, now, effective_timezone)
                    defer_until = eval_after.get('defer_until')
                    for rem_batch in batches[idx:]:
                        for rem_it in rem_batch.items:
                            if store.claim_is_current(rem_it['id'], rem_it['claim_token'], now):
                                if defer_until is not None:
                                    self._defer_or_snooze(store, rem_it['id'], rem_it['claim_token'], now, defer_until)
                                    self._record_log(
                                        store, rem_it['id'], now, 'policy_gate', 'deferred', eval_after.get('reason', ''),
                                    )
                                else:
                                    store.release_claim(rem_it['id'], rem_it['claim_token'], now)
                    break

            prompt = build_action_first_wake_prompt(
                cur_items, cur_decisions_map, settings, now=now, effective_timezone=effective_timezone,
                engagement=(store.get_engagement() if hasattr(store, 'get_engagement') else None),
            )
            accepted = False
            err_detail = ''
            pending_wake_id = None
            try:
                if not hasattr(store, 'enqueue_pending_wake'):
                    raise RuntimeError('store does not support hidden wake context')
                pending_wake_id = store.enqueue_pending_wake(batch.route_id, prompt, now)
                if route['kind'] == 'session_key':
                    accepted = bool(
                        self.ctx.inject_message(VISIBLE_WAKE_MARKER, role='user', session_key=route['session_key'])
                    )
                else:
                    accepted = bool(
                        self.ctx.inject_message(VISIBLE_WAKE_MARKER, role='user', origin=route['origin'])
                    )
            except Exception as exc:
                log.warning('Butler wake injection failed for route %s', batch.route_id, exc_info=True)
                accepted = False
                err_detail = str(exc)

            if accepted is True:
                for it, dec in cur_pairs:
                    self._advance_item(store, it['id'], it['claim_token'], now, dec)
                    actions_str = ','.join(dec.actions) if getattr(dec, 'actions', None) else 'check_in'
                    self._record_log(store, it['id'], now, actions_str, 'injected', f'stage={dec.next_stage}')
                injected_batches += 1
                woken_items += len(cur_items)
            else:
                if pending_wake_id is not None and hasattr(store, 'cancel_pending_wake'):
                    store.cancel_pending_wake(pending_wake_id)
                for it, dec in cur_pairs:
                    store.release_claim(it['id'], it['claim_token'], now)
                    actions_str = ','.join(dec.actions) if getattr(dec, 'actions', None) else 'check_in'
                    self._record_log(
                        store,
                        it['id'],
                        now,
                        actions_str,
                        'failed',
                        err_detail or 'inject_message rejected',
                    )
                failed_items += len(cur_items)

        return injected_batches, woken_items, failed_items

    def tick(self, *, force_wake: bool = False) -> dict:
        with self._tick_lock:
            now = self.now_fn()
            if now.tzinfo is None or now.utcoffset() is None:
                now = now.replace(tzinfo=timezone.utc)
            store = self.store_factory()
            registry = None
            try:
                prof_dir = self._resolve_directory(store)
                registry = self._open_route_registry(store, prof_dir)
                settings = self._load_profile_settings(prof_dir)
                effective_timezone, _ = resolve_effective_timezone(settings)

                local_date = local_time(now, effective_timezone).date().isoformat()
                reset_count = 0
                if hasattr(store, 'reset_daily_non_negotiables'):
                    morning_due = now  # no fixed morning time: reopened non-negotiables wake the model, which picks the time
                    reset_count = int(store.reset_daily_non_negotiables(local_date, morning_due) or 0)

                claimed = Scheduler(store, batch_size=10, lease_seconds=300).claim(now)
                if not claimed:
                    return {
                        'claimed': 0,
                        'injected': 0,
                        'woken_items': 0,
                        'deferred': 0,
                        'skipped_unroutable': 0,
                        'watches_unchanged': 0,
                        'reset_non_negotiables': reset_count,
                    }

                routable = []
                skipped_unroutable = 0
                watches_unchanged = 0

                for item in claimed:
                    route_id = item.get('route_id')
                    route = registry.get_route(route_id) if (route_id and registry is not None) else None
                    if not _route_is_usable(route):
                        store.release_claim(item['id'], item['claim_token'], now)
                        skipped_unroutable += 1
                        continue

                    should_wake_watch, checked_item = self._check_watch_item(store, item, now)
                    if not should_wake_watch:
                        watches_unchanged += 1
                        continue

                    routable.append((checked_item, route))

                if not routable:
                    return {
                        'claimed': len(claimed),
                        'injected': 0,
                        'woken_items': 0,
                        'deferred': 0,
                        'skipped_unroutable': skipped_unroutable,
                        'watches_unchanged': watches_unchanged,
                        'reset_non_negotiables': reset_count,
                    }

                if force_wake:
                    if settings.get('paused'):
                        for item, _ in routable:
                            store.release_claim(item['id'], item['claim_token'], now)
                        return {
                            'claimed': len(claimed),
                            'injected': 0,
                            'woken_items': 0,
                            'deferred': 0,
                            'skipped_unroutable': skipped_unroutable,
                            'watches_unchanged': watches_unchanged,
                            'reset_non_negotiables': reset_count,
                            'policy_reason': 'paused',
                        }
                else:
                    policy = Policy(prof_dir)
                    eval_res = _evaluate_wake(policy, prof_dir, settings, now, effective_timezone)
                    if not eval_res.get('allowed'):
                        defer_until = eval_res.get('defer_until')
                        reason = eval_res.get('reason', 'blocked')
                        deferred_count = 0
                        for item, _ in routable:
                            if defer_until is not None:
                                self._defer_or_snooze(store, item['id'], item['claim_token'], now, defer_until)
                                self._record_log(store, item['id'], now, 'policy_gate', 'deferred', reason)
                                deferred_count += 1
                            else:
                                store.release_claim(item['id'], item['claim_token'], now)
                        return {
                            'claimed': len(claimed),
                            'injected': 0,
                            'woken_items': 0,
                            'deferred': deferred_count,
                            'skipped_unroutable': skipped_unroutable,
                            'watches_unchanged': watches_unchanged,
                            'reset_non_negotiables': reset_count,
                            'policy_reason': reason,
                        }

                if not force_wake and hasattr(store, 'get_engagement'):
                    held = self._engagement_gate(store, settings, now)
                    if held:
                        if held == 'max_unanswered' and hasattr(store, 'park_claim_for_reply'):
                            for item, _ in routable:
                                store.park_claim_for_reply(item['id'], item['claim_token'], now)
                                self._record_log(store, item['id'], now, 'engagement_gate', 'parked', held)
                        elif held == 'min_wake_gap':
                            eng = store.get_engagement()
                            next_allowed = eng['last_wake_at'] + timedelta(minutes=int(settings.get('minWakeGapMinutes') or 0))
                            for item, _ in routable:
                                self._defer_or_snooze(store, item['id'], item['claim_token'], now, next_allowed)
                                self._record_log(store, item['id'], now, 'engagement_gate', 'deferred', held)
                        else:
                            for item, _ in routable:
                                store.release_claim(item['id'], item['claim_token'], now)
                        return {
                            'claimed': len(claimed), 'injected': 0, 'woken_items': 0, 'deferred': 0,
                            'skipped_unroutable': skipped_unroutable, 'watches_unchanged': watches_unchanged,
                            'reset_non_negotiables': reset_count, 'policy_reason': held,
                        }

                waking_pairs = []
                routes_by_id = {}
                for item, route in routable:
                    decision = next_followup_step(
                        item, now, settings, effective_timezone=effective_timezone,
                    )
                    if not decision.should_wake:
                        self._advance_item(store, item['id'], item['claim_token'], now, decision)
                        self._record_log(
                            store, item['id'], now, 'followup_skip', 'advanced', getattr(decision, 'reason', ''),
                        )
                        continue
                    item_copy = dict(item)
                    item_copy['route_id'] = route['route_id']
                    waking_pairs.append((item_copy, decision))
                    routes_by_id[route['route_id']] = route

                injected_batches, woken_items, failed_items = self._deliver_batches(
                    store=store,
                    registry=registry,
                    prof_dir=prof_dir,
                    settings=settings,
                    effective_timezone=effective_timezone,
                    now=now,
                    waking_pairs=waking_pairs,
                    routes_by_id=routes_by_id,
                    force_wake=force_wake,
                )
                return {
                    'claimed': len(claimed),
                    'injected': injected_batches,
                    'woken_items': woken_items,
                    'failed': failed_items,
                    'deferred': 0,
                    'skipped_unroutable': skipped_unroutable,
                    'watches_unchanged': watches_unchanged,
                    'reset_non_negotiables': reset_count,
                }
            finally:
                if registry is not None and hasattr(registry, 'close'):
                    registry.close()
                if hasattr(store, 'close'):
                    store.close()

    def trigger_now(self, item_id: str | None = None) -> dict:
        """Immediately fire an action-first check-in for item_id or open non-negotiables."""
        with self._tick_lock:
            now = self.now_fn()
            if now.tzinfo is None or now.utcoffset() is None:
                now = now.replace(tzinfo=timezone.utc)
            store = self.store_factory()
            registry = None
            try:
                prof_dir = self._resolve_directory(store)
                registry = self._open_route_registry(store, prof_dir)
                settings = self._load_profile_settings(prof_dir)
                if settings.get('paused'):
                    return {
                        'ok': False,
                        'triggered': False,
                        'injected': 0,
                        'woken_items': 0,
                        'reason': 'policy_blocked',
                        'error': 'Butler is paused',
                    }
                effective_timezone, _ = resolve_effective_timezone(settings)
                local_date = local_time(now, effective_timezone).date().isoformat()

                if item_id is not None:
                    target = store.get(item_id)
                    if target is None:
                        return {
                            'ok': False,
                            'triggered': False,
                            'injected': 0,
                            'woken_items': 0,
                            'reason': 'not_found',
                            'error': 'responsibility not found',
                        }
                    if target.get('status') in ('queued', 'submitted') and hasattr(store, 'reopen'):
                        store.reopen(item_id, due_at=now)
                        target = store.get(item_id)
                    if target.get('status') != 'active':
                        return {
                            'ok': False,
                            'triggered': False,
                            'injected': 0,
                            'woken_items': 0,
                            'reason': 'invalid_status',
                            'error': f"responsibility status is {target.get('status')}",
                        }
                    candidates = [target]
                else:
                    all_rows = store.list()
                    active = [it for it in all_rows if it.get('status') == 'active']
                    open_nn = [
                        it for it in active
                        if it.get('kind') == 'non_negotiable'
                        and it.get('completed_day') != local_date
                        and (it.get('metadata') or {}).get('completed_day') != local_date
                    ]
                    due_now = [it for it in active if it.get('due_at') is not None and it['due_at'] <= now]
                    candidates = open_nn or due_now or active
                    if not candidates and hasattr(store, 'reopen'):
                        awaiting = [
                            it for it in all_rows
                            if it.get('status') in ('queued', 'submitted')
                            and not (
                                it.get('kind') == 'non_negotiable'
                                and (
                                    it.get('completed_day') == local_date
                                    or (it.get('metadata') or {}).get('completed_day') == local_date
                                )
                            )
                        ]
                        for it in awaiting:
                            store.reopen(it['id'], due_at=now)
                        if awaiting:
                            candidates = [store.get(it['id']) for it in awaiting if store.get(it['id']) is not None]
                    if not candidates:
                        return {
                            'ok': True,
                            'triggered': False,
                            'injected': 0,
                            'woken_items': 0,
                            'reason': 'no open items to trigger',
                        }

                routable_targets = {}
                original_due = {}
                for it in candidates:
                    rid = it.get('route_id')
                    route = (
                        registry.get_route(rid)
                        if (rid and registry is not None)
                        else (registry.preferred_route() if registry is not None else None)
                    )
                    if _route_is_usable(route):
                        routable_targets[it['id']] = route
                        if it.get('due_at') is not None and it['due_at'] > now:
                            original_due[it['id']] = it['due_at']
                            store.snooze(it['id'], now)

                if not routable_targets:
                    return {
                        'ok': False,
                        'triggered': False,
                        'injected': 0,
                        'woken_items': 0,
                        'reason': 'route_missing',
                        'error': 'no authenticated route available',
                    }

                claimed = Scheduler(store, batch_size=max(10, len(routable_targets) + 10), lease_seconds=300).claim(now)
                waking_pairs = []
                routes_by_id = {}
                for cl in claimed:
                    if cl['id'] not in routable_targets:
                        store.release_claim(cl['id'], cl['claim_token'], now)
                        continue
                    route = routable_targets[cl['id']]
                    decision = next_followup_step(cl, now, settings, effective_timezone=effective_timezone)
                    if not decision.should_wake:
                        decision = _FallbackFollowupDecision(
                            should_wake=True,
                            actions=getattr(decision, 'actions', ('check_in',)) or ('check_in',),
                            next_stage=getattr(decision, 'next_stage', 1),
                            next_due_at=getattr(decision, 'next_due_at', None),
                            next_status=getattr(decision, 'next_status', 'active'),
                            urgency=getattr(decision, 'urgency', 'normal'),
                            heads_up=getattr(decision, 'heads_up', False),
                            reason='manual_trigger',
                        )
                    cl_copy = dict(cl)
                    cl_copy['route_id'] = route['route_id']
                    waking_pairs.append((cl_copy, decision))
                    routes_by_id[route['route_id']] = route

                injected_batches, woken_items, failed_items = self._deliver_batches(
                    store=store,
                    registry=registry,
                    prof_dir=prof_dir,
                    settings=settings,
                    effective_timezone=effective_timezone,
                    now=now,
                    waking_pairs=waking_pairs,
                    routes_by_id=routes_by_id,
                    force_wake=True,
                )
                if failed_items and original_due:
                    for failed_id, prev_due in original_due.items():
                        cur = store.get(failed_id)
                        if cur and cur.get('status') == 'active' and cur.get('claim_token') is None:
                            store.snooze(failed_id, prev_due)

                ok = injected_batches > 0
                return {
                    'ok': ok,
                    'triggered': ok,
                    'injected': injected_batches,
                    'woken_items': woken_items,
                    'failed': failed_items,
                    'reason': 'ok' if ok else 'injection_failed',
                }
            finally:
                if registry is not None and hasattr(registry, 'close'):
                    registry.close()
                if hasattr(store, 'close'):
                    store.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception('Butler scheduler tick failed')
            self._stop.wait(self.poll_seconds)

    def start(self, **kwargs):
        with self._lock:
            if self._stop.is_set() or (self._thread and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._run, name='butler-scheduler', daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        with _SHARED_RUNNER_STATE['lock']:
            if _SHARED_RUNNER_STATE['runner'] is self:
                _SHARED_RUNNER_STATE['runner'] = None
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                log.warning('Butler scheduler did not stop within 5 seconds')
