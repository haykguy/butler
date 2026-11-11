"""Gateway-admission-started scheduler. Injection is acceptance, not delivery."""
import logging
import threading

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, ctx, store_factory, *, poll_seconds=30):
        self.ctx, self.store_factory, self.poll_seconds = ctx, store_factory, poll_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def tick(self):
        # A persisted session_key alone is not proof of an admitted, consenting
        # caller. Until the public SDK supplies a trustworthy turn binding,
        # neither legacy metadata nor environment values may authorize a wake.
        return

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
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                log.warning('Butler scheduler did not stop within 5 seconds')
