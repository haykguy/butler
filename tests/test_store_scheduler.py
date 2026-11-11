import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from butler.store import ResponsibilityStore, VALID_KINDS


def test_finish_claim_checks_expiry_after_waiting_for_write_lock():
    with TemporaryDirectory(dir=Path(__file__).parent) as directory:
        path = Path(directory) / 'db'
        writer = ResponsibilityStore(path)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        item = writer.create('locked', now)
        claim = writer.claim_due(now, lease_seconds=2)[0]
        ready = threading.Event()
        start = threading.Event()
        attempting_begin = threading.Event()
        finished = threading.Event()
        outcome = []
        errors = []

        def finish():
            try:
                finisher = ResponsibilityStore(path)
                try:
                    finisher.db.set_trace_callback(
                        lambda sql: attempting_begin.set() if sql == 'BEGIN IMMEDIATE' else None
                    )
                    ready.set()
                    if not start.wait(5):
                        raise TimeoutError('finish was not started')
                    outcome.append(finisher.finish_claim(
                        item['id'], claim['claim_token'], now + timedelta(seconds=1)
                    ))
                finally:
                    finisher.close()
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=finish)
        thread.start()
        try:
            assert ready.wait(5)
            writer.db.execute('BEGIN IMMEDIATE')
            try:
                start.set()
                assert attempting_begin.wait(5)
                assert not finished.wait(0.2), 'finish did not wait for the write lock'
                # The claim is valid at the supplied time until this transaction
                # shortens its lease. The finisher must see the committed expiry.
                writer.db.execute(
                    'UPDATE responsibilities SET claim_until=? WHERE id=?',
                    ((now + timedelta(seconds=1)).isoformat(timespec='microseconds'), item['id']),
                )
            finally:
                writer.db.execute('COMMIT')
        finally:
            start.set()
            thread.join(5)
            writer.close()
        assert not thread.is_alive()
        assert errors == []
        assert outcome == [False]
        store = ResponsibilityStore(path)
        try:
            assert store.get(item['id'])['status'] == 'active'
        finally:
            store.close()


def test_finish_claim_explicitly_submits_not_completes():
    with TemporaryDirectory(dir=Path(__file__).parent) as directory:
        s = ResponsibilityStore(Path(directory) / 'db')
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        item = s.create('queued', now)
        claim = s.claim_due(now)[0]
        assert s.finish_claim(item['id'], claim['claim_token'], now)
        assert s.get(item['id'])['status'] == 'submitted'
        assert s.claim_due(now) == []
        s.close()


