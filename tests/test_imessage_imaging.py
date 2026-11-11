import builtins
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('imessage_imaging_tested', Path(__file__).resolve().parents[1] / 'butler/imessage_imaging.py')
imaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imaging)


def test_missing_pillow_returns_error(monkeypatch, tmp_path):
    edit_image = imaging.edit_image
    source = tmp_path / 'source.png'
    source.write_bytes(b'not an image')
    real_import = builtins.__import__
    def without_pillow(name, *args, **kwargs):
        if name == 'PIL' or name.startswith('PIL.'):
            raise ImportError('no PIL')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_pillow)
    result = edit_image({'input_path': str(source), 'operations': []})
    assert result['ok'] is False
    assert 'Pillow' in result['error']


def test_resize_and_grayscale(tmp_path, monkeypatch):
    import pytest
    Image = pytest.importorskip('PIL.Image')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    src = tmp_path / 'src.png'
    Image.new('RGB', (10, 6), 'red').save(src)
    result = imaging.edit_image({'input_path': str(src), 'operations': [
        {'op': 'resize', 'width': 5}, {'op': 'grayscale'}]})
    assert result['ok'], result
    with Image.open(result['output_path']) as out:
        assert out.size == (5, 3)
        pixel = out.convert('RGB').getpixel((0, 0))
        assert pixel[0] == pixel[1] == pixel[2]
    assert Image.open(src).size == (10, 6)


def test_paths_cannot_overwrite_or_escape_cache(tmp_path, monkeypatch):
    import pytest
    Image = pytest.importorskip('PIL.Image')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    src = tmp_path / 'src.png'
    Image.new('RGB', (20, 20), 'blue').save(src)
    before = src.read_bytes()
    cache = tmp_path / 'home/image_cache/edited'
    cache.mkdir(parents=True)
    (cache / 'escape').symlink_to(tmp_path, target_is_directory=True)
    for out in (src, cache / '..' / '..' / '..' / 'src.png', cache / 'escape' / 'src.png'):
        result = imaging.edit_image({'input_path': str(src), 'operations': [], 'output_path': str(out)})
        assert not result['ok']
        assert src.read_bytes() == before
    existing = cache / 'existing.png'
    existing.write_bytes(b'keep')
    assert not imaging.edit_image({'input_path': str(src), 'operations': [], 'output_path': str(existing)})['ok']
    assert existing.read_bytes() == b'keep'
    (cache / 'link.png').symlink_to(src)
    assert not imaging.edit_image({'input_path': str(src), 'operations': [], 'output_path': str(cache / 'link.png')})['ok']
    assert src.read_bytes() == before
    result = imaging.edit_image({'input_path': str(src), 'operations': [], 'output_path': str(cache / 'new.jpg')})
    assert result['ok'], result
    assert Image.open(result['output_path']).format == 'JPEG'


def test_all_operations_and_invalid_operations(tmp_path, monkeypatch):
    import pytest
    Image = pytest.importorskip('PIL.Image')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    src = tmp_path / 'src.png'
    Image.new('RGB', (50, 50), 'blue').save(src)
    ops = [{'op': 'crop', 'box': [0, 0, 40, 40]}, {'op': 'rotate', 'degrees': 90},
           {'op': 'flip', 'direction': 'vertical'}, {'op': 'brightness', 'factor': 1.1},
           {'op': 'contrast', 'factor': 1.1}, {'op': 'blur', 'radius': 1},
           {'op': 'pixelate', 'box': [0, 0, 10, 10]}, {'op': 'rectangle', 'box': [1, 1, 20, 20]},
           {'op': 'ellipse', 'box': [1, 1, 20, 20]}, {'op': 'arrow', 'from': [1, 1], 'to': [30, 30]},
           {'op': 'text', 'text': 'hi'}, {'op': 'overlay', 'path': str(src), 'width': 10}]
    result = imaging.edit_image({'input_path': str(src), 'operations': ops})
    assert result['ok'], result
    assert Image.open(result['output_path']).size == (40, 40)
    for bad in ([{'op': 'bogus'}], [{'op': 'crop', 'box': [4, 4, 1, 1]}], 'resize', [None]):
        assert not imaging.edit_image({'input_path': str(src), 'operations': bad})['ok']
