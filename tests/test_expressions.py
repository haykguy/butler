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
    assert invoke(bridge, action='capabilities')['capabilities'] == {'react': False, 'reply': False, 'effect': False}
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


def test_effect_fails_closed_without_public_route(bridge):
    response = invoke(bridge, action='effect', chat_id='chat-1', text='hello', effect='slam')
    assert response['ok'] is False
    assert 'send_effect' in response['error']
    assert not bridge.calls


def test_never_reads_hermes_runtime_secret_store():
    import butler.expressions as expressions
    import butler.imessage_tools as imessage_tools
    from pathlib import Path
    for module in (expressions, imessage_tools):
        source = Path(module.__file__).read_text()
        assert 'photon-sidecar' not in source
        assert "'runtime'" not in source and '"runtime"' not in source
        assert 'urllib' not in source
    assert not hasattr(expressions, '_sidecar')


def test_message_operations_fail_closed_when_membership_unproven(bridge):
    assert invoke(bridge, action='react', chat_id='chat-1', message_id='m1', emoji='👍')['ok'] is False
    assert invoke(bridge, action='reply', chat_id='chat-1', message_id='m1', text='x')['ok'] is False
    assert not bridge.calls
    assert invoke(bridge, action='capabilities')['capabilities'] == {'react': False, 'reply': False, 'effect': False}
