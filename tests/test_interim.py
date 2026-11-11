import pytest

from butler.interim import interim_message


def test_judge_prose_is_never_emitted():
    assert interim_message('Yes, he should receive this message because it is directly relevant') is None


def test_explicit_send_returns_only_message():
    assert interim_message('{"send":true,"message":"Hello"}') == 'Hello'


@pytest.mark.parametrize('output', [
    None, RuntimeError('judge failed'), {}, '', 'SKIP', 'not json',
    '{"send":false,"message":"Hello"}',
    '{"send":true,"message":""}', '{"send":true,"message":"  "}',
    '{"send":true}', '{"message":"Hello"}', '{}',
    '{"send":true,"message":"Hello","reason":"relevant"}',
    '{"send":1,"message":"Hello"}', '{"send":"true","message":"Hello"}',
    '{"send":true,"message":null}', '{"send":true,"message":123}',
    '[]', 'null', '"Hello"',
    '{"send":false,"send":true,"message":"Hello"}',
    'prefix {"send":true,"message":"Hello"}',
    '{"send":true,"message":"Hello"} trailing',
])
def test_invalid_negative_or_failed_decisions_are_not_emitted(output):
    assert interim_message(output) is None
