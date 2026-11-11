from butler.events import Event, EventLedger


def test_uncommitted_events_replay_after_restart(tmp_path):
    path = tmp_path / 'ledger.sqlite'
    event = Event('p', 'sms', 'c', 'id1', 'incoming', 'unknown', '2026-01-01T00:00:00Z', 'hello')
    with EventLedger(path) as ledger:
        assert ledger.append(event)
        assert ledger.read('p', 'sms', 'c', 'session').events[0].text == 'hello'
    with EventLedger(path) as ledger:
        batch = ledger.read('p', 'sms', 'c', 'session')
        assert batch.events == (event,)
        ledger.commit(batch)
    with EventLedger(path) as ledger:
        assert ledger.read('p', 'sms', 'c', 'session').events == ()


from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import pytest
from butler.events import Batch, CheckpointConflict, EventConflict, LedgerFull


def event(id="1", **changes):
    return replace(Event("p", "sms", "c", id, "assistant", "unknown", "2026-01-01T00:00:00+00:00", "untrusted 📬"), **changes)


def test_dedup_conflict_scope_and_states(tmp_path):
    with EventLedger(tmp_path / "db") as db:
        for state in ("attempted", "accepted", "delivered", "unknown"):
            assert db.append(event(state, delivery_state=state))
        assert not db.append(event("unknown"))
        with pytest.raises(EventConflict):
            db.append(event("unknown", text="changed"))
        assert db.append(event("unknown", conversation="other"))
        assert [x.delivery_state for x in db.read("p", "sms", "c", "s").events] == ["attempted", "accepted", "delivered", "unknown"]
        assert all(x.untrusted for x in db.read("p", "sms", "c", "s").events)


def test_independent_sessions_and_scopes_with_pagination(tmp_path):
    path = tmp_path / "db"
    with EventLedger(path) as db:
        for e in (event("1"), event("2", conversation="other"), event("3"), event("4", profile="q"), event("5", channel="mail")):
            db.append(e)
        first = db.read("p", "sms", "c", "s", limit=1)
        assert [e.event_id for e in first.events] == ["1"]
        db.commit(first)
        assert [e.event_id for e in db.read("p", "sms", "c", "s").events] == ["3"]
        assert [e.event_id for e in db.read("p", "sms", "c", "other-session").events] == ["1", "3"]
        for scope, expected in ((("p", "sms", "other"), "2"), (("q", "sms", "c"), "4"), (("p", "mail", "c"), "5")):
            assert [e.event_id for e in db.read(*scope, "s").events] == [expected]
    with EventLedger(path) as db:
        assert [e.event_id for e in db.read("p", "sms", "c", "s").events] == ["3"]


def test_stale_and_modified_batch_cannot_acknowledge(tmp_path):
    with EventLedger(tmp_path / "db") as db:
        db.append(event("1")); db.append(event("2"))
        batch = db.read("p", "sms", "c", "s", limit=1)
        forged = replace(batch, sequences=(batch.sequences[0] + 1,), events=(event("2"),))
        with pytest.raises(CheckpointConflict): db.commit(forged)
        assert len(db.read("p", "sms", "c", "s").events) == 2
        db.commit(batch)
        with pytest.raises(CheckpointConflict): db.commit(batch)
        assert [e.event_id for e in db.read("p", "sms", "c", "s").events] == ["2"]


def test_overflow_rejects_without_loss_and_dedup_still_works(tmp_path):
    path = tmp_path / "db"
    with EventLedger(path, max_events=2, max_text_bytes=4) as db:
        assert db.append(event("1", text="é"))
        with pytest.raises(LedgerFull): db.append(event("2", text="éé"))
        assert db.append(event("2", text="ab"))
        with pytest.raises(LedgerFull): db.append(event("3", text=""))
        assert not db.append(event("1", text="é"))
    with EventLedger(path, max_events=2, max_text_bytes=4) as db:
        assert [e.text for e in db.read("p", "sms", "c", "s").events] == ["é", "ab"]
    with pytest.raises(ValueError): EventLedger(path, max_events=3, max_text_bytes=4)


def test_input_validation_and_failed_append_no_partial_write(tmp_path):
    with EventLedger(tmp_path / "db") as db:
        for bad in (event(""), event(delivery_state="sent"), event(timestamp="2026-01-01"), event(text=None)):
            with pytest.raises(ValueError): db.append(bad)
        with pytest.raises(ValueError): db.read("p", "sms", "c", "s", limit=0)
        assert db.read("p", "sms", "c", "s").events == ()


def test_concurrent_dedup_and_checkpoint_compare_and_swap(tmp_path):
    path = tmp_path / "db"
    with EventLedger(path): pass
    def append(_):
        with EventLedger(path) as db: return db.append(event())
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(pool.map(append, range(8))) == [False] * 7 + [True]
    with EventLedger(path) as a, EventLedger(path) as b:
        one = a.read("p", "sms", "c", "s")
        two = b.read("p", "sms", "c", "s")
        a.commit(one)
        with pytest.raises(CheckpointConflict): b.commit(two)
