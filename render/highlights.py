"""Поиск самых сильных моментов в длинном ролике и субтитры.

Без внешних API: расшифровка речи (faster-whisper на CPU), громкость звука, смена кадров.
Оценка 0–100 повторяет веса из промпта, насколько это возможно без понимания смысла:
  реакция/эмоция (всплеск громкости, смех, восклицания)  — 35
  реплики-маркеры смешного и неожиданного                  — 25
  хук: сильное начало в первые 2 секунды                    — 15
  темп речи                                                  — 15
  динамика кадра                                             — 10
Окончательный отбор по смыслу — за человеком или LLM-оценщиком (поле text у каждого момента).
"""
import json, math, re, subprocess, wave
from pathlib import Path

import numpy as np

BIN = 0.5  # шаг анализа, с
LAUGH = re.compile(r"(?i)(ха(-?ха)+|хах|ахах|хех|хи-?хи|ржу|ору|угар|смешн|\[смех\]|\(смех\))")
REACT = re.compile(r"(?i)\b(что\?!|чего\?!|блин|блять|бля|офиг|охренеть|капец|жесть|вау|ого|ебать|пипец|нифига|"
                   r"серьёзно|серьезно|да ладно|не может быть|какого|ну всё|ну все|ааа+|ооо+|фу+)\b")


def _run(cmd, timeout=1800):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(" ".join(cmd[:3]) + ": " + r.stderr[-500:])
    return r


def extract_audio(src, wav, start=None, dur=None):
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(src)]
    if dur is not None:
        cmd += ["-t", f"{dur:.2f}"]
    _run(cmd + ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])


def rms_bins(wav):
    with wave.open(str(wav)) as w:
        sr, n = w.getframerate(), w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    step = int(sr * BIN)
    nb = max(1, len(x) // step)
    x = x[: nb * step].reshape(nb, step)
    return 20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1e-6)


