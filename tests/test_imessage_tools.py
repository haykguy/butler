import json
from pathlib import Path

from butler.imessage_tools import register_imessage_tools


class Context:
    def register_tool(self, **kwargs):
        self.tool = kwargs


class MediaCache:
    """Stands in for gateway.platforms.base.cache_*_from_bytes."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.calls = []

    def __call__(self, kind, data, ext):
        path = self.root / f'{kind}_{len(self.calls)}{ext}'
        path.write_bytes(data)
        self.calls.append((kind, ext, path))
        return str(path)


def setup(cache=None, session=lambda: ('photon', 'chat-A'), ctx=None):
    ctx = ctx or Context()
    register_imessage_tools(ctx, session_provider=session, media_cache=cache)
    return lambda **args: json.loads(ctx.tool['handler'](args))


def test_poll_and_effect_fail_closed_without_public_route():
    calls = []
    run = setup(lambda *a: calls.append(a))
    poll = run(action='poll', title='Lunch?', options=['A', 'B'])
    assert not poll['ok'] and 'send_poll' in poll['error']
    effect = run(action='effect', text='hello', effect='slam')
    assert not effect['ok'] and 'send_effect' in effect['error']
    assert not calls


def test_message_targeting_fails_closed():
    calls = []
    run = setup(lambda *args: calls.append(args))
    for action in ('react', 'reply', 'edit', 'unsend'):
        result = run(action=action, message_id='some-id', text='hello', emoji='👍')
        assert not result['ok'] and 'membership' in result['error']
    assert not run(action='voice_note', audio_path='/tmp/test.m4a', reply_to_message_id='x')['ok']
    assert not calls


def test_session_and_card_fail_closed():
    calls = []
    run = setup(lambda *args: calls.append(args), lambda: ('telegram', 'chat-A'))
    assert not run(action='send_image', path='/tmp/x.png')['ok']
    run = setup(lambda *args: calls.append(args))
    assert not run(action='card', title='x', choices=['a'])['ok']
    assert not run(action='thread')['ok']
    assert not run(action='background', clear=True)['ok']
    assert not run(action='contact_card')['ok']
    assert not calls


def test_entrypoint_registers_namespaced_tool():
    import importlib.util
    import sys
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('butler_imessage_plugin_test', root / '__init__.py', submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    class PluginContext:
        def __init__(self): self.tools = {}
        def register_tool(self, **kw): self.tools[kw['name']] = kw
        def register_hook(self, *args): pass
        def on_unload(self, callback): pass
    try:
        spec.loader.exec_module(module)
        ctx = PluginContext()
        module.register(ctx)
        assert 'butler_imessage' in ctx.tools
    finally:
        sys.modules.pop(spec.name, None)


def test_mismatched_chat_fails_closed(tmp_path):
    cache = MediaCache(tmp_path / 'cache')
    run = setup(cache)
    assert not run(action='send_image', chat_id='other', path='/tmp/x.png')['ok']
    assert not cache.calls


def test_local_file_operations_require_trusted_provenance(tmp_path):
    secret = tmp_path / 'private.png'
    secret.write_bytes(b'private')
    audio = secret.with_suffix('.m4a')
    audio.write_bytes(b'private')
    cache = MediaCache(tmp_path / 'cache')
    run = setup(cache)
    assert not run(action='send_image', path=str(secret))['ok']
    assert not run(action='voice_note', audio_path=str(audio))['ok']
    assert not run(action='image_edit', input_path=str(secret), operations=[])['ok']
    assert not cache.calls


def test_send_image_returns_media_directive_for_reply(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / 'image_cache').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    image = home / 'image_cache' / 'test.png'
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    cache = MediaCache(tmp_path / 'cache')
    run = setup(cache)
    response = run(action='send_image', path=str(image), caption='hello')
    assert response['ok'], response
    assert response['delivery'] == 'reply_attachment'
    assert response['reply_directive'] == f"MEDIA:{response['media_path']}"
    assert response['caption'] == 'hello'
    assert cache.calls[0][:2] == ('image', '.png')
    assert Path(response['media_path']).read_bytes() == image.read_bytes()


def test_voice_note_converts_authorized_audio_to_voice_attachment(tmp_path, monkeypatch):
    import wave
    home = tmp_path / 'home'
    (home / 'image_cache').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    audio = home / 'image_cache' / 'test.wav'
    with wave.open(str(audio), 'wb') as out:
        out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        out.writeframes(b'\0\0' * 8000)
    cache = MediaCache(tmp_path / 'cache')
    run = setup(cache)
    response = run(action='voice_note', audio_path=str(audio))
    assert response['ok'], response
    assert response['reply_directive'] == f"[[audio_as_voice]]\nMEDIA:{response['media_path']}"
    assert cache.calls[0][:2] == ('audio', '.m4a')
    assert b'ftyp' in Path(response['media_path']).read_bytes()[:16]
    assert not any((home / 'voice_cache').iterdir())
    assert not run(action='voice_note', text='hello')['ok']


def test_text_voice_note_dispatches_tts_then_stages(tmp_path, monkeypatch):
    import wave
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    ctx = Context()
    def dispatch(name, args):
        assert name == 'text_to_speech' and args['text'] == 'hello'
        output = Path(args['output_path']).with_suffix('.wav')
        with wave.open(str(output), 'wb') as out:
            out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            out.writeframes(b'\0\0' * 8000)
        return json.dumps({'success': True, 'file_path': str(output), 'file_paths': [str(output)]})
    ctx.dispatch_tool = dispatch
    cache = MediaCache(tmp_path / 'cache')
    run = setup(cache, ctx=ctx)
    response = run(action='voice_note', text='hello')
    assert response['ok'], response
    assert response['reply_directive'].startswith('[[audio_as_voice]]\nMEDIA:')
    assert b'ftyp' in Path(response['media_path']).read_bytes()[:16]


def test_default_media_cache_uses_public_hermes_helpers(tmp_path, monkeypatch):
    import sys
    import types
    calls = []
    base = types.ModuleType('gateway.platforms.base')
    base.cache_image_from_bytes = lambda data, ext: calls.append(('image', ext)) or str(tmp_path / f'img{ext}')
    base.cache_audio_from_bytes = lambda data, ext: calls.append(('audio', ext)) or str(tmp_path / f'aud{ext}')
    monkeypatch.setitem(sys.modules, 'gateway', types.ModuleType('gateway'))
    monkeypatch.setitem(sys.modules, 'gateway.platforms', types.ModuleType('gateway.platforms'))
    monkeypatch.setitem(sys.modules, 'gateway.platforms.base', base)
    from butler.imessage_tools import _cache_media
    assert _cache_media('image', b'x', '.png').endswith('img.png')
    assert _cache_media('audio', b'x', '.m4a').endswith('aud.m4a')
    assert calls == [('image', '.png'), ('audio', '.m4a')]


def test_image_edit_authorizes_input_and_overlay(tmp_path, monkeypatch):
    import pytest
    Image = pytest.importorskip('PIL.Image')
    home = tmp_path / 'home'
    cache = home / 'image_cache'
    cache.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    source = cache / 'input.png'
    Image.new('RGB', (3, 3), 'red').save(source)
    outside = tmp_path / 'private.png'
    Image.new('RGB', (1, 1), 'blue').save(outside)
    run = setup()
    bad = run(action='image_edit', input_path=str(source), operations=[{'op':'overlay', 'path':str(outside)}])
    assert not bad['ok']
    edited = run(action='image_edit', input_path=str(source), operations=[{'op':'grayscale'}])
    assert edited['ok'], edited
    with Image.open(edited['output_path']) as im:
        assert len(set(im.getpixel((0, 0)))) == 1
    assert not setup(session=lambda: ('telegram', 'x'))(action='image_edit', input_path=str(source), operations=[])['ok']
