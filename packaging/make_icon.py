"""Draw cryrec.ico: dark rounded tile, white ring, red record dot with a small sound wave.
Drawn at 1024 px and downsampled. Sizes up to 24 px use a simpler version (no wave, thicker ring)
so the taskbar and tray icons stay sharp."""
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024


def tile():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.23), fill=255)
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S)
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip((46, 46, 54), (16, 16, 22)))
    img.paste(grad, (0, 0), mask)
    hl = Image.new("L", (S, S), 0)
    ImageDraw.Draw(hl).ellipse([-S * 0.2, -S * 0.75, S * 1.2, S * 0.45], fill=34)
    img.paste(Image.new("RGBA", (S, S), (255, 255, 255, 255)), (0, 0), Image.composite(hl, Image.new("L", (S, S)), mask))
    return img


def draw(simple):
    img = tile()
    d = ImageDraw.Draw(img)
    ring = 0.09 if simple else 0.06
    d.ellipse([S * 0.16, S * 0.16, S * 0.84, S * 0.84], outline="white", width=int(S * ring))
    inner = 0.31 if simple else 0.29
    d.ellipse([S * inner, S * inner, S * (1 - inner), S * (1 - inner)], fill=(232, 64, 64))
    if not simple:
        heights, w, gap = (0.10, 0.22, 0.30, 0.18, 0.10), S * 0.04, S * 0.03
        x0 = S * 0.5 - (len(heights) * w + (len(heights) - 1) * gap) / 2
        for i, h in enumerate(heights):
            x = x0 + i * (w + gap)
            d.rounded_rectangle([x, S * 0.5 - S * h / 2, x + w, S * 0.5 + S * h / 2], radius=w / 2,
                                fill=(255, 230, 230))
    return img


full, simple = draw(False), draw(True)
frames = [(simple if s <= 24 else full).resize((s, s), Image.LANCZOS) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
out = Path(__file__).resolve().parent.parent / "cryrec.ico"
frames[-1].save(out, format="ICO", sizes=[f.size for f in frames], append_images=frames[:-1])
print(out)
