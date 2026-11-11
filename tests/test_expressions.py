import json

import pytest
from butler.expressions import register_expression_tools


class Bridge:
    def __init__(self):
        self.calls = []
        self.response = '{"ok": true, "messageId": "sent"}'

    def register_tool(self, **kw):
        self.tool = kw

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return self.response


@pytest.fixture
def bridge():
    bridge = Bridge()
    register_expression_tools(bridge, lambda: ('photon', 'chat-1'))
    return bridge


def invoke(bridge, **kwargs):
    return json.loads(bridge.tool['handler'](kwargs))


def test_registration_and_capabilities(bridge):
    assert bridge.tool['name'] == 'butler_expression'
    assert invoke(bridge, action='capabilities')['capabilities'] == {'react': False, 'reply': False, 'effect': True}
    assert not bridge.calls


@pytest.mark.parametrize('args', [
    {'action': 'reply', 'chat_id': 'other', 'message_id': 'm', 'text': 'x'},
    {'action': 'reply', 'chat_id': 'chat-1', 'message_id': '', 'text': 'x'},
    {'action': 'reply', 'chat_id': 'chat-1', 'message_id': ' m', 'text': 'x'},
    {'action': 'react', 'chat_id': 'chat-1', 'message_id': 'm'},
    {'action': 'react', 'chat_id': 'chat-1', 'message_id': 'm', 'remove': 'yes'},
    {'action': 'effect', 'chat_id': 'chat-1', 'text': 'x', 'effect': []},
    {'action': 'effect', 'chat_id': 'chat-1', 'text': '', 'effect': 'slam'},
])
def test_invalid_arguments_never_dispatch(bridge, args):
    assert invoke(bridge, **args)['ok'] is False
    assert not bridge.calls


def test_telegram_bridge_gap():
    b = Bridge()
    register_expression_tools(b, lambda: ('telegram', '123'))
    assert not any(invoke(b, action='capabilities')['capabilities'].values())
    assert invoke(b, action='reply', chat_id='123', message_id='9', text='x')['ok'] is False
    assert not b.calls


def test_missing_session_does_not_use_home_channel():
    b = Bridge()
    register_expression_tools(b, lambda: ('photon', ''))
    assert invoke(b, action='effect', chat_id='chat-1', text='x', effect='slam')['ok'] is False
    assert not b.calls


def test_failure_and_malformed_response(bridge, monkeypatch):
    import butler.expressions as expressions
    monkeypatch.setattr(expressions, '_sidecar', lambda *args: {'ok': False, 'error': 'backend failure'})
    assert invoke(bridge, action='effect', chat_id='chat-1', text='x', effect='slam')['error'] == 'backend failure'
    monkeypatch.setattr(expressions, '_sidecar', lambda *args: {})
    assert invoke(bridge, action='effect', chat_id='chat-1', text='x', effect='slam')['ok'] is False


def test_effect_uses_native_sidecar_without_tool_dispatch(monkeypatch, tmp_path):
    import butler.expressions as expressions
    calls = []
    monkeypatch.setattr(expressions, '_sidecar', lambda path, body: calls.append((path, body)) or {'ok': True})
    b = Bridge()
    register_expression_tools(b, lambda: ('photon', 'chat-1'))
    assert invoke(b, action='effect', chat_id='chat-1', text='hello', effect='slam')['ok'] is True
    assert calls == [('/send-effect', {'spaceId': 'chat-1', 'text': 'hello', 'effect': 'slam'})]
    assert b.calls == []


def test_message_operations_fail_closed_when_membership_unproven(bridge):
    assert invoke(bridge, action='react', chat_id='chat-1', message_id='m1', emoji='👍')['ok'] is False
    assert invoke(bridge, action='reply', chat_id='chat-1', message_id='m1', text='x')['ok'] is False
    assert not bridge.calls
    assert invoke(bridge, action='capabilities')['capabilities'] == {'react': False, 'reply': False, 'effect': True}


def test_sidecar_uses_loopback_and_runtime_token(tmp_path, monkeypatch):
    import butler.expressions as expressions
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime' / 'photon-sidecar.json').write_text(json.dumps({'port': 12345, 'token': 'test-token'}))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"ok": true}'
    def fake_urlopen(req, timeout):
        calls.append((req.full_url, req.get_header('X-hermes-sidecar-token'), json.loads(req.data)))
        return Response()
    monkeypatch.setattr(expressions.urllib.request, 'build_opener', lambda *handlers: type('Opener', (), {'open': staticmethod(fake_urlopen)})())
    assert expressions._sidecar('/send-effect', {'spaceId': 'chat-1'}) == {'ok': True}
    assert calls == [('http://127.0.0.1:12345/send-effect', 'test-token', {'spaceId': 'chat-1'})]


def test_sidecar_refuses_http_redirects():
    import butler.expressions as expressions
    import urllib.request
    handler = expressions._NoRedirect()
    request = urllib.request.Request('http://127.0.0.1:1234/send-effect')
    assert handler.redirect_request(request, None, 302, 'redirect', {}, 'http://example.invalid/') is None
