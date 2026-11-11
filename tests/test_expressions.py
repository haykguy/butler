import json

import pytest
from overseer.expressions import register_expression_tools


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
    assert bridge.tool['name'] == 'overseer_expression'
    assert invoke(bridge, action='capabilities')['capabilities'] == {'react': True, 'reply': True, 'effect': True}
    assert not bridge.calls


@pytest.mark.parametrize('action,extra,expected', [
    ('react', {'message_id': 'm1', 'emoji': '👍'}, {'message_id': 'm1', 'emoji': '👍', 'remove': False}),
    ('react', {'message_id': 'm1', 'remove': True}, {'message_id': 'm1', 'remove': True}),
    ('reply', {'message_id': 'm1', 'text': 'hello', 'effect': 'slam'}, {'message_id': 'm1', 'text': 'hello', 'effect': 'slam'}),
    ('effect', {'text': 'hello', 'effect': 'confetti'}, {'text': 'hello', 'effect': 'confetti'}),
])
def test_dispatch(bridge, action, extra, expected):
    assert invoke(bridge, action=action, chat_id='chat-1', **extra)['ok'] is True
    assert bridge.calls == [('imessage_' + action, expected)]


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


def test_failure_and_malformed_response(bridge):
    bridge.response = '{"ok": false, "error": "backend failure"}'
    assert invoke(bridge, action='effect', chat_id='chat-1', text='x', effect='slam')['error'] == 'backend failure'
    bridge.response = '{}'
    assert invoke(bridge, action='effect', chat_id='chat-1', text='x', effect='slam')['ok'] is False
