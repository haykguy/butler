"""Optional Pillow-backed, local image editing for direct tool integration.

Outputs are new PNG/JPEG files directly inside $HERMES_HOME/image_cache/edited;
existing files are never overwritten. Input and overlay paths are local files.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import uuid


def hermes_home():
    """Active profile's Hermes home (public hermes_constants helper; env fallback)."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        import os
        return Path(os.environ.get('HERMES_HOME', '~/.hermes')).expanduser()
    return Path(get_hermes_home()).expanduser()


def _box(op, image):
    box = op.get('box', (0, 0, image.width, image.height))
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError('box must have four coordinates')
    left, top, right, bottom = map(int, box)
    box = (max(0, left), max(0, top), min(image.width, right), min(image.height, bottom))
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError('box must have positive area within the image')
    return box


def _apply(image, op, pil):
    Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps = pil
    if not isinstance(op, dict) or not isinstance(op.get('op'), str):
        raise ValueError('each operation must be an object with an op')
    kind = op['op'].lower()
    if kind == 'crop':
        return image.crop(_box(op, image))
    if kind == 'resize':
        w, h = op.get('width'), op.get('height')
        if w is None and h is None:
            raise ValueError('resize requires width or height')
        w = int(w) if w is not None else max(1, round(image.width * int(h) / image.height))
        h = int(h) if h is not None else max(1, round(image.height * w / image.width))
        if w < 1 or h < 1 or w * h > 40_000_000:
            raise ValueError('invalid resize dimensions')
        return image.resize((w, h), Image.Resampling.LANCZOS)
    if kind == 'rotate':
        return image.rotate(-float(op.get('degrees', 90)), expand=True)
    if kind == 'flip':
        direction = op.get('direction', 'horizontal')
        if direction not in ('horizontal', 'vertical'):
            raise ValueError('flip direction must be horizontal or vertical')
        return ImageOps.mirror(image) if direction == 'horizontal' else ImageOps.flip(image)
    if kind == 'grayscale':
        return ImageOps.grayscale(image).convert('RGB')
    if kind in ('brightness', 'contrast'):
        enhancer = ImageEnhance.Brightness if kind == 'brightness' else ImageEnhance.Contrast
        return enhancer(image).enhance(float(op.get('factor', 1.2)))
    if kind in ('blur', 'pixelate'):
        box = _box(op, image)
        region = image.crop(box)
        if kind == 'blur':
            region = region.filter(ImageFilter.GaussianBlur(float(op.get('radius', 12))))
        else:
            size = max(2, int(op.get('size', 12)))
            region = region.resize((max(1, region.width // size), max(1, region.height // size)), Image.Resampling.NEAREST).resize(region.size, Image.Resampling.NEAREST)
        image = image.copy()
        image.paste(region, box[:2])
        return image
    if kind == 'overlay':
        with Image.open(os.path.expanduser(op['path'])) as source:
            overlay = ImageOps.exif_transpose(source).convert('RGBA')
        if op.get('width'):
            w = int(op['width'])
            h = max(1, round(overlay.height * w / overlay.width))
            if w < 1 or w * h > 40_000_000:
                raise ValueError('invalid overlay dimensions')
            overlay = overlay.resize((w, h), Image.Resampling.LANCZOS)
        image = image.convert('RGBA')
        image.alpha_composite(overlay, tuple(map(int, op.get('xy', (0, 0)))))
        return image.convert('RGB')
    draw = ImageDraw.Draw(image)
    color = op.get('color', 'red')
    width = int(op.get('width', max(3, image.width // 200)))
    if width < 1:
        raise ValueError('width must be positive')
    if kind == 'rectangle':
        draw.rectangle(_box(op, image), outline=color, width=width, fill=op.get('fill'))
    elif kind == 'ellipse':
        draw.ellipse(_box(op, image), outline=color, width=width)
    elif kind == 'arrow':
        (x1, y1), (x2, y2) = op['from'], op['to']
        draw.line((x1, y1, x2, y2), fill=color, width=width)
        angle = math.atan2(y2 - y1, x2 - x1)
        head = max(12, width * 4)
        draw.polygon([(x2, y2), (x2 - head * math.cos(angle - .45), y2 - head * math.sin(angle - .45)), (x2 - head * math.cos(angle + .45), y2 - head * math.sin(angle + .45))], fill=color)
    elif kind == 'text':
        size = int(op.get('size', max(18, image.width // 25)))
        if not 1 <= size <= 4096:
            raise ValueError('invalid font size')
        font = None
        for candidate in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf'):
            if os.path.isfile(candidate):
                font = ImageFont.truetype(candidate, size)
                break
        if font is None:
            font = ImageFont.load_default()
        xy = tuple(map(int, op.get('xy', (10, 10))))
        text = str(op.get('text', ''))
        if op.get('background'):
            l, t, r, b = draw.textbbox(xy, text, font=font)
            draw.rectangle((l - 6, t - 4, r + 6, b + 4), fill=op['background'])
        draw.text(xy, text, fill=op.get('color', 'white'), font=font)
    else:
        raise ValueError(f'unknown op {kind!r}')
    return image


def edit_image(args):
    """Return {'ok': True, 'output_path': ...} or {'ok': False, 'error': ...}."""
    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
    except ImportError:
        return {'ok': False, 'error': 'Pillow is required for image editing'}
    try:
        if not isinstance(args, dict):
            raise ValueError('arguments must be an object')
        path = args.get('input_path')
        operations = args.get('operations')
        if not isinstance(path, str) or not path or not isinstance(operations, list):
            raise ValueError('input_path and operations list are required')
        cache = (hermes_home() / 'image_cache' / 'edited')
        output = args.get('output_path')
        if output is None:
            name = f'{Path(path).stem}-edited-{uuid.uuid4().hex}.png'
        else:
            if not isinstance(output, str) or not output:
                raise ValueError('output_path must be a nonempty path')
            requested = Path(output).expanduser().absolute()
            if requested.parent.resolve() != cache.resolve():
                raise ValueError('output_path must be directly inside the edited image cache')
            name = requested.name
        extension = Path(name).suffix.lower()
        if extension not in ('.png', '.jpg', '.jpeg'):
            raise ValueError('output must be PNG or JPEG')
        with Image.open(os.path.expanduser(path)) as source:
            image = ImageOps.exif_transpose(source).convert('RGB')
        for op in operations:
            image = _apply(image, op, (Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps))
        cache.mkdir(parents=True, exist_ok=True)
        # A directory fd and exclusive creation prevent symlink/hardlink overwrite.
        directory = os.open(cache, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        created = False
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=directory)
            created = True
            with os.fdopen(fd, 'wb') as stream:
                image.save(stream, format='PNG' if extension == '.png' else 'JPEG')
        except Exception:
            if created:
                os.unlink(name, dir_fd=directory)
            raise
        finally:
            os.close(directory)
        return {'ok': True, 'output_path': str(cache.resolve() / name)}
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:300]}
