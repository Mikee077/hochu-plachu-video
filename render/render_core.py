"""Ядро монтажа «Видео Хочу-Плачу»: 9:16 (1080x1920), карточка на хромакее, полоса с условиями, хук.

Используется и сервисом на сервере (app.py), и запуском в GitHub Actions (render_cli.py).
"""
import os, json, shutil, subprocess, tempfile
from pathlib import Path


class RenderError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


def HTTPException(code, detail):  # совместимость с кодом сервиса
    return RenderError(code, detail)


ASSETS = Path(os.environ.get("ASSETS_DIR", Path(__file__).parent / "assets"))
FONT = os.environ.get("FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MIN_DUR, MAX_DUR = float(os.environ.get("MIN_DUR", "5")), 60.0
DEFAULT_DUR = float(os.environ.get("DEFAULT_DUR", "30"))


def probe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height:stream_tags=rotate:stream_side_data=rotation",
         "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(422, "файл не читается как видео: " + (r.stderr.strip()[-200:] or "ffprobe error"))
    j = json.loads(r.stdout)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise HTTPException(422, "в файле нет видеодорожки")
    has_audio = any(s.get("codec_type") == "audio" for s in j["streams"])
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    # Видео с телефона часто записано «лёжа» с пометкой поворота — учитываем её
    rot = v.get("tags", {}).get("rotate") or next((sd.get("rotation") for sd in v.get("side_data_list", [])
                                                     if "rotation" in sd), 0)
    if abs(int(float(rot or 0))) % 180 == 90:
        w, h = h, w
    return float(j["format"].get("duration", 0)), has_audio, w, h


def wrap_text(text, size, width, max_lines=3):
    """Переносит текст по ширине кадра; убирает эмодзи (шрифт их не рисует)."""
    from PIL import ImageFont, Image, ImageDraw
    text = "".join(ch for ch in text.strip() if ord(ch) < 0x2190 or 0x2010 <= ord(ch) <= 0x2026)
    font = ImageFont.truetype(FONT, size)
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if d.textlength(trial, font=font) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return "\n".join(lines[:max_lines])


def health():
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None,
            "banner": (ASSETS / "banner.png").exists(),
            "overlays": sorted({p.stem.replace("overlay-", "") for p in ASSETS.glob("overlay-*.*")})}


# Карточка на хромакее. Предпочтительно — заранее подготовленный WebM с прозрачностью
# (prepare_overlay.py: чистые края, иконки не вырезаются). Если его нет — ключ на лету по MP4.
KEY_COLOR = os.environ.get("KEY_COLOR", "0x00AF3D")
CROP = os.environ.get("OVERLAY_CROP", "680:640:200:240")  # w:h:x:y для ключа на лету
POSITIONS = {  # отступы под интерфейс площадок: сверху 150 px, справа кнопки начинаются ниже ~1000 px
    "tr": "W-w-16:150", "tl": "16:150", "top": "(W-w)/2:120",
    "br": "W-w-16:H-h-420", "bl": "16:H-h-420",
}


def overlay_filter(variant, scale, pos, start, mode, show):
    """Возвращает (доп. входы ffmpeg, цепочку фильтров) для карточки.
    scale — масштаб относительно исходной анимации 1080x1920; show — сколько секунд показывать (0 — весь оборот)."""
    webm, mp4 = ASSETS / f"overlay-{variant}.webm", ASSETS / f"overlay-{variant}.mp4"
    loop = ["-stream_loop", "-1"] if mode == "loop" else []
    if webm.exists():
        inputs = loop + ["-c:v", "libvpx-vp9", "-i", str(webm)]
        chain = "[1:v]format=yuva420p"
    elif mp4.exists():
        inputs = loop + ["-i", str(mp4)]
        chain = (f"[1:v]crop={CROP},format=yuva444p,chromakey={KEY_COLOR}:0.13:0.06,"
                 "despill=type=green:mix=0.6:expand=0.1,format=yuva420p")
    else:
        raise HTTPException(422, f"нет файла карточки overlay-{variant} на сервере")
    chain += f",scale=iw*{scale:.3f}:-2:flags=lanczos"
    if show > 0 and mode != "loop":
        chain += f",trim=0:{show:.2f},fade=t=out:st={max(0, show - 0.35):.2f}:d=0.35:alpha=1"
    chain += f",setpts=PTS-STARTPTS+{start}/TB[ov]"
    xy = POSITIONS.get(pos, POSITIONS["tr"])
    eof = "repeat" if mode == "loop" else "pass"
    return inputs, [chain, f"[base][ov]overlay={xy}:eof_action={eof}:enable='gte(t,{start})'[v1]"]



