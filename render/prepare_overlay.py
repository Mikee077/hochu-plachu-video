"""Готовит карточку на хромакее к наложению: вырезает зелёный фон и сохраняет WebM (VP9) с прозрачностью.

Запускается один раз на каждый ролик с хромакеем:
    python3 prepare_overlay.py greenscreen.mp4 assets/overlay-logo.webm

Почему не обычный chromakey ffmpeg: зелёный в иконке Spotify почти совпадает с цветом фона
и вырезается вместе с ним. Здесь фон определяется ещё и по форме: зелёное, полностью окружённое
непрозрачными пикселями (внутри иконки или карточки), считается частью рисунка.
"""
import subprocess, sys, json
import numpy as np
from scipy import ndimage

T0, T1 = 18.0, 55.0     # расстояние по цветности до ключа: <T0 — фон, >T1 — рисунок, между — полупрозрачно
PAD = 24                # запас вокруг найденной области


def cbcr(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return np.stack([-0.1687 * r - 0.3313 * g + 0.5 * b, 0.5 * r - 0.4187 * g - 0.0813 * b], -1)


def frames(path, w, h, vf=None):
    cmd = ["ffmpeg", "-v", "error", "-i", path] + (["-vf", vf] if vf else []) + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = w * h * 3
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    p.wait()


def main(src, dst):
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                      "stream=width,height,r_frame_rate", "-of", "json", src],
                                     capture_output=True, text=True, check=True).stdout)["streams"][0]
    W, H, fps = info["width"], info["height"], info["r_frame_rate"]

    # 1) Цвет ключа — по краям первого кадра; 2) область рисунка — объединение по всем кадрам.
    first = next(frames(src, W, H)).astype(float)
    border = np.concatenate([first[:20].reshape(-1, 3), first[-20:].reshape(-1, 3),
                             first[:, :20].reshape(-1, 3), first[:, -20:].reshape(-1, 3)])
    key = cbcr(np.median(border, 0))
    union = np.zeros((H, W), bool)
    for fr in frames(src, W, H):
        union |= np.linalg.norm(cbcr(fr.astype(float)) - key, axis=-1) > T0
    union = ndimage.binary_opening(union, iterations=2)
    ys, xs = np.where(union)
    x0, x1 = max(0, xs.min() - PAD), min(W, xs.max() + PAD)
    y0, y1 = max(0, ys.min() - PAD), min(H, ys.max() + PAD)
    cw, ch = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    print(f"ключ CbCr={key.round(1)}, область рисунка {cw}x{ch} от ({x0},{y0})")

    enc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{cw}x{ch}",
                            "-r", fps, "-i", "-", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0",
                            "-crf", "18", "-row-mt", "1", "-auto-alt-ref", "0", dst], stdin=subprocess.PIPE)
    n = 0
    for fr in frames(src, cw, ch, vf=f"crop={cw}:{ch}:{x0}:{y0}"):
        rgb = fr.astype(float)
        d = np.linalg.norm(cbcr(rgb) - key, axis=-1)
        a = np.clip((d - T0) / (T1 - T0), 0, 1)
        a = ndimage.median_filter(a, size=3)                 # гасим шум сжатия на фоне
        solid = ndimage.binary_fill_holes(a > 0.5)           # зелёное внутри рисунка — тоже рисунок
        inner = solid & (a <= 0.5)
        a = np.where(solid, np.maximum(a, 1.0 * inner), a)
        # Убираем зелёный отсвет только на краях и в тени, не трогая зелёное внутри иконок.
        edge = (a < 0.999) & ~inner
        g = rgb[..., 1]
        lim = np.maximum(rgb[..., 0], rgb[..., 2])
        rgb[..., 1] = np.where(edge, np.minimum(g, lim), g)
        out = np.dstack([rgb, a * 255]).clip(0, 255).astype(np.uint8)
        enc.stdin.write(out.tobytes())
        n += 1
    enc.stdin.close()
    enc.wait()
    print(f"готово: {dst}, кадров {n}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
