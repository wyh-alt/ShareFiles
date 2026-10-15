# -*- coding: utf-8 -*-
"""生成 icon.ico —— Fluent 风格的分享图标（蓝底 + 白色上传箭头）。

    python make_icon.py

想换颜色或造型，改下面的常量再跑一次即可。
"""

import os

from PIL import Image, ImageDraw

SIZE = 1024                     # 先按大尺寸画，最后缩下去，边缘才平滑
TOP = (0x00, 0x86, 0xF0)        # 渐变起始（上）
BOTTOM = (0x00, 0x50, 0xA0)     # 渐变结束（下）
RADIUS = 0.225                  # 圆角比例
STROKE = 0.074                  # 线条粗细比例


def rounded_mask(size: int, radius_ratio: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * radius_ratio), fill=255
    )
    return mask


def gradient(size: int) -> Image.Image:
    grad = Image.new("RGB", (1, size))
    px = grad.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))
    return grad.resize((size, size))


def draw_glyph(img: Image.Image, size: int) -> None:
    d = ImageDraw.Draw(img)
    w = int(size * STROKE)
    half = w // 2

    def line(p1, p2):
        d.line([p1, p2], fill="white", width=w)
        for p in (p1, p2):                      # 补圆头
            d.ellipse([p[0] - half, p[1] - half, p[0] + half, p[1] + half], fill="white")

    # 托盘（下方 U 形）
    line((size * 0.285, size * 0.615), (size * 0.285, size * 0.775))
    line((size * 0.285, size * 0.775), (size * 0.715, size * 0.775))
    line((size * 0.715, size * 0.775), (size * 0.715, size * 0.615))

    # 箭头（竖线 + 三角）
    line((size * 0.5, size * 0.735), (size * 0.5, size * 0.335))
    d.polygon(
        [(size * 0.5, size * 0.175),
         (size * 0.325, size * 0.385),
         (size * 0.675, size * 0.385)],
        fill="white",
    )


def main() -> int:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    img.paste(gradient(SIZE), (0, 0), rounded_mask(SIZE, RADIUS))
    draw_glyph(img, SIZE)

    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "icon.ico")
    img.save(out, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(here, "icon.png"))
    print(f"已生成 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
