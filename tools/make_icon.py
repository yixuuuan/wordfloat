"""生成 AppIcon.icns：系统蓝圆角方块 + 白色「浮」字。需要 Pillow 和一款中文衬线字体。"""
import sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"
OUT = sys.argv[2] if len(sys.argv) > 2 else "AppIcon.icns"
S = 1024
M = 100                       # macOS 图标网格：824 内容区，四周留 100
R = 185                       # 圆角

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# 阴影
sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(sh).rounded_rectangle([M, M + 12, S - M, S - M + 12], R, fill=(0, 0, 0, 90))
img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(18)))
# 竖向渐变：亮蓝 → 系统蓝
grad = Image.new("RGBA", (S, S))
top, bot = (64, 156, 255), (0, 98, 230)
for y in range(S):
    t = y / (S - 1)
    grad.paste(tuple(int(a + (b - a) * t) for a, b in zip(top, bot)) + (255,), (0, y, S, y + 1))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([M, M, S - M, S - M], R, fill=255)
img.paste(grad, (0, 0), mask)
# 「浮」
d = ImageDraw.Draw(img)
f = ImageFont.truetype(FONT, 520, index=2)   # SC
bb = d.textbbox((0, 0), "浮", font=f)
w, h = bb[2] - bb[0], bb[3] - bb[1]
d.text(((S - w) / 2 - bb[0], (S - h) / 2 - bb[1] - 10), "浮", font=f, fill=(255, 255, 255, 255))
img.save(OUT, sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
img.save(OUT.rsplit(".", 1)[0] + ".png")
print("ok", OUT)
