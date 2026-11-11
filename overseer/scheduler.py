"""Explicitly driven scheduler; no background thread or Hermes integration."""
from datetime import datetime, timezone


class Scheduler:
    def __init__(self, store, *, batch_size=10, lease_seconds=300):
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError('batch_size must be a positive integer')
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError('lease_seconds must be a positive integer')
        self.store = store
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds

    def claim(self, now=None):
        return self.store.claim_due(now or datetime.now(timezone.utc), limit=self.batch_size, lease_seconds=self.lease_seconds)
