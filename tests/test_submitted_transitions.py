from datetime import datetime, timezone

import pytest

from butler.store import ResponsibilityStore


@pytest.mark.parametrize(('action', 'expected'), [('complete', 'completed'), ('stop', 'stopped'), ('pause', 'paused')])
def test_submitted_followup_can_be_resolved(tmp_path, action, expected):
    store = ResponsibilityStore(tmp_path / 'store.sqlite')
    try:
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        item = store.create('Reminder', now)
        claim = store.claim_due(now)[0]
        assert store.finish_claim(item['id'], claim['claim_token'], now)
        assert store.get(item['id'])['status'] == 'submitted'
        assert getattr(store, action)(item['id']) is True
        assert store.get(item['id'])['status'] == expected
    finally:
        store.close()