# ---------- Полоса с условиями внизу кадра ----------
NAVY, NAVY2, GOLD1, GOLD2 = (22, 26, 64), (12, 14, 38), (240, 205, 130), (190, 140, 60)
BAR_W = 1000
BAR_BOTTOM = 1920 - 400   # нижняя граница полосы: ниже — подписи и кнопки площадок
DEFAULT_BAR_TITLE = "Зарубежная карта от 890 ₽"
DEFAULT_BAR_ITEMS = ["Готова за 5–20 минут", "Без документов", "СБП без комиссии"]
BAR_CACHE = Path(os.environ.get("BAR_CACHE", "/tmp/bars"))


def _font(size, bold=True):
    from PIL import ImageFont
    return ImageFont.truetype(FONT if bold else FONT.replace("-Bold", ""), size)


def _gold_text(img, xy, text, font, anchor="la"):
    """Текст с вертикальным золотым градиентом, как на карточке."""
    from PIL import Image, ImageDraw
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255, anchor=anchor)
    grad = Image.new("RGBA", img.size)
    top, bottom = mask.getbbox()[1], mask.getbbox()[3]
    gd = ImageDraw.Draw(grad)
    for y in range(top, bottom + 1):
        k = (y - top) / max(1, bottom - top)
        gd.line([(0, y), (img.size[0], y)], fill=tuple(int(GOLD1[i] + (GOLD2[i] - GOLD1[i]) * k) for i in range(3)) + (255,))
    img.paste(grad, (0, 0), mask)


def _panel(w, h, radius):
    """Тёмно-синяя плашка с градиентом, золотой обводкой и мягкой тенью."""
    from PIL import Image, ImageDraw, ImageFilter
    pad = 24
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad + 8, pad + w, pad + h + 8), radius, fill=(0, 0, 0, 150))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(12)))
    body = Image.new("RGBA", (w, h))
    bd = ImageDraw.Draw(body)
    for y in range(h):
        k = y / max(1, h - 1)
        bd.line([(0, y), (w, y)], fill=tuple(int(NAVY[i] + (NAVY2[i] - NAVY[i]) * k) for i in range(3)) + (238,))
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=255)
    img.paste(body, (pad, pad), m)
    ImageDraw.Draw(img).rounded_rectangle((pad, pad, pad + w - 1, pad + h - 1), radius, outline=GOLD2 + (255,), width=3)
    return img, pad


def _check(img, cx, cy, r):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=GOLD1 + (255,))
    d.line([(cx - r * 0.45, cy + r * 0.02), (cx - r * 0.1, cy + r * 0.38), (cx + r * 0.5, cy - r * 0.35)],
           fill=NAVY2 + (255,), width=max(3, int(r * 0.28)), joint="curve")


def bar_static(title, items):
    """Вариант A: две строки — золотой заголовок и три условия с галочками."""
    import hashlib
    key = hashlib.md5(("A" + title + "|".join(items)).encode()).hexdigest()[:12]
    out = BAR_CACHE / f"bar-a-{key}.png"
    if out.exists():
        return out
    from PIL import ImageDraw
    h = 190
    img, pad = _panel(BAR_W, h, 36)
    _gold_text(img, (pad + BAR_W / 2, pad + 52), title, _font(50), anchor="mm")
    d = ImageDraw.Draw(img)
    items = items[:3]
    size = 32
    while True:  # подбираем шрифт, чтобы условия поместились с зазором не меньше 28 px
        f = _font(size)
        r = int(size * 0.5)
        widths = [d.textlength(t, font=f) + 2 * r + 12 for t in items]
        gap = (BAR_W - 64 - sum(widths)) / max(1, len(items) - 1)
        if gap >= 28 or size <= 20:
            break
        size -= 1
    if len(items) == 1:
        x = pad + (BAR_W - widths[0]) / 2
    else:
        x = pad + 32
    for t, w in zip(items, widths):
        _check(img, x + r, pad + 134, r)
        d.text((x + 2 * r + 12, pad + 134), t, font=f, fill=(255, 255, 255, 255), anchor="lm")
        x += w + gap
    BAR_CACHE.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def bar_ticker(items):
    """Вариант B: одна строка, условия сменяют друг друга; справа адрес сайта."""
    import hashlib
    from PIL import ImageDraw
    paths = []
    for t in items:
        key = hashlib.md5(("B" + t).encode()).hexdigest()[:12]
        out = BAR_CACHE / f"bar-b-{key}.png"
        if not out.exists():
            h = 120
            img, pad = _panel(BAR_W, h, 60)
            _check(img, pad + 62, pad + h / 2, 30)
            d = ImageDraw.Draw(img)
            size = 42
            while d.textlength(t, font=_font(size)) > BAR_W - 420 and size > 28:
                size -= 2
            d.text((pad + 112, pad + h / 2), t, font=_font(size), fill=(255, 255, 255, 255), anchor="lm")
            _gold_text(img, (pad + BAR_W - 36, pad + h / 2), "hochu-plachu.com", _font(28), anchor="rm")
            BAR_CACHE.mkdir(parents=True, exist_ok=True)
            img.save(out)
        paths.append(out)
    return paths


