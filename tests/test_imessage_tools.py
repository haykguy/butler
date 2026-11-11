import json
from butler.imessage_tools import register_imessage_tools


class Context:
    def register_tool(self, **kwargs):
        self.tool = kwargs


def setup(sidecar, session=lambda: ('photon', 'chat-A')):
    ctx = Context()
    register_imessage_tools(ctx, session_provider=session, sidecar=sidecar)
    return lambda **args: json.loads(ctx.tool['handler'](args))


def test_poll_dispatches_to_active_chat():
    calls = []
    run = setup(lambda path, body: calls.append((path, body)) or {'ok': True})
    assert run(action='poll', title='Lunch?', options=['A', 'B'])['ok']
    assert calls == [('/send-poll', {'spaceId': 'chat-A', 'title': 'Lunch?', 'options': ['A', 'B']})]


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
    assert not run(action='effect', text='x', effect='slam')['ok']
    run = setup(lambda *args: calls.append(args))
    assert not run(action='card', title='x', choices=['a'])['ok']
    assert not run(action='thread')['ok']
    assert not calls


def test_entrypoint_registers_namespaced_tool():
    import importlib.util
    import sys
    from pathlib import Path
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


def test_invalid_effect_does_not_raise_or_dispatch():
    calls = []
    run = setup(lambda *args: calls.append(args))
    assert not run(action='effect', text='hello', effect=[])['ok']
    assert not calls


def test_attachment_and_background_dispatch(tmp_path):
    media = tmp_path / 'audio.m4a'
    media.write_bytes(b'test')
    calls = []
    run = setup(lambda path, body: calls.append((path, body)) or {'ok': True})
    assert not run(action='voice_note', audio_path=str(media))['ok']
    assert not run(action='send_image', path=str(media), caption='caption')['ok']
    before = len(calls)
    assert not run(action='background', clear=True)['ok']
    assert not run(action='contact_card')['ok']
    assert len(calls) == before


def test_real_loopback_sidecar_contract(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, self.headers.get('X-Hermes-Sidecar-Token'), data))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"messageId":"fixture"}')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (tmp_path / 'runtime').mkdir()
        (tmp_path / 'runtime' / 'photon-sidecar.json').write_text(json.dumps({'port': server.server_port, 'token': 'fixture-token'}))
        monkeypatch.setenv('HERMES_HOME', str(tmp_path))
        from butler.expressions import _sidecar
        run = setup(_sidecar)
        assert run(action='poll', title='Lunch?', options=['A', 'B'])['ok']
        assert run(action='effect', text='hello', effect='slam')['ok']
        audio = tmp_path / 'voice.m4a'
        audio.write_bytes(b'fixture')
        assert not run(action='voice_note', audio_path=str(audio))['ok']
        assert not run(action='send_image', path=str(audio))['ok']
        assert calls == [
            ('/send-poll', 'fixture-token', {'spaceId': 'chat-A', 'title': 'Lunch?', 'options': ['A', 'B']}),
            ('/send-effect', 'fixture-token', {'spaceId': 'chat-A', 'text': 'hello', 'effect': 'slam'}),

        ]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_mismatched_chat_and_invalid_sidecar_fail_closed():
    calls = []
    run = setup(lambda *args: calls.append(args))
    assert not run(action='effect', chat_id='other', text='x', effect='slam')['ok']
    assert not calls
    assert not run(action='contact_card')['ok']


def test_local_file_operations_require_trusted_provenance(tmp_path):
    secret = tmp_path / 'private.png'
    secret.write_bytes(b'private')
    audio = secret.with_suffix('.m4a')
    audio.write_bytes(b'private')
    calls = []
    run = setup(lambda *args: calls.append(args) or {'ok': True})
    assert not run(action='send_image', path=str(secret))['ok']
    assert not run(action='voice_note', audio_path=str(audio))['ok']
    assert not run(action='image_edit', input_path=str(secret), operations=[])['ok']
    assert not calls


