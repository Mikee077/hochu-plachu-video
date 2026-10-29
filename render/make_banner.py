"""Собирает assets/banner.png: полупрозрачная плашка, логотип и короткий призыв.

python3 make_banner.py                       # без логотипа: текстовый знак «Хочу-Плачу»
python3 make_banner.py --logo logo.png       # с вашим логотипом (PNG с прозрачностью)
python3 make_banner.py --text "Оплата зарубежных сервисов из России"
"""
import argparse
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H = 560, 560  # итоговый размер; на видео масштабируется до 440 px


def wrap(draw, text, font, width):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logo", default="")
    ap.add_argument("--text", default="Зарубежная карта за 5–20 минут")
    ap.add_argument("--brand", default="Хочу-Плачу")
    ap.add_argument("--color", default="#FF5A1F", help="фирменный цвет плашки")
    ap.add_argument("--out", default=str(Path(__file__).parent / "assets" / "banner.png"))
    a = ap.parse_args()

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r, g, b = (int(a.color[i:i + 2], 16) for i in (1, 3, 5))
    d.rounded_rectangle((8, 8, W - 8, H - 8), radius=64, fill=(r, g, b, 215), outline=(255, 255, 255, 230), width=6)

    top = 48
    if a.logo and Path(a.logo).exists():
        logo = Image.open(a.logo).convert("RGBA")
        logo.thumbnail((W - 120, 230))
        img.alpha_composite(logo, ((W - logo.width) // 2, top))
        top += logo.height + 30
    else:
        f = ImageFont.truetype(FONT, 76)
        for line in wrap(d, a.brand, f, W - 80):
            d.text(((W - d.textlength(line, font=f)) / 2, top), line, font=f, fill="white")
            top += 92
        top += 20

    size = 46
    while size > 26:
        f = ImageFont.truetype(FONT, size)
        lines = wrap(d, a.text, f, W - 90)
        if top + len(lines) * (size + 12) <= H - 50:
            break
        size -= 2
    for line in lines:
        d.text(((W - d.textlength(line, font=f)) / 2, top), line, font=f, fill="white")
        top += size + 12

    d.text((W / 2, H - 70), "hochu-plachu.com", font=ImageFont.truetype(FONT, 30), fill=(255, 255, 255, 220), anchor="mm")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    img.save(a.out)
    print("saved", a.out)


if __name__ == "__main__":
    main()
