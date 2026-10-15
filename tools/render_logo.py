"""画插件 logo：一颗有轨道的小世界 + 一座小屋。

SS 过采样再缩小，边缘不会有锯齿；两处 png（仓库根目录 / 编辑器 assets）
一次生成，保证 README、插件列表与网页编辑器用的是同一张图。

    python tools/render_logo.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

SS = 2  # 过采样倍数
SIZE = 512
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BG = (23, 30, 51)
WORLD = (46, 158, 143)
WORLD_LIGHT = (70, 194, 174)
WALL = (243, 233, 210)
ROOF = (240, 168, 104)
ORBIT = (127, 217, 204)
STAR = (255, 217, 138)
NAVY = (23, 30, 51)


def draw_logo() -> Image.Image:
    canvas = SIZE * SS
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def s(value: float) -> float:
        return value * SS

    # 底板：圆角方块
    draw.rounded_rectangle(
        (0, 0, canvas - 1, canvas - 1), radius=s(104), fill=BG
    )

    # 轨道：虚线圈（用一段段圆弧画出来）
    box = (s(80), s(184), s(432), s(368))
    start = 0
    while start < 360:
        draw.arc(box, start, start + 16, fill=ORBIT, width=s(11))
        start += 30

    # 她的世界
    draw.ellipse((s(144), s(156), s(368), s(380)), fill=WORLD)
    draw.arc(
        (s(144), s(156), s(368), s(380)),
        start=200,
        end=340,
        fill=WORLD_LIGHT,
        width=s(10),
    )

    # 小屋
    draw.rounded_rectangle((s(216), s(268), s(296), s(332)), radius=s(8), fill=WALL)
    draw.polygon(
        [(s(198), s(276)), (s(256), s(212)), (s(314), s(276))], fill=ROOF
    )
    draw.rounded_rectangle((s(286), s(222), s(302), s(254)), radius=s(4), fill=ROOF)
    draw.rounded_rectangle((s(240), s(292), s(268), s(332)), radius=s(6), fill=NAVY)
    draw.rounded_rectangle((s(272), s(286), s(288), s(302)), radius=s(4), fill=ROOF)

    # 轨道上的两点：一颗是你们，一颗是她还没遇到的人
    draw.ellipse((s(74), s(264), s(102), s(292)), fill=STAR)
    draw.ellipse((s(414), s(266), s(438), s(290)), fill=ORBIT)

    return image.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    logo = draw_logo()
    targets = [
        os.path.join(ROOT, "logo.png"),
        os.path.join(ROOT, "pages", "world_editor", "assets", "logo.png"),
    ]
    for path in targets:
        logo.save(path)
        print(f"written: {path}")


if __name__ == "__main__":
    main()