def bar_filters(kind, title, items, out_dur, first_input, base_label):
    """Возвращает (входы, фильтры, итоговая метка, верх полосы в px)."""
    inputs, f = [], []
    slide = "if(lt(t,0.45),{y}+90*(1-t/0.45),{y})"
    if kind == "static":
        png = bar_static(title, items)
        from PIL import Image
        w, h = Image.open(png).size
        y = BAR_BOTTOM - h
        inputs += ["-loop", "1", "-t", f"{out_dur:.2f}", "-i", str(png)]
        f.append(f"[{first_input}:v]format=rgba,fade=t=in:st=0:d=0.45:alpha=1[bar0]")
        f.append(f"[{base_label}][bar0]overlay=x=(W-w)/2:y='{slide.format(y=y)}':eval=frame:eof_action=pass[vbar]")
        return inputs, f, "vbar", y + 24
    pngs = bar_ticker(items)
    from PIL import Image
    w, h = Image.open(pngs[0]).size
    y = BAR_BOTTOM - h
    slot, xf = 3.0, 0.3
    n = max(1, int(-(-out_dur // slot)))
    last = base_label
    for i in range(n):
        st = i * slot
        ln = min(slot + xf, out_dur - st)
        inputs += ["-loop", "1", "-t", f"{ln:.2f}", "-i", str(pngs[i % len(pngs)])]
        fades = f"fade=t=in:st=0:d={0.45 if i == 0 else xf}:alpha=1"
        if st + ln < out_dur - 0.05:
            fades += f",fade=t=out:st={ln - xf:.2f}:d={xf}:alpha=1"
        f.append(f"[{first_input + i}:v]format=rgba,{fades},setpts=PTS-STARTPTS+{st:.2f}/TB[bar{i}]")
        ypos = f"'{slide.format(y=y)}'" if i == 0 else str(y)
        f.append(f"[{last}][bar{i}]overlay=x=(W-w)/2:y={ypos}:eval=frame:eof_action=pass[vb{i}]")
        last = f"vb{i}"
    return inputs, f, last, y + 24


DEFAULTS = dict(start=-1, duration=0, hook="", marking="", banner_from=0, overlay="logo", overlay_pos="tr",
                overlay_scale=0.68, overlay_mode="loop", overlay_show=0, bar="auto", vertical_boost=1.10,
                bar_title="", bar_items="")


def render_video(src, out, **params):
    """Монтирует src в out (MP4). Параметры — как у POST /render. Возвращает сведения о ролике."""
    p = {**DEFAULTS, **{k: v for k, v in params.items() if k in DEFAULTS and v is not None and v != ""}}
    start, duration = float(p["start"]), float(p["duration"])
    hook, marking, banner_from = str(p["hook"]), str(p["marking"]), float(p["banner_from"])
    overlay, overlay_pos = str(p["overlay"]).lower(), str(p["overlay_pos"]).lower()
    overlay_scale, overlay_mode, overlay_show = float(p["overlay_scale"]), str(p["overlay_mode"]).lower(), float(p["overlay_show"])
    bar, vertical_boost = str(p["bar"]).lower(), float(p["vertical_boost"])
    bar_title, bar_items = str(p["bar_title"]), str(p["bar_items"])
    src, out = Path(src), Path(out)
    tmp = Path(tempfile.mkdtemp())
    try:
        src_dur, has_audio, src_w, src_h = probe(src)
        vertical = src_h >= src_w * 1.2  # 9:16, 3:4 и т. п.
        orientation = "vertical" if vertical else "horizontal"
        # Видимая область исходника в кадре 1080x1920 (вписан по ширине или высоте)
        fit = min(1080 / max(1, src_w), 1920 / max(1, src_h))
        video_top = int((1920 - src_h * fit) / 2)
        if bar == "auto":  # горизонтальным — полоса A под видео, вертикальным — без полосы
            bar = "none" if vertical else "static"
        if vertical:
            overlay_scale *= max(1.0, vertical_boost)  # вертикальным карточка крупнее
        if src_dur < 3:
            raise HTTPException(422, f"исходник слишком короткий: {src_dur:.1f} с")
        dur = duration if duration > 0 else DEFAULT_DUR
        dur = max(MIN_DUR, min(MAX_DUR, dur))
        if start < 0:  # авто: берём середину ролика
            start = max(0.0, (src_dur - dur) / 2)
        dur = min(dur, max(0.0, src_dur - start))
        loop = dur < MIN_DUR  # исходник короче минимума — зацикливаем
        out_dur = MIN_DUR if loop else dur

        # Фон: размытая копия на весь кадр; поверх — исходник целиком по ширине.
        f = [
            "[0:v]split=2[a][b]",
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2,eq=brightness=-0.08[bg]",
            "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[base]",
        ]
        overlay = overlay if overlay in ("logo", "nologo", "png", "none") else "logo"
        extra_inputs = []
        if overlay == "png":
            # Старый вариант: PNG-баннер ~10% кадра, покачивание ±7°, правый верхний угол.
            extra_inputs = ["-loop", "1", "-i", str(ASSETS / "banner.png")]
            f += ["[1:v]scale=440:-1,format=rgba,rotate='0.12*sin(2*PI*t/2)':c=none:ow=rotw(0.13):oh=roth(0.13)[ban]",
                  f"[base][ban]overlay=W-w-24:150:enable='gte(t,{banner_from})'[v1]"]
            last = "v1"
        elif overlay == "none":
            last = "base"
        else:
            extra_inputs, chain = overlay_filter(overlay, max(0.3, min(1.5, overlay_scale)), overlay_pos,
                                                 max(0.0, banner_from), overlay_mode, max(0.0, overlay_show))
            f += chain
            last = "v1"
        bar_top = None
        if bar in ("static", "ticker"):
            items = [x.strip() for x in bar_items.split("|") if x.strip()] or DEFAULT_BAR_ITEMS
            n_inputs = 1 + (1 if overlay != "none" else 0)
            b_in, b_f, last, bar_top = bar_filters(bar, bar_title.strip() or DEFAULT_BAR_TITLE, items[:6],
                                                   out_dur, n_inputs, last)
            extra_inputs += b_in
            f += b_f
        if hook.strip():
            (tmp / "hook.txt").write_text(wrap_text(hook, 58, 960), encoding="utf-8")
            f.append(f"[{last}]drawtext=fontfile={FONT}:textfile={tmp/'hook.txt'}:fontsize=58:fontcolor=white:text_align=C:"
                     "borderw=4:bordercolor=black@0.8:line_spacing=8:x=(w-text_w)/2:"
                     + (f"y={video_top + 30}[v2]" if bar_top and not vertical and video_top > 200
                        else f"y={bar_top - 40}-text_h[v2]" if bar_top else "y=h*0.72[v2]"))
            last = "v2"
        if marking.strip():
            (tmp / "mark.txt").write_text(marking.strip()[:160], encoding="utf-8")
            f.append(f"[{last}]drawtext=fontfile={FONT}:textfile={tmp/'mark.txt'}:fontsize=26:fontcolor=white@0.9:"
                     "box=1:boxcolor=black@0.35:boxborderw=8:x=32:y=60[v3]")
            last = "v3"

        cmd = ["ffmpeg", "-y", "-v", "error"]
        cmd += ["-stream_loop", "-1"] if loop else []
        cmd += ["-ss", f"{start:.2f}", "-i", str(src)] + extra_inputs
        cmd += ["-filter_complex", ";".join(f), "-map", f"[{last}]"]
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"] if has_audio else ["-an"]
        cmd += ["-t", f"{out_dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise HTTPException(500, "ffmpeg: " + r.stderr[-800:])
        return {"duration": round(out_dur, 2), "start": round(start, 2), "width": 1080, "height": 1920,
                "source_duration": round(src_dur, 2), "looped": loop, "overlay": overlay,
                "orientation": orientation, "bar": bar, "source_size": f"{src_w}x{src_h}", "size": out.stat().st_size}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
