import pytest

from overseer.interim import interim_message


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


def test_owned_post_never_posts_exact_leaked_judge_prose():
    from overseer.interim import post_judged_interim
    class Context:
        def __init__(self): self.sent = []
        def post_message(self, text): self.sent.append(text)
    ctx = Context()
    post_judged_interim(ctx, lambda: "YES. Needs Hayk's input.")
    assert ctx.sent == []


def test_owned_post_sends_only_approved_message_and_fails_closed_on_judge_error():
    from overseer.interim import post_judged_interim
    class Context:
        def __init__(self): self.sent = []
        def post_message(self, text): self.sent.append(text); return 'handle'
    ctx = Context()
    assert post_judged_interim(ctx, lambda: '{"send":true,"message":"Checking."}') == 'handle'
    def failed(): raise RuntimeError('YES. Needs Hayk\'s input.')
    assert post_judged_interim(ctx, failed) is None
    assert ctx.sent == ['Checking.']