def scene_bins(src, nb):
    """Средняя сила смены кадра в каждом 0,5-секундном отрезке (кадры 2 в секунду, уменьшенные)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-skip_frame", "nokey", "-i", str(src), "-an", "-vf",
                        "scale=160:-2,select='gte(scene,0)',metadata=print:file=-", "-vsync", "vfr", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=1800)
    out = np.zeros(nb)
    t = None
    for line in r.stdout.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m and t is not None:
            i = min(nb - 1, int(t / BIN))
            out[i] = max(out[i], float(m.group(1)))
    return out


_MODEL = {}


def whisper(name):
    if name not in _MODEL:
        from faster_whisper import WhisperModel
        _MODEL[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=4)
    return _MODEL[name]


def _pcm(wav):
    """Звук как массив float32 16 кГц: так faster-whisper не открывает файл через PyAV (несовместимость версий)."""
    with wave.open(str(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(wav, model="base", words=False):
    segs, info = whisper(model).transcribe(_pcm(wav), language="ru", vad_filter=True, beam_size=1,
                                           word_timestamps=words, condition_on_previous_text=False)
    out = []
    for s in segs:
        item = {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
        if words:
            item["words"] = [{"start": round(w.start, 2), "end": round(w.end, 2), "w": w.word.strip()}
                             for w in (s.words or []) if w.word.strip()]
        out.append(item)
    return out


def _z(a):
    a = np.asarray(a, dtype=np.float32)
    sd = a.std()
    return (a - a.mean()) / sd if sd > 1e-6 else np.zeros_like(a)


def _rolling_median(a, win):
    pad = win // 2
    ap = np.pad(a, pad, mode="edge")
    return np.array([np.median(ap[i:i + win]) for i in range(len(a))])


def find_moments(src, work, k=5, min_dur=7.0, max_dur=20.0, target=15.0, model=None, layout="clip"):
    """Возвращает список лучших непересекающихся моментов [{start, duration, score, text, ...}].

    layout="clip" — короткий момент (подводка → пик → реакция);
    layout="episode" — серия 60–120 с: нарастание к пику ближе к концу и обрыв сразу после него (клиффхэнгер).
    """
    work = Path(work)
    wav = work / "full.wav"
    extract_audio(src, wav)
    rms = rms_bins(wav)
    nb = len(rms)
    total = nb * BIN
    model = model or ("small" if total <= 25 * 60 else "base" if total <= 50 * 60 else "tiny")
    segs = transcribe(wav, model=model)
    scene = scene_bins(src, nb)

    # 1. Эмоция: всплеск громкости относительно фона последних ~30 с
    excite = np.clip(_z(rms - _rolling_median(rms, int(30 / BIN) | 1)), -1, 4)
    silent = rms < -45
    # 2. Маркеры смешного/реакции и 3. темп речи — по сегментам расшифровки
    laugh = np.zeros(nb); react = np.zeros(nb); speech = np.zeros(nb)
    for s in segs:
        a, b = int(s["start"] / BIN), max(int(s["start"] / BIN) + 1, int(s["end"] / BIN))
        a, b = max(0, a), min(nb, b)
        txt = s["text"]
        wps = len(txt.split()) / max(0.5, s["end"] - s["start"])
        speech[a:b] = np.maximum(speech[a:b], min(wps / 3.0, 1.5))
        if LAUGH.search(txt):
            laugh[max(0, a - 1):min(nb, b + 2)] = 1
        n = len(REACT.findall(txt)) + txt.count("!") * 0.5 + txt.count("?") * 0.3
        if n:
            react[a:b] = np.maximum(react[a:b], min(n, 3) / 3)
    scene_z = np.clip(_z(scene), -1, 3)

    # Кандидаты: окрестности пиков эмоции/реакции
    peak = excite + 1.5 * laugh + 1.2 * react
    order = np.argsort(-peak)
    seg_starts = [s["start"] for s in segs]
    seg_ends = [s["end"] for s in segs]
    cands = []
    used = np.zeros(nb, dtype=bool)
    for i in order[: max(60, k * 15)]:
        if used[i] or peak[i] <= 0.3:
            continue
        t = i * BIN
        if layout == "episode":
            # Серия: пик на ~70% длины, начало — с начала фразы, конец — фраза после пика + 0,8 с (обрыв)
            st0 = max(0.0, t - 0.7 * target)
            st = next((x for x in seg_starts if st0 - 3 <= x <= st0 + 6), st0)
            en_opts = [x for x in seg_ends if t + 2 <= x <= t + 12]
            en = (en_opts[0] + 0.8) if en_opts else min(total, t + 5)
        else:
            # Начало — начало фразы за 2–6 с до пика (подводка), конец — фраза после пика + реакция
            st = max([x for x in seg_starts if t - 8 <= x <= t - 1.5] or [max(0.0, t - 4)])
            st = min(st, t - 1.0) if t > 1 else 0.0
            en_opts = [x for x in seg_ends if t + 1.5 <= x <= st + max_dur]
            en = (en_opts[0] + 1.0) if en_opts else min(total, st + target)
        dur = max(min_dur, min(max_dur, en - st))
        if st + dur > total:
            st = max(0.0, total - dur)
        a, b = int(st / BIN), min(nb, int((st + dur) / BIN))
        if b - a < 4 or silent[a:b].mean() > 0.5:
            continue
        hook2 = int(2 / BIN)
        if layout == "episode":
            if used[a:b].any():  # серия пересекается с уже выбранной
                continue
            # Веса из промпта мини-серий: напряжение 30, понятность 20, хук 15, клиффхэнгер 15, темп 10, место для карточки 10
            third = max(1, (b - a) // 3)
            build = excite[b - third:b].mean() - excite[a:a + third].mean()  # нарастание к концу
            tail = int(5 / BIN)
            score = (30 * np.clip((peak[a:b].max() / 4 + max(0.0, build) / 2) / 1.5, 0, 1)
                     + 20 * np.clip(speech[a:b].mean(), 0, 1)
                     + 15 * np.clip((excite[a:a + int(3 / BIN)].mean() + react[a:a + int(3 / BIN)].max() + 1) / 3, 0, 1)
                     + 15 * np.clip((excite[max(a, b - tail):b].mean() + react[max(a, b - tail):b].max() + 1) / 3, 0, 1)
                     + 10 * np.clip((scene_z[a:b].mean() + 1) / 3, 0, 1)
                     + 10 * np.clip(1 - silent[a:b].mean() * 2, 0, 1))
            text = " ".join(s["text"] for s in segs if s["end"] > st and s["start"] < st + dur)
            cands.append({"start": round(float(st), 2), "duration": round(float(dur), 2), "peak": round(t, 2),
                          "score": int(round(float(score))), "text": text[:900]})
            used[max(0, a):min(nb, b)] = True  # серии не пересекаются
            continue
        score = (35 * np.clip(peak[a:b].max() / 4, 0, 1)
                 + 25 * np.clip((laugh[a:b].max() + react[a:b].mean() * 2) / 2, 0, 1)
                 + 15 * np.clip((excite[a:a + hook2].mean() + react[a:a + hook2].max() + 1) / 3, 0, 1)
                 + 15 * np.clip(speech[a:b].mean(), 0, 1)
                 + 10 * np.clip((scene_z[a:b].mean() + 1) / 3, 0, 1))
        text = " ".join(s["text"] for s in segs if s["end"] > st and s["start"] < st + dur)
        cands.append({"start": round(float(st), 2), "duration": round(float(dur), 2), "peak": round(t, 2),
                      "score": int(round(float(score))), "text": text[:600]})
        used[max(0, a - int(3 / BIN)):min(nb, b + int(3 / BIN))] = True
    cands.sort(key=lambda c: -c["score"])
    return {"source_duration": round(total, 2), "model": model, "candidates": cands[: max(k * 3, 10)],
            "moments": cands[:k], "segments": len(segs)}


def contact_sheet(src, out, times, width=320):
    """Картинка-превью: по кадру на каждый момент (для проверки человеком)."""
    tmp = Path(out).with_suffix("")
    tmp.mkdir(exist_ok=True)
    files = []
    for n, t in enumerate(times[:12]):
        f = tmp / f"f{n:02d}.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", str(f)], timeout=120)
        if f.exists():
            files.append(f)
    if not files:
        return None
    from PIL import Image, ImageDraw
    ims = [Image.open(f).convert("RGB") for f in files]
    w, h = ims[0].size
    cols = min(4, len(ims))
    rows = math.ceil(len(ims) / cols)
    sheet = Image.new("RGB", (cols * w, rows * h), "black")
    d = ImageDraw.Draw(sheet)
    for n, im in enumerate(ims):
        x, y = (n % cols) * w, (n // cols) * h
        sheet.paste(im.resize((w, h)), (x, y))
        d.rectangle([x, y, x + 46, y + 22], fill="black")
        d.text((x + 6, y + 4), f"#{n + 1}", fill="yellow")
    sheet.save(out, quality=85)
    return out


# ---------- субтитры ----------

def _ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def make_ass(words_segs, path, y, font="DejaVu Sans", size=72, max_words=3):
    """ASS-файл: крупные белые слова с чёрной обводкой, по 2–3 слова, текущее слово жёлтое."""
    chunks = []
    for s in words_segs:
        ws = []
        for w in s.get("words") or []:
            # «-то», «-нибудь» и отдельные знаки препинания приклеиваем к предыдущему слову
            if ws and (w["w"].startswith("-") or not re.search(r"\w", w["w"])):
                ws[-1] = {**ws[-1], "w": ws[-1]["w"] + w["w"], "end": w["end"]}
            else:
                ws.append(dict(w))
        for w in ws:
            w["w"] = re.sub(r"([.,!?…])(?=\w)", r"\1 ", w["w"])
        for i in range(0, len(ws), max_words):
            part = ws[i:i + max_words]
            if part:
                chunks.append(part)
    lines = []
    for ci, part in enumerate(chunks):
        end_chunk = part[-1]["end"]
        nxt = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else end_chunk + 0.4
        end_chunk = min(max(end_chunk, part[-1]["end"]) + 0.25, nxt)
        for wi, w in enumerate(part):
            st = w["start"]
            en = part[wi + 1]["start"] if wi + 1 < len(part) else end_chunk
            # верхний регистр — только у слов: теги ASS (\\c) должны остаться строчными
            txt = " ".join(("{\\c&H00E5FF&}" + x["w"].upper() + "{\\c&HFFFFFF&}") if j == wi else x["w"].upper()
                           for j, x in enumerate(part))
            # \pos отключает «раздвигание» одновременных строк libass — текст всегда на своём месте
            lines.append(f"Dialogue: 0,{_ass_time(st)},{_ass_time(max(en, st + 0.05))},S,,0,0,0,,"
                         f"{{\\an8\\pos(540,{int(y)})}}{txt}")
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"Style: S,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,8,60,60,{int(y)},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def clip_subtitles(src, start, dur, work, path, y, model="small"):
    """Расшифровывает только нужный кусок и пишет ASS. Возвращает число строк (0 — речи нет)."""
    wav = Path(work) / "clip.wav"
    extract_audio(src, wav, start, dur)
    segs = transcribe(wav, model=model, words=True)
    return make_ass(segs, path, y) if segs else 0


if __name__ == "__main__":
    import sys
    print(json.dumps(find_moments(sys.argv[1], Path(sys.argv[2] if len(sys.argv) > 2 else "."), k=5),
                     ensure_ascii=False, indent=1))
