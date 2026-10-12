"""Draw cryrec.ico: a red record dot on a dark rounded square."""
from pathlib import Path
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=56, fill=(28, 30, 38, 255))
d.ellipse([60, 60, S - 60, S - 60], fill=(220, 38, 38, 255))
d.ellipse([92, 84, 132, 124], fill=(255, 255, 255, 70))
out = Path(__file__).resolve().parent.parent / "cryrec.ico"
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
