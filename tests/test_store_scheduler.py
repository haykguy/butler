import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from butler.store import ResponsibilityStore


def test_finish_claim_checks_expiry_after_waiting_for_write_lock():
    import threading
    from datetime import timedelta
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
        from datetime import timedelta
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
        from datetime import timedelta
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
        from datetime import timedelta
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

if __name__ == '__main__':
    unittest.main()
