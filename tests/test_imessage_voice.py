import wave
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('imessage_voice_tested', Path(__file__).resolve().parents[1] / 'butler/imessage_voice.py')
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)
prepare_voice = voice.prepare_voice


def test_converts_local_audio_to_m4a(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    source = tmp_path / 'sample.wav'
    with wave.open(str(source), 'wb') as wav:
        wav.setparams ( (1, 2, 8000, 8000, 'NONE', 'not compressed') )
        wav.writeframes(b'\0\0' * 8000)
    result = prepare_voice({'audio_path': str(source)})
    assert result['ok'], result
    assert Path(result['path']).suffix == '.m4a'
    assert Path(result['path']).is_file()
    assert 0.9 <= result['duration'] <= 1.2
    assert source.exists()


def test_text_uses_hermes_tts_and_converts(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    source = tmp_path / 'synth.wav'
    with wave.open(str(source), 'wb') as wav:
        wav.setparams((1, 2, 8000, 8000, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 8000)
    calls = []
    def synth(name, args):
        calls.append((name, args['text']))
        output = Path(args['output_path']).with_suffix('.wav')
        output.write_bytes(source.read_bytes())
        return json.dumps({'success': True, 'file_path': str(output), 'file_paths': [str(output)]})
    result = prepare_voice({'text': 'Hello'}, dispatch_tool=synth)
    assert result['ok'], result
    assert calls == [('text_to_speech', 'Hello')]
    assert Path(result['path']).is_file()


@pytest.mark.parametrize('kind', ['outside', 'symlink', 'multiple', 'failure', 'invalid_json'])
def test_tts_rejects_untrusted_results_without_sending(tmp_path, monkeypatch, kind):
    import json
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    private = tmp_path / 'private.wav'
    private.write_bytes(b'private')
    def dispatch(name, args):
        path = Path(args['output_path']).with_suffix('.wav')
        if kind == 'outside':
            path = private
        elif kind == 'symlink':
            path.symlink_to(private)
        elif kind == 'failure':
            return json.dumps({'success': False})
        elif kind == 'invalid_json':
            return 'not json'
        paths = [str(path), str(private)] if kind == 'multiple' else [str(path)]
        return json.dumps({'success': True, 'file_path': str(path), 'file_paths': paths})
    result = prepare_voice({'text': 'hello'}, dispatch_tool=dispatch)
    assert not result['ok']
    assert private.read_bytes() == b'private'
    assert not list((tmp_path / 'home' / 'voice_cache').glob('tts-*'))


def test_conversion_has_hard_resource_limits(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    source = tmp_path / 'audio.wav'
    with wave.open(str(source), 'wb') as wav:
        wav.setparams((1, 2, 8000, 8000, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 8000)
    real_run = voice.subprocess.run
    commands = []
    def run(cmd, **kwargs):
        commands.append(cmd)
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(voice.subprocess, 'run', run)
    assert prepare_voice({'audio_path': str(source)})['ok']
    conversion = next(cmd for cmd in commands if cmd[0] == 'ffmpeg')
    assert '-t' in conversion and '-fs' in conversion


def test_missing_ffprobe_is_graceful(tmp_path, monkeypatch):
    source = tmp_path / 'audio.wav'
    source.write_bytes(b'something')
    monkeypatch.setenv('PATH', '')
    result = prepare_voice({'audio_path': str(source)})
    assert not result['ok']
    assert 'ffprobe' in result['error']


def test_rejects_non_audio(tmp_path):
    source = tmp_path / 'not-audio'
    source.write_bytes(b'this is not audio')
    assert not prepare_voice({'audio_path': str(source)})['ok']


def test_invalid_input_does_not_call_tts(monkeypatch):
    import builtins
    original = builtins.__import__
    def check(name, *args, **kwargs):
        assert name != 'tools.tts_tool'
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', check)
    assert not prepare_voice({'text': ' '})['ok']
    assert not prepare_voice({'text': 'x' * 5001})['ok']
