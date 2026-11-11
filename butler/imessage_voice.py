"""Prepare a local AAC voice note; this module never sends messages.

Requires ffmpeg and ffprobe on PATH. Text requires the public Hermes
``ctx.dispatch_tool`` API and a configured TTS provider.
Successful output is a private, newly created file under HERMES_HOME/voice_cache;
the caller owns its eventual deletion. Input files are never modified.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile


def hermes_home():
    """Active profile's Hermes home (public hermes_constants helper; env fallback)."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        import os
        return Path(os.environ.get('HERMES_HOME', '~/.hermes')).expanduser()
    return Path(get_hermes_home()).expanduser()


_MAX_BYTES = 50 * 1024 * 1024
_MAX_DURATION = 300


def _probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                             '-show_entries', 'format=duration', '-of', 'json', str(path)],
                            capture_output=True, timeout=30, check=True)
    duration = float(json.loads(result.stdout)['format']['duration'])
    if not 0 < duration <= _MAX_DURATION:
        raise ValueError('audio duration must be between 0 and 300 seconds')
    return duration


def prepare_voice(args, *, dispatch_tool=None):
    """Return {ok, path, duration, mime_type, name} or {ok: False, error}."""
    try:
        if not isinstance(args, dict):
            raise ValueError('arguments must be an object')
        source = args.get('audio_path')
        if not source:
            text = args.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 5000:
                raise ValueError('provide audio_path or nonempty text of at most 5000 characters')
            if not callable(dispatch_tool):
                raise ValueError('Hermes ctx.dispatch_tool is unavailable; text-to-speech cannot run')
            cache = hermes_home() / 'voice_cache'
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix='tts-', dir=cache) as tmp:
                output = Path(tmp).resolve() / 'speech.mp3'
                try:
                    response = json.loads(dispatch_tool('text_to_speech', {'text': text, 'output_path': str(output)}))
                except Exception:
                    raise ValueError('Hermes text_to_speech dispatch failed or returned invalid JSON') from None
                if not isinstance(response, dict) or response.get('success') is not True:
                    raise ValueError('Hermes text_to_speech failed or is unavailable; check provider configuration')
                source = response.get('file_path')
                paths = response.get('file_paths')
                if not isinstance(source, str) or paths != [source]:
                    raise ValueError('Hermes TTS returned multiple or invalid audio files')
                path = Path(source)
                if not path.is_absolute() or path.parent != output.parent or path.stem != output.stem or path.is_symlink():
                    raise ValueError('Hermes TTS returned a path outside its private output')
                info = path.stat()
                if info.st_nlink != 1 or not path.is_file():
                    raise ValueError('Hermes TTS output must be a regular unlinked file')
                return prepare_voice({'audio_path': source})
        if not isinstance(source, str) or not source:
            raise ValueError('audio_path must be a local path')
        path = Path(source).expanduser().resolve(strict=True)
        if not path.is_file() or not 0 < path.stat().st_size <= _MAX_BYTES:
            raise ValueError('audio must be a nonempty local file of at most 50 MiB')
        _probe(path)
        cache = hermes_home() / 'voice_cache'
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = Path(tempfile.mkdtemp(prefix='voice-', dir=cache))
        output = directory / 'Audio Message.m4a'
        try:
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                            '-i', str(path), '-vn', '-ac', '1', '-ar', '44100',
                            '-c:a', 'aac', '-b:a', '128k', '-t', str(_MAX_DURATION + 1),
                            '-fs', str(_MAX_BYTES), '-n', str(output)],
                           capture_output=True, timeout=120, check=True)
            if not output.is_file() or not 0 < output.stat().st_size <= _MAX_BYTES:
                raise ValueError('converted audio is empty or too large')
            duration = _probe(output)
        except Exception:
            output.unlink(missing_ok=True)
            directory.rmdir()
            raise
        return {'ok': True, 'path': str(output.resolve()), 'duration': duration,
                'mime_type': 'audio/mp4', 'name': 'Audio Message.m4a'}
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:300]}