class StoreSchedulerTest(unittest.TestCase):
    def test_persists_across_reopen(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'store.sqlite'
            store = ResponsibilityStore(path)
            item = store.create('Check mail', datetime(2026, 1, 1, tzinfo=timezone.utc))
            store.close()
            store = ResponsibilityStore(path)
            self.assertEqual(store.get(item['id'])['title'], 'Check mail')
            store.close()

    def test_scheduler_claims_are_fenced_and_recovery_bounded(self):
        from butler.scheduler import Scheduler
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'store.sqlite'
            a = ResponsibilityStore(path)
            b = ResponsibilityStore(path)
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            first = a.create('one', now, interval_seconds=60)
            a.create('two', now)
            scheduler = Scheduler(a, batch_size=1, lease_seconds=30)
            claim = scheduler.claim(now)[0]
            self.assertEqual(len(Scheduler(b, batch_size=5).claim(now)), 1)
            self.assertTrue(a.claim_is_current(claim['id'], claim['claim_token'], now))
            later = now + timedelta(seconds=31)
            recovered = Scheduler(b, batch_size=1).claim(later)[0]
            self.assertEqual(recovered['id'], claim['id'])
            self.assertFalse(a.finish_claim(claim['id'], claim['claim_token'], later))
            self.assertTrue(b.finish_claim(recovered['id'], recovered['claim_token'], later))
            a.close()
            b.close()

    def test_controls_invalidate_claims_and_terminal_states(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            s = ResponsibilityStore(Path(directory) / 'db')
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            item = s.create('task', now, timezone_name='America/Chicago')
            claim = s.claim_due(now)[0]
            self.assertTrue(s.pause(item['id']))
            self.assertFalse(s.claim_is_current(item['id'], claim['claim_token'], now))
            self.assertEqual(s.claim_due(now), [])
            self.assertTrue(s.snooze(item['id'], now + timedelta(hours=1)))
            self.assertTrue(s.resume(item['id']))
            self.assertEqual(s.claim_due(now), [])
            self.assertEqual(len(s.claim_due(now + timedelta(hours=1))), 1)
            self.assertTrue(s.stop(item['id']))
            self.assertFalse(s.resume(item['id']))
            self.assertFalse(s.complete(item['id']))
            self.assertEqual(s.get(item['id'])['timezone'], 'America/Chicago')
            second = s.create('second', now)
            self.assertTrue(s.complete(second['id']))
            self.assertEqual(s.get(second['id'])['status'], 'completed')
            s.close()

    def test_recurring_recovery_coalesces_and_release(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            s = ResponsibilityStore(Path(directory) / 'db')
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            item = s.create('recurring', now, interval_seconds=60)
            later = now + timedelta(days=100)
            claim = s.claim_due(later)[0]
            self.assertTrue(s.release_claim(item['id'], claim['claim_token'], later))
            self.assertFalse(s.release_claim(item['id'], claim['claim_token'], later))
            claim = s.claim_due(later)[0]
            self.assertTrue(s.finish_claim(item['id'], claim['claim_token'], later))
            self.assertEqual(s.get(item['id'])['due_at'], later + timedelta(seconds=60))
            self.assertEqual(s.claim_due(later), [])
            s.close()

    def test_validation_and_one_shot_completion(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            s = ResponsibilityStore(Path(directory) / 'db')
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            with self.assertRaises(ValueError):
                s.create('naive', datetime(2026, 1, 1))
            with self.assertRaises(ValueError):
                s.create('bad interval', now, interval_seconds=0)
            item = s.create('one', now, metadata={'key': 'value'})
            claim = s.claim_due(now)[0]
            self.assertTrue(s.finish_claim(item['id'], claim['claim_token'], now))
            self.assertEqual(s.get(item['id'])['metadata'], {'key': 'value'})
            self.assertEqual(s.get(item['id'])['status'], 'submitted')
            self.assertFalse(s.finish_claim(item['id'], claim['claim_token'], now))
            s.close()


def test_idempotent_schema_migration_preserves_legacy_rows(tmp_path):
    db_path = tmp_path / 'legacy.sqlite3'
    raw = sqlite3.connect(db_path)
    raw.execute('''
    CREATE TABLE responsibilities (
      id TEXT PRIMARY KEY, title TEXT NOT NULL, due_at TEXT NOT NULL,
      timezone TEXT NOT NULL, interval_seconds INTEGER,
      status TEXT NOT NULL CHECK(status IN ('active','paused','stopped','completed','queued','submitted')),
      metadata TEXT NOT NULL, claim_token TEXT, claim_until TEXT,
      generation INTEGER NOT NULL DEFAULT 0
    )
    ''')
    raw.execute(
        "INSERT INTO responsibilities(id,title,due_at,timezone,interval_seconds,status,metadata,generation) VALUES (?,?,?,?,?,?,?,?)",
        ('leg-1', 'Legacy Task', '2026-01-01T00:00:00.000000+00:00', 'UTC', None, 'active', '{"a":1}', 2),
    )
    raw.commit()
    raw.close()

    with ResponsibilityStore(db_path) as store:
        item = store.get('leg-1')
        assert item['title'] == 'Legacy Task'
        assert item['kind'] == 'commitment'
        assert item['route_id'] is None
        assert item['stage'] == -1
        assert item['nudge_count'] == 0
        assert item['last_nudged_at'] is None
        assert item['deadline_at'] is None
        assert item['completed_day'] is None
        assert item['metadata'] == {'a': 1}
        assert item['generation'] == 2

    # Second open is idempotent
    with ResponsibilityStore(db_path) as store:
        assert store.get('leg-1')['kind'] == 'commitment'


def test_kinds_update_reopen_and_delete(tmp_path):
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(days=2)
    assert VALID_KINDS == ('commitment', 'non_negotiable', 'check_in', 'watch', 'open_loop')

    with ResponsibilityStore(tmp_path / 'store.sqlite3') as store:
        for kind in VALID_KINDS:
            created = store.create(f'item-{kind}', now, kind=kind)
            assert created['kind'] == kind
            assert store.delete(created['id']) is True
            assert store.delete(created['id']) is False

        try:
            store.create('bad-kind', now, kind='invalid_kind')
            assert False, 'expected ValueError'
        except ValueError:
            pass

        item = store.create(
            'Draft memo',
            now,
            timezone_name='America/New_York',
            interval_seconds=3600,
            metadata={'p': 1},
            kind='open_loop',
            route_id='sk:0123456789abcdef',
            deadline_at=deadline,
        )
        assert item['deadline_at'] == deadline
        assert item['route_id'] == 'sk:0123456789abcdef'

        claim = store.claim_due(now)[0]
        assert store.claim_is_current(item['id'], claim['claim_token'], now)

        # update_item clearsnullable fields when None is explicitly passed, invalidates claims, increments generation
        updated = store.update_item(
            item['id'],
            title='Final memo',
            interval_seconds=None,
            deadline_at=None,
            route_id=None,
            kind='commitment',
            metadata={'p': 2},
        )
        assert updated['title'] == 'Final memo'
        assert updated['interval_seconds'] is None
        assert updated['deadline_at'] is None
        assert updated['route_id'] is None
        assert updated['kind'] == 'commitment'
        assert updated['metadata'] == {'p': 2}
        assert updated['claim_token'] is None
        assert updated['claim_until'] is None
        assert updated['generation'] == item['generation'] + 1
        assert not store.claim_is_current(item['id'], claim['claim_token'], now)
        assert store.update_item('missing-id', title='nope') is None

        # reopen transitions from stopped/completed/submitted/paused back to active and resets stage + completed_day
        assert store.stop(item['id']) is True
        new_due = now + timedelta(hours=5)
        assert store.reopen(item['id'], due_at=new_due) is True
        reopened = store.get(item['id'])
        assert reopened['status'] == 'active'
        assert reopened['due_at'] == new_due
        assert reopened['stage'] == -1
        assert reopened['completed_day'] is None
        assert store.reopen('missing-id') is False


def test_daily_non_negotiables_mark_done_and_rollover(tmp_path):
    now = datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc)
    morning_next = datetime(2026, 4, 11, 13, 0, tzinfo=timezone.utc)

    with ResponsibilityStore(tmp_path / 'store.sqlite3') as store:
        workout = store.create('Morning workout', now, kind='non_negotiable')
        journal = store.create('Daily journal', now, kind='non_negotiable')
        already_today = store.create('Hydrate', now, kind='non_negotiable')
        paused_nn = store.create('Paused habit', now, kind='non_negotiable')
        one_off = store.create('Tax filing', now, kind='commitment')

        # Advance workout once, then mark done on 2026-04-10
        claim_w = store.claim_due(now, limit=10)
        token_map = {c['id']: c['claim_token'] for c in claim_w}
        assert store.advance_followup(workout['id'], token_map[workout['id']], now, stage=1)
        assert store.mark_non_negotiable_done(workout['id'], '2026-04-10') is True
        w_done = store.get(workout['id'])
        assert w_done['status'] == 'completed'
        assert w_done['completed_day'] == '2026-04-10'
        assert w_done['nudge_count'] == 1

        # Submit journal (completed_day stays NULL)
        assert store.finish_claim(journal['id'], token_map[journal['id']], now) is True
        assert store.get(journal['id'])['status'] == 'submitted'

        # Mark already_today done for 2026-04-11 already
        assert store.mark_non_negotiable_done(already_today['id'], '2026-04-11') is True

        # Pause paused_nn and complete ordinary commitment
        assert store.pause(paused_nn['id']) is True
        assert store.complete(one_off['id']) is True

        # Rollover for 2026-04-11 resets workout (2026-04-10 != 2026-04-11) and journal (completed_day IS NULL)
        reset_count = store.reset_daily_non_negotiables('2026-04-11', morning_next)
        assert reset_count == 2

        w_after = store.get(workout['id'])
        assert w_after['status'] == 'active'
        assert w_after['stage'] == -1
        assert w_after['nudge_count'] == 0
        assert w_after['completed_day'] is None
        assert w_after['due_at'] == morning_next

        j_after = store.get(journal['id'])
        assert j_after['status'] == 'active'
        assert j_after['due_at'] == morning_next

        # Untouched items
        assert store.get(already_today['id'])['status'] == 'completed'
        assert store.get(already_today['id'])['completed_day'] == '2026-04-11'
        assert store.get(paused_nn['id'])['status'] == 'paused'
        assert store.get(one_off['id'])['status'] == 'completed'

        # Re-running rollover on same day is a no-op
        assert store.reset_daily_non_negotiables('2026-04-11', morning_next) == 0


def test_advance_followup_and_defer_claim_fencing_and_wake_logs(tmp_path):
    now = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    with ResponsibilityStore(tmp_path / 'store.sqlite3') as store:
        item = store.create('Follow up with Alex', now)
        claim = store.claim_due(now, lease_seconds=60)[0]

        # Wrong token fails closed
        assert store.advance_followup(item['id'], 'wrong-token', now, stage=0) is False
        assert store.defer_claim(item['id'], 'wrong-token', now, until=now + timedelta(hours=2)) is False

        # Expired lease fails closed
        expired_now = now + timedelta(seconds=61)
        assert store.advance_followup(item['id'], claim['claim_token'], expired_now, stage=0) is False
        assert store.defer_claim(item['id'], claim['claim_token'], expired_now, until=now + timedelta(hours=2)) is False

        # Valid defer_claim updates due_at without incrementing nudge_count
        deferred_until = now + timedelta(hours=1)
        assert store.defer_claim(item['id'], claim['claim_token'], now + timedelta(seconds=10), until=deferred_until) is True
        after_defer = store.get(item['id'])
        assert after_defer['due_at'] == deferred_until
        assert after_defer['nudge_count'] == 0
        assert after_defer['claim_token'] is None

        # Re-claim at deferred_until and advance_followup
        claim2 = store.claim_due(deferred_until, lease_seconds=60)[0]
        next_due = deferred_until + timedelta(hours=3)
        nudge_time = deferred_until + timedelta(seconds=5)
        assert store.advance_followup(
            item['id'],
            claim2['claim_token'],
            nudge_time,
            next_due_at=next_due,
            stage=2,
            next_status='active',
        ) is True
        after_adv = store.get(item['id'])
        assert after_adv['stage'] == 2
        assert after_adv['nudge_count'] == 1
        assert after_adv['last_nudged_at'] == nudge_time
        assert after_adv['due_at'] == next_due
        assert after_adv['claim_token'] is None

        # advance_followup with next_due_at=None preserves existing due_at and can transition status
        claim3 = store.claim_due(next_due, lease_seconds=60)[0]
        assert store.advance_followup(
            item['id'],
            claim3['claim_token'],
            next_due,
            next_due_at=None,
            stage=3,
            next_status='submitted',
        ) is True
        final_state = store.get(item['id'])
        assert final_state['status'] == 'submitted'
        assert final_state['due_at'] == next_due
        assert final_state['nudge_count'] == 2

        # Wake logs recorded and listed newest first
        store.record_wake_log(item['id'], now, 'nudge', 'deferred', 'quiet_hours')
        store.record_wake_log(item['id'], now + timedelta(minutes=5), 'nudge', 'sent', 'stage=2')
        store.record_wake_log(None, now + timedelta(minutes=5), 'rollover', 'ok', 'reset=1')

        logs = store.list_wake_logs(limit=2)
        assert len(logs) == 2
        assert logs[0]['action'] == 'rollover'
        assert logs[0]['item_id'] is None
        assert logs[1]['action'] == 'nudge'
        assert logs[1]['outcome'] == 'sent'

        all_logs = store.list_wake_logs()
        assert len(all_logs) == 3
        assert all_logs[-1]['outcome'] == 'deferred'


if __name__ == '__main__':
    unittest.main()
