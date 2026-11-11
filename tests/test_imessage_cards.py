import json
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from wsgiref.simple_server import make_server, WSGIRequestHandler

import pytest
from concurrent.futures import ThreadPoolExecutor
from butler.imessage_cards import CardStore, card_app, card_url, send_card


def test_real_http_submission_is_durable_and_single_use(tmp_path):
    store = CardStore(tmp_path / 'private')
    token = store.create('Lunch?', ['Pizza', 'Salad'], ttl=60, non_sensitive=True)
    class Quiet(WSGIRequestHandler):
        def log_message(self, *args):
            pass
    server = make_server('127.0.0.1', 0, card_app(store), handler_class=Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/submit'
        body = json.dumps({'token': token, 'choice': 1}).encode()
        def post():
            request = Request(url, body, {'Content-Type': 'application/json'}, method='POST')
            try:
                with urlopen(request) as response:
                    return response.status
            except HTTPError as exc:
                return exc.code
        assert post() == 200
        assert post() == 409
        event = store.pending()[0]
        assert event['choice'] == 'Salad'
        assert event['authentication'] == 'bearer_only'
        store.ack(event['id'])
        assert CardStore(tmp_path / 'private').pending() == []
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_delivery_acknowledgement_and_ambiguous_failure(tmp_path):
    store = CardStore(tmp_path / 'private')
    calls = []
    def transport(path, body):
        calls.append((path, body))
        return {'ok': True}
    result = send_card(store, transport, 'chat', 'https://cards.example', 'Lunch?', ['A', 'B'], ttl=60, non_sensitive=True)
    assert result['delivery'] == 'acknowledged'
    assert calls[0][0] == '/send-app'
    assert calls[0][1]['spaceId'] == 'chat'
    assert calls[0][1]['url'].startswith('https://cards.example/card#')
    def fails(*args):
        raise TimeoutError('secret')
    assert send_card(store, fails, 'chat', 'https://cards.example', 'Lunch?', ['A', 'B'], ttl=60, non_sensitive=True)['delivery'] == 'unknown'


def test_expiry_races_and_private_storage(tmp_path):
    clock = [100.0]
    store = CardStore(tmp_path / 'private', clock=lambda: clock[0])
    token = store.create('Pick', ['A', 'B'], ttl=1, non_sensitive=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: store.submit(token, 0), range(8)))
    assert outcomes.count(True) == 1
    expired = store.create('Pick', ['A', 'B'], ttl=1, non_sensitive=True)
    clock[0] = 101
    assert store.view(expired) is None
    assert not store.submit(expired, 0)
    with pytest.raises(ValueError):
        store.create('Sensitive', ['A', 'B'], ttl=60)
    (tmp_path / 'alias').symlink_to(tmp_path / 'private')
    with pytest.raises(ValueError):
        CardStore(tmp_path / 'alias')


@pytest.mark.parametrize('origin', ['https://cards.example\n.evil', 'https://cards.example:99999', 'https://cards.example\\evil'])
def test_origin_rejected_if_not_valid_for_sidecar(origin):
    with pytest.raises(ValueError):
        card_url(origin, 'A' * 43)