def test_send_image_dispatches_private_snapshot(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    cache = home / 'image_cache'
    cache.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    image = cache / 'test.png'
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    calls = []
    def sidecar(path, body):
        from pathlib import Path
        calls.append((path, body.copy(), Path(body['path']).read_bytes()))
        return {'ok': True}
    run = setup(sidecar)
    assert run(action='send_image', path=str(image), caption='hello')['ok']
    assert calls[0][0] == '/send-attachment'
    assert calls[0][1]['spaceId'] == 'chat-A'
    assert calls[0][1]['kind'] == 'attachment'
    assert calls[0][1]['caption'] == 'hello'
    assert calls[0][2] == image.read_bytes()
    assert not run(action='send_image', path=str(image), chat_id='other')['ok']
    assert len(calls) == 1


def test_voice_note_converts_authorized_audio(tmp_path, monkeypatch):
    import wave
    from pathlib import Path
    home = tmp_path / 'home'
    cache = home / 'image_cache'
    cache.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    audio = cache / 'test.wav'
    with wave.open(str(audio), 'wb') as out:
        out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        out.writeframes(b'\0\0' * 8000)
    calls = []
    def sidecar(path, body):
        calls.append((path, body.copy(), Path(body['path']).read_bytes()))
        return {'ok': True}
    run = setup(sidecar)
    response = run(action='voice_note', audio_path=str(audio))
    assert response['ok'], response
    assert calls[0][0] == '/send-attachment'
    assert calls[0][1]['kind'] == 'voice'
    assert calls[0][1]['spaceId'] == 'chat-A'
    assert b'ftyp' in calls[0][2][:16]
    assert not Path(calls[0][1]['path']).exists()
    assert not run(action='voice_note', text='hello')['ok']


def test_text_voice_note_dispatches_tts_then_sends(tmp_path, monkeypatch):
    import wave
    from pathlib import Path
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
    calls = []
    def sidecar(path, body):
        calls.append((path, body.copy(), Path(body['path']).read_bytes()))
        return {'ok': True}
    register_imessage_tools(ctx, session_provider=lambda: ('photon', 'chat-A'), sidecar=sidecar)
    response = json.loads(ctx.tool['handler']({'action': 'voice_note', 'text': 'hello'}))
    assert response['ok'], response
    assert calls[0][1]['kind'] == 'voice' and b'ftyp' in calls[0][2][:16]
    assert not Path(calls[0][1]['path']).exists()


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
    run = setup(lambda *a: {'ok': True})
    bad = run(action='image_edit', input_path=str(source), operations=[{'op':'overlay', 'path':str(outside)}])
    assert not bad['ok']
    edited = run(action='image_edit', input_path=str(source), operations=[{'op':'grayscale'}])
    assert edited['ok'], edited
    with Image.open(edited['output_path']) as im:
        assert len(set(im.getpixel((0, 0)))) == 1
    assert not setup(lambda *a: {'ok': True}, lambda: ('telegram', 'x'))(action='image_edit', input_path=str(source), operations=[])['ok']


def test_media_real_loopback_sidecar(tmp_path, monkeypatch):
    import wave
    from pathlib import Path
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, self.headers.get('X-Hermes-Sidecar-Token'), body, Path(body['path']).read_bytes()))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        home = tmp_path / 'home'
        cache = home / 'image_cache'
        cache.mkdir(parents=True)
        (home / 'runtime').mkdir()
        (home / 'runtime/photon-sidecar.json').write_text(json.dumps({'port': server.server_port, 'token':'fixture-token'}))
        monkeypatch.setenv('HERMES_HOME', str(home))
        image = cache / 'test.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        audio = cache / 'test.wav'
        with wave.open(str(audio), 'wb') as out:
            out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            out.writeframes(b'\0\0' * 8000)
        from butler.expressions import _sidecar
        run = setup(_sidecar)
        assert run(action='send_image', path=str(image))['ok']
        assert run(action='voice_note', audio_path=str(audio))['ok']
        assert [x[2]['kind'] for x in calls] == ['attachment', 'voice']
        assert all(x[0] == '/send-attachment' and x[1] == 'fixture-token' and x[2]['spaceId'] == 'chat-A' for x in calls)
        assert calls[0][3] == image.read_bytes() and b'ftyp' in calls[1][3][:16]
        assert all(not Path(x[2]['path']).exists() for x in calls)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
