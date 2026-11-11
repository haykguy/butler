"""Filesystem boundary for explicitly shared media.

Only image_cache and plugin-data/butler/media under the active Hermes home
are eligible. These directories are a sharing boundary, not proof of consent:
anyone able to put a file there can make it available to the agent. Reject
symlinks and hardlinks; read via directory descriptors and copy to a private
snapshot before consumers reopen a path. This does not defend against a
malicious process running as the same OS user or modifying an open file.
"""
import os
from pathlib import Path
import stat
import tempfile
from contextlib import contextmanager
from .paths import hermes_home

_MAX = 50 * 1024 * 1024
_TYPES = {'image': {'.png', '.jpg', '.jpeg', '.gif', '.webp'},
          'audio': {'.m4a', '.mp3', '.wav', '.aac', '.ogg', '.flac'}}


def roots():
    home = hermes_home().absolute()
    return (home / 'image_cache', home / 'plugin-data' / 'butler' / 'media')


@contextmanager
def snapshot(raw, kind):
    if not isinstance(raw, str) or not raw or kind not in _TYPES:
        raise ValueError('a local media path is required')
    path = Path(raw).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('media path must be absolute without traversal')
    if path.suffix.lower() not in _TYPES[kind] or path.name.startswith('.'):
        raise ValueError('unsupported or sensitive media file type')
    root = next((r for r in roots() if path.is_relative_to(r) and path != r), None)
    if root is None:
        raise ValueError('media must be inside Hermes image_cache or plugin-data/butler/media')
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
    directory = None
    fd = None
    try:
        home = roots()[0].parent
        directory = os.open(home, flags | os.O_DIRECTORY)
        parts = root.relative_to(home).parts + path.relative_to(root).parts
        for part in parts[:-1]:
            new = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = new
        fd = os.open(parts[-1], flags, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= _MAX:
            raise ValueError('media must be a nonempty, unlinked regular file of at most 50 MiB')
        with tempfile.TemporaryDirectory(prefix='butler-media-') as tmp:
            staged = Path(tmp) / ('media' + path.suffix.lower())
            with os.fdopen(fd, 'rb') as src, staged.open('xb') as dst:
                fd = None
                data = src.read(_MAX + 1)
                if not 0 < len(data) <= _MAX:
                    raise ValueError('media exceeds 50 MiB or is empty')
                if kind == 'image' and not (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff') or data.startswith((b'GIF87a', b'GIF89a')) or (data.startswith(b'RIFF') and data[8:12] == b'WEBP')):
                    raise ValueError('file is not a supported image')
                if kind == 'audio':
                    suffix = path.suffix.lower()
                    valid = ((suffix == '.wav' and data.startswith(b'RIFF') and data[8:12] == b'WAVE') or
                             (suffix == '.m4a' and data[4:8] == b'ftyp') or
                             (suffix == '.flac' and data.startswith(b'fLaC')) or
                             (suffix == '.ogg' and data.startswith(b'OggS')) or
                             (suffix == '.mp3' and (data.startswith(b'ID3') or (len(data) > 1 and data[0] == 255 and data[1] & 224 == 224))) or
                             (suffix == '.aac' and len(data) > 1 and data[0] == 255 and data[1] & 246 == 240))
                    if not valid:
                        raise ValueError('audio content does not match a supported format')
                dst.write(data)
            yield staged
    except OSError as exc:
        raise ValueError('media unavailable or contains a symlink') from exc
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)
