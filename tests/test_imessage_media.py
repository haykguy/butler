import pytest
from butler.imessage_media import snapshot


def test_authorized_media_is_snapshotted_and_outside_rejected(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    cache = home / 'image_cache'
    cache.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    source = cache / 'photo.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
    with snapshot(str(source), 'image') as staged:
        assert staged.read_bytes() == source.read_bytes()
        assert staged != source
    assert not staged.exists()
    outside = tmp_path / 'secret.png'
    outside.write_bytes(source.read_bytes())
    with pytest.raises(ValueError):
        with snapshot(str(outside), 'image'): pass


def test_reject_symlinks_hardlinks_and_sensitive_extensions(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    root = home / 'image_cache'
    root.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    secret = tmp_path / 'secret.png'
    secret.write_bytes(b'\x89PNG\r\n\x1a\nsecret')
    (root / 'link.png').symlink_to(secret)
    import os
    os.link(secret, root / 'hard.png')
    (root / 'dir').symlink_to(tmp_path, target_is_directory=True)
    (root / 'secret.pem').write_bytes(secret.read_bytes())
    for p in (root / 'link.png', root / 'hard.png', root / 'dir' / 'secret.png', root / 'secret.pem'):
        with pytest.raises(ValueError):
            with snapshot(str(p), 'image'): pass


def test_plugin_media_rejects_symlink_in_root_ancestors(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'outside'
    media = outside / 'butler' / 'media'
    media.mkdir(parents=True)
    (home / 'plugin-data').symlink_to(outside, target_is_directory=True)
    source = media / 'secret.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nsecret')
    monkeypatch.setenv('HERMES_HOME', str(home))
    with pytest.raises(ValueError):
        with snapshot(str(home / 'plugin-data/butler/media/secret.png'), 'image'): pass


def test_reject_disguised_audio_playlist(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    cache = home / 'image_cache'
    cache.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    path = cache / 'audio.m4a'
    path.write_bytes(b"#EXTM3U\nfile:///private/secret\n")
    with pytest.raises(ValueError):
        with snapshot(str(path), 'audio'): pass


def test_plugin_media_directory_is_allowed(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    media = home / 'plugin-data/butler/media'
    media.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    image = media / 'shared.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\nshared')
    with snapshot(str(image), 'image') as staged:
        assert staged.read_bytes() == image.read_bytes()
