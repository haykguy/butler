"""Profile-local prompt onboarding. Preview is read-only; apply is opt-in.

The caller supplies the already-resolved profile home. Project AGENTS.md is
intentionally not supported: this API has no trusted project context parameter.
Missing files have a distinct SHA-256 token so creation conflicts are detected.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import uuid
from pathlib import Path

_SPECS = {
    'soul': ('SOUL.md', 'Practical operating principles', '\n## Working principles\n\n- Own outcomes, not just plans.\n- Verify tool results and prove the outcome before claiming success.\n- Be concise and candid about uncertainty or blockers.\n'),
    'capabilities': ('skills/butler-capabilities/SKILL.md', 'Discover available capabilities before acting', '---\nname: butler-capabilities\ndescription: "Use when discovering available tools, services, or skills."\n---\n\n# Capability discovery\n\n## Environment and user surfaces\nDiscover the actual environment and how results reach the user; do not assume devices, access, or delivery channels.\n\n## Capabilities indexed to skills\nDiscover available tools and relevant skills before acting; load applicable skills and do not invent capabilities.\n\n## Delegation and services\nDiscover available delegation options and services before relying on them. Check availability and authorization.\n\n## Verify outcomes and maintain an accurate inventory\nVerify the deliverable, not merely tool success. Distinguish discovered facts from unknowns and stale information; never present assumptions as facts. Keep any authorized inventory accurate as the environment changes.\n'),
}
_MISSING = hashlib.sha256(b'butler prompt review: absent file\0').hexdigest()
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _directory(home, relative, create=False):
    """Open each directory without following links, anchoring subsequent IO."""
    try:
        fd = os.open(os.fspath(home), _DIR)
    except OSError as exc:
        raise ValueError('unsafe or missing profile home') from exc
    try:
        for part in Path(relative).parts[:-1]:
            try:
                child = os.open(part, _DIR, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        os.close(fd)
        raise ValueError('unsafe profile path') from exc


def _read(fd, name):
    if fd is None:
        return None
    try:
        f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError('unsafe prompt file') from exc
    try:
        info = os.fstat(f)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe prompt file')
        with os.fdopen(f, 'rb') as stream:
            f = -1
            return stream.read()
    finally:
        if f != -1:
            os.close(f)


def _token(data):
    return _MISSING if data is None else hashlib.sha256(data).hexdigest()


def _preview_token(home, item_id, data):
    """Bind the file snapshot to its resolved profile and logical target."""
    raw = os.fsencode(str(home)) + b'\0' + item_id.encode('utf-8') + b'\0' + _token(data).encode('ascii')
    return hashlib.sha256(raw).hexdigest()


def _proposal(data, addition):
    current = (data or b'').decode('utf-8')
    if current and addition.startswith('---\n'):
        addition = '\n' + addition.split('---\n', 2)[2].lstrip('\n')
    return current, current if addition.strip() in current else current + addition


def _inventory(home):
    """Read only bounded, conservative frontmatter from local skill files."""
    root = Path(home) / 'skills'
    lines = []
    visited = 0
    # Never traverse linked directories; open each candidate again with no-follow.
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(directory) / d).is_symlink())
        depth = len(Path(directory).relative_to(root).parts)
        if depth >= 3:
            dirs[:] = []
        visited += 1
        if visited > 500 or len(lines) >= 50:
            break
        if 'SKILL.md' not in files:
            continue
        relative = str((Path(directory) / 'SKILL.md').relative_to(home))
        fd = None
        file_fd = None
        try:
            fd = _directory(home, relative)
            file_fd = os.open('SKILL.md', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 128 * 1024:
                continue
            text = os.read(file_fd, 8192).decode('utf-8')
            parts = text.split('---\n', 2)
            if len(parts) < 3 or parts[0] != '':
                continue
            fields = {}
            for line in parts[1].splitlines():
                match = re.fullmatch(r'(name|description):\s*(.*?)\s*', line)
                if match:
                    fields[match.group(1)] = match.group(2).strip('"\'')
            name, desc = fields.get('name', ''), fields.get('description', '')
            # Exclude addresses, tokens, paths, digits and suspicious credential language.
            if not re.fullmatch(r'[a-z][a-z-]{0,59}', name):
                continue
            if not re.fullmatch(r'[A-Za-z ,.;:()/-]{3,140}', desc):
                continue
            if re.search(r'(?i)\b(secret|token|password|credential|api[- ]?key|private key|bearer)\b', desc):
                continue
            lines.append(f'- {name}: {desc}')
        except (OSError, ValueError, UnicodeError):
            continue
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if fd is not None:
                os.close(fd)
    heading = '\n## Installed local skills (metadata snapshot)\n\n'
    note = 'Installed metadata is not proof of availability or authorization; verify before use. This snapshot may be incomplete or stale.\n'
    return heading + note + ('\n' + '\n'.join(lines) + '\n' if lines else '\nNo safe skill metadata discovered.\n')


def review(home):
    home = Path(home).resolve(strict=True)
    items = []
    for item_id, (relative, description, addition) in _SPECS.items():
        fd = _directory(home, relative)
        try:
            data = _read(fd, Path(relative).name)
        finally:
            if fd is not None:
                os.close(fd)
        if item_id == 'capabilities':
            # An installed template is already onboarded; don't append a new
            # copy merely because the discovered inventory has changed.
            body = addition.split('---\n', 2)[2].strip()
            if data and body in data.decode('utf-8'):
                continue
            addition += _inventory(home)
        current, proposed = _proposal(data, addition)
        if proposed == current:
            continue
        items.append({'id': item_id, 'path': str(Path(home) / relative), 'current': current,
                      'proposed': proposed, 'diff': ''.join(difflib.unified_diff(current.splitlines(keepends=True), proposed.splitlines(keepends=True), fromfile=relative, tofile=relative)),
                      'sha256': _preview_token(home, item_id, data), 'description': description})
    return {'items': items}


def _write_new(fd, name, data, mode=0o600):
    f = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
    try:
        with os.fdopen(f, 'wb') as stream:
            f = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if f != -1:
            os.close(f)
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        raise


def apply(home, items):
    """Apply selected, possibly edited proposals; raise ValueError on conflict.

    Existing files receive unique sibling backups. Individual writes are atomic;
    on a later failure, earlier writes are restored when possible.
    """
    home = Path(home).resolve(strict=True)
    if not isinstance(items, list):
        raise ValueError('items must be a list')
    seen = set()
    prepared = []
    # Validate the entire selection before creating directories or backups.
    for item in items:
        if not isinstance(item, dict) or set(item) != {'id', 'sha256', 'proposed'}:
            raise ValueError('invalid item')
        item_id = item['id']
        if not isinstance(item_id, str) or item_id not in _SPECS or item_id in seen:
            raise ValueError('invalid or duplicate item id')
        seen.add(item_id)
        relative, _, addition = _SPECS[item_id]
        fd = _directory(home, relative)
        try:
            data = _read(fd, Path(relative).name)
        finally:
            if fd is not None:
                os.close(fd)
        if item['sha256'] != _preview_token(home, item_id, data):
            raise ValueError('prompt conflict: file changed since preview')
        proposed = item['proposed']
        if not isinstance(proposed, str) or '\x00' in proposed:
            raise ValueError('invalid proposal')
        try:
            encoded = proposed.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise ValueError('invalid proposal') from exc
        if len(encoded) > 256 * 1024 or not proposed.strip() or encoded == data or (data is not None and not encoded.startswith(data)):
            raise ValueError('invalid proposal')
        prepared.append((item_id, relative, data, encoded))
    committed = []
    try:
        for item_id, relative, data, proposed in prepared:
            fd = _directory(home, relative, create=True)
            name = Path(relative).name
            temp = '.' + name + '.' + uuid.uuid4().hex + '.tmp'
            try:
                if _token(_read(fd, name)) != _token(data):
                    raise ValueError('prompt conflict: file changed during apply')
                if data is not None:
                    _write_new(fd, name + '.bak.' + uuid.uuid4().hex, data)
                _write_new(fd, temp, proposed)
                if _token(_read(fd, name)) != _token(data):
                    raise ValueError('prompt conflict: file changed during apply')
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
                committed.append((fd, name, data, proposed))
                fd = None
            finally:
                if fd is not None:
                    try:
                        os.unlink(temp, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                    os.close(fd)
    except BaseException:
        for fd, name, data, proposed in reversed(committed):
            # Do not overwrite a concurrent writer during rollback.
            if _read(fd, name) != proposed:
                continue
            if data is None:
                os.unlink(name, dir_fd=fd)
            else:
                temp = '.' + name + '.' + uuid.uuid4().hex + '.rollback'
                _write_new(fd, temp, data)
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        raise
    finally:
        for fd, _, _, _ in committed:
            os.close(fd)
    return {'applied': [item_id for item_id, _, _, _ in prepared]}
