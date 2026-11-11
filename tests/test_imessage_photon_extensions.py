from pathlib import Path

from butler.imessage_photon_extensions import invoke_extension


def test_contact_card_uses_only_active_chat():
    calls = []
    result = invoke_extension('contact_card', {'confirm': True}, session=('photon', 'chat-A'),
                              sidecar=lambda path, body: calls.append((path, body)) or {'ok': True})
    assert result == {'ok': True}
    assert calls == [('/x/contact-card', {'spaceId': 'chat-A'})]


def test_background_snapshot_and_clear(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    root = tmp_path / 'image_cache'
    root.mkdir()
    image = root / 'photo.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\nexample')
    calls = []
    def sidecar(route, body):
        calls.append((route, body))
        if 'path' in body:
            assert Path(body['path']).read_bytes() == image.read_bytes()
            assert body['path'] != str(image)
        return {'ok': True}
    assert invoke_extension('background', {'confirm': True, 'path': str(image)}, session=('photon', 'chat-A'), sidecar=sidecar)['ok']
    assert not Path(calls[0][1]['path']).exists()
    assert invoke_extension('background', {'confirm': True, 'clear': True}, session=('photon', 'chat-A'), sidecar=sidecar)['ok']
    assert calls[1] == ('/x/background', {'spaceId': 'chat-A', 'clear': True})


def test_invalid_requests_never_call_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    calls = []
    def sidecar(*args):
        calls.append(args)
        return {'ok': True}
    for action, args, session in [
        ('contact_card', {}, ('photon', 'chat')),
        ('contact_card', {'confirm': 1}, ('photon', 'chat')),
        ('contact_card', {'confirm': True, 'chat_id': 'other'}, ('photon', 'chat')),
        ('contact_card', {'confirm': True, 'unexpected': 1}, ('photon', 'chat')),
        ('contact_card', {'confirm': True}, ('telegram', 'chat')),
        ('background', {'confirm': True, 'clear': True, 'path': 'x'}, ('photon', 'chat')),
        ('background', {'confirm': True, 'clear': 'yes'}, ('photon', 'chat')),
        ('background', {'confirm': True, 'path': '/etc/passwd.jpg'}, ('photon', 'chat')),
    ]:
        assert not invoke_extension(action, args, session=session, sidecar=sidecar)['ok']
    assert calls == []


def test_errors_are_sanitized_and_not_retried():
    calls = []
    def broken(*args):
        calls.append(args)
        raise OSError('secret')
    result = invoke_extension('contact_card', {'confirm': True}, session=('photon', 'chat'), sidecar=broken)
    assert 'outcome may be unknown' in result['error']
    assert 'secret' not in str(result)
    assert len(calls) == 1
    result = invoke_extension('contact_card', {'confirm': True}, session=('photon', 'chat'), sidecar=lambda *_: {'ok': 'true'})
    assert not result['ok']
