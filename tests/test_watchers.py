import unittest
import sqlite3
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from overseer.watchers import WatchPolicy, WatcherEngine


class WatcherTests(unittest.TestCase):
    def test_concurrent_checks_observe_in_transaction_order(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as d:
            db_path = str(Path(d) / 'state.db')
            first_observed = threading.Event()
            release_first = threading.Event()
            second_observed = threading.Event()
            first_ready = threading.Event()
            second_ready = threading.Event()
            start_first = threading.Event()
            start_second = threading.Event()
            results = {}
            errors = []

            def run(name):
                db = sqlite3.connect(db_path, isolation_level=None, timeout=5)
                try:
                    def probe():
                        if name == 'first':
                            first_observed.set()
                            if not release_first.wait(5):
                                raise RuntimeError('first check timed out')
                            return 'old'
                        second_observed.set()
                        return 'new'
                    engine = WatcherEngine(WatchPolicy(probes={'p': probe}), db)
                    (first_ready if name == 'first' else second_ready).set()
                    if not (start_first if name == 'first' else start_second).wait(5):
                        raise RuntimeError('check start timed out')
                    results[name] = engine.check('w', {'kind': 'probe', 'name': 'p'})
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    db.close()

            first = threading.Thread(target=run, args=('first',))
            second = threading.Thread(target=run, args=('second',))
            first.start()
            second.start()
            try:
                self.assertTrue(first_ready.wait(5))
                self.assertTrue(second_ready.wait(5))
                start_first.set()
                self.assertTrue(first_observed.wait(5))
                start_second.set()
                self.assertFalse(second_observed.wait(0.3), 'second observation overtook first')
            finally:
                start_first.set()
                start_second.set()
                release_first.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(results['first'].changed)
            self.assertTrue(results['second'].changed)
            self.assertEqual(results['second'].previous_digest, results['first'].digest)

    def test_file_change_baseline_and_persistent_state(self):
        from overseer.store import ResponsibilityStore
        with TemporaryDirectory(dir=Path(__file__).parent) as d:
            root = Path(d).resolve()
            target = root / 'value.txt'
            target.write_text('one')
            store = ResponsibilityStore(root / 'state.db')
            engine = WatcherEngine(WatchPolicy(files={str(target)}), store.db)
            spec = {'kind': 'file', 'path': str(target)}
            self.assertFalse(engine.check('w', spec).changed)
            store.close()
            target.write_text('two')
            store = ResponsibilityStore(root / 'state.db')
            self.assertTrue(WatcherEngine(WatchPolicy(files={str(target)}), store.db).check('w', spec).changed)
            store.close()

    def test_unapproved_file_and_symlink_rejected(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as d:
            target = Path(d).resolve() / 'a'
            target.write_text('secret')
            link = Path(d).resolve() / 'link'
            link.symlink_to(target)
            engine = WatcherEngine(WatchPolicy(files={str(link)}))
            with self.assertRaises(ValueError):
                engine.check('x', {'kind': 'file', 'path': str(target)})
            with self.assertRaises(ValueError):
                engine.check('x', {'kind': 'file', 'path': str(link)})

    def test_probe_allowlist_and_change(self):
        values = iter(['one', 'two'])
        engine = WatcherEngine(WatchPolicy(probes={'status': lambda: next(values)}))
        self.assertFalse(engine.check('w', {'kind': 'probe', 'name': 'status'}).changed)
        self.assertTrue(engine.check('w', {'kind': 'probe', 'name': 'status'}).changed)
        with self.assertRaises(ValueError):
            engine.check('w', {'kind': 'probe', 'name': 'unknown'})

    def test_urls_fail_closed_without_trusted_transport(self):
        engine = WatcherEngine(WatchPolicy(urls={'https://example.com/status'}))
        with self.assertRaises(ValueError):
            engine.check('w', {'kind': 'url', 'url': 'http://127.0.0.1/'})
        with self.assertRaises(ValueError):
            engine.check('w', {'kind': 'url', 'url': 'https://example.com/status'})

    def test_spec_change_rebaselines_and_errors_preserve_state(self):
        values = iter(['same', 'changed'])
        engine = WatcherEngine(WatchPolicy(probes={'a': lambda: next(values), 'b': lambda: 'other'}))
        self.assertFalse(engine.check('w', {'kind': 'probe', 'name': 'a'}).changed)
        with self.assertRaises(ValueError):
            engine.check('w', {'kind': 'unknown'})
        self.assertTrue(engine.check('w', {'kind': 'probe', 'name': 'a'}).changed)
        self.assertFalse(engine.check('w', {'kind': 'probe', 'name': 'b'}).changed)
