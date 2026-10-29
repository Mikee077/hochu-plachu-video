"""Сервис монтажа «Видео Хочу-Плачу» (вариант с сервером).

POST /render  — монтаж (параметры — см. render_core.DEFAULTS), файл или source_url.
GET  /files/… — готовые MP4 по публичной ссылке.
GET  /health  — проверка.
Ключ доступа: заголовок X-API-Key = переменная окружения RENDER_KEY.
"""
import os, time, uuid, shutil, secrets, tempfile, urllib.request
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.staticfiles import StaticFiles
import render_core

RENDER_KEY = os.environ.get("RENDER_KEY", "")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "http://localhost:8000").rstrip("/")
OUT_DIR = Path(os.environ.get("OUT_DIR", "/data/files"))
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "14"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="video-render")
app.mount("/files", StaticFiles(directory=str(OUT_DIR)), name="files")


def check_key(key):
    if not RENDER_KEY or not secrets.compare_digest(key or "", RENDER_KEY):
        raise HTTPException(401, "bad key")


def cleanup():
    border = time.time() - KEEP_DAYS * 86400
    for f in OUT_DIR.glob("*.mp4"):
        if f.stat().st_mtime < border:
            f.unlink(missing_ok=True)


@app.get("/health")
def health():
    return render_core.health()


@app.post("/render")
def render(
    x_api_key: str = Header(None),
    file: UploadFile | None = File(None),
    source_url: str = Form(""),
    id: str = Form(...),
    start: float = Form(-1), duration: float = Form(0), hook: str = Form(""), marking: str = Form(""),
    banner_from: float = Form(0), overlay: str = Form("logo"), overlay_pos: str = Form("tr"),
    overlay_scale: float = Form(0.68), overlay_mode: str = Form("loop"), overlay_show: float = Form(0),
    bar: str = Form("auto"), vertical_boost: float = Form(1.10), bar_title: str = Form(""), bar_items: str = Form(""),
):
    check_key(x_api_key)
    cleanup()
    safe_id = "".join(c for c in id if c.isalnum() or c in "-_")[:60] or uuid.uuid4().hex
    tmp = Path(tempfile.mkdtemp())
    try:
        src = tmp / "src"
        if file is not None:
            with open(src, "wb") as f:
                shutil.copyfileobj(file.file, f)
        elif source_url.startswith("https://"):
            req = urllib.request.Request(source_url, headers={"User-Agent": "video-render/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r, open(src, "wb") as f:
                shutil.copyfileobj(r, f)
        else:
            raise HTTPException(400, "нужен file или source_url (https)")
        name = f"{safe_id}-{secrets.token_hex(4)}.mp4"
        try:
            meta = render_core.render_video(
                src, OUT_DIR / name, start=start, duration=duration, hook=hook, marking=marking,
                banner_from=banner_from, overlay=overlay, overlay_pos=overlay_pos, overlay_scale=overlay_scale,
                overlay_mode=overlay_mode, overlay_show=overlay_show, bar=bar, vertical_boost=vertical_boost,
                bar_title=bar_title, bar_items=bar_items)
        except render_core.RenderError as e:
            raise HTTPException(e.code, e.detail)
        return {"id": id, "file": name, "url": f"{PUBLIC_BASE}/files/{name}", **meta}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
