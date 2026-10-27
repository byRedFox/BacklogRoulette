"""Рисует значок Backlog Roulette: игральный кубик с буквами BR в центре на фиолетово-синем фоне.

Запуск: python make_icon.py — появятся icon.ico (для программы и .exe) и icon.png (для README).
Нужен только Pillow, он уже есть в requirements.txt.
"""

import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024  # рисуем крупно и уменьшаем — так края получаются гладкими

TOP, BOTTOM = (124, 77, 255), (0, 150, 255)  # градиент фона
FACE, SIDE = (255, 255, 255), (196, 204, 255)  # грань кубика и её нижний «бортик»
INK = (76, 52, 214)  # точки и буквы
ANGLE = 12  # наклон кубика, градусы
TEXT = "BR"
FONTS = ("seguibl.ttf", "segoeuib.ttf", "arialbd.ttf")  # Segoe UI Black, Segoe UI Bold, Arial Bold

BICUBIC = getattr(Image, "Resampling", Image).BICUBIC
LANCZOS = getattr(Image, "Resampling", Image).LANCZOS


def background():
    """Скруглённый квадрат в стиле значков Windows 11 с вертикальным градиентом."""
    gradient = Image.new("RGBA", (S, S))
    draw = ImageDraw.Draw(gradient)
    for y in range(S):
        t = y / (S - 1)
        draw.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))
    # Мягкий блик сверху
    shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shine).ellipse([-0.2 * S, -0.75 * S, 1.2 * S, 0.45 * S], fill=(255, 255, 255, 38))
    gradient = Image.alpha_composite(gradient, shine.filter(ImageFilter.GaussianBlur(S * 0.04)))

    mask = Image.new("L", (S, S), 0)
    m = S * 0.06
    ImageDraw.Draw(mask).rounded_rectangle([m, m, S - m, S - m], radius=S * 0.22, fill=255)
    result = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    result.paste(gradient, (0, 0), mask)
    return result


def load_font(size):
    """Самый жирный шрифт Windows из доступных; без них — встроенный шрифт Pillow."""
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def monogram(draw, cx, cy, f):
    """Буквы BR вместо центральной точки: подбираем размер, чтобы надпись не задевала угловые точки."""
    max_w, max_h = 0.40 * f, 0.22 * f
    size = round(0.30 * f)
    while True:
        font = load_font(size)
        left, top, right, bottom = draw.textbbox((0, 0), TEXT, font=font)
        if (right - left <= max_w and bottom - top <= max_h) or size <= 8:
            break
        size -= 4
    # Ставим по центру рамки самих букв, а не строки — иначе надпись съезжает вниз
    draw.text((cx - (left + right) / 2, cy - (top + bottom) / 2), TEXT, font=font, fill=INK)


def die():
    """Грань кубика: четыре точки по углам и буквы BR в центре — как «пятёрка»."""
    f = S * 0.56
    depth = f * 0.06  # нижний бортик даёт ощущение объёма
    layer = Image.new("RGBA", (round(f), round(f + depth)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    radius = f * 0.2
    draw.rounded_rectangle([0, depth, f - 1, f + depth - 1], radius=radius, fill=SIDE)
    draw.rounded_rectangle([0, 0, f - 1, f - 1], radius=radius, fill=FACE)

    c = f / 2
    pip = 0.085 * f
    for sx in (-1, 1):
        for sy in (-1, 1):
            x, y = c + sx * 0.27 * f, c + sy * 0.27 * f
            draw.ellipse([x - pip, y - pip, x + pip, y + pip], fill=INK)
    monogram(draw, c, c, f)
    return layer.rotate(ANGLE, resample=BICUBIC, expand=True)


def compose():
    icon = background()
    cube = die()
    x, y = (S - cube.width) // 2, (S - cube.height) // 2 + round(S * 0.01)

    # Мягкая тень под кубиком
    shadow = Image.new("RGBA", cube.size, (25, 10, 70, 0))
    shadow.putalpha(cube.getchannel("A").point(lambda a: a * 0.45))
    shadow = shadow.filter(ImageFilter.GaussianBlur(S * 0.03))
    icon.alpha_composite(shadow, (x, y + round(S * 0.03)))
    icon.alpha_composite(cube, (x, y))
    return icon


def main():
    icon = compose()
    icon.resize((512, 512), LANCZOS).save(os.path.join(HERE, "icon.png"))
    icon.resize((256, 256), LANCZOS).save(
        os.path.join(HERE, "icon.ico"),
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Готово: icon.ico и icon.png")


if __name__ == "__main__":
    main()
