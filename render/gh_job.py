"""Задание монтажа в GitHub Actions.

n8n кладёт исходник в черновой релиз репозитория (его видят только владельцы),
запускает сценарий render.yml с номером релиза и параметрами, а потом забирает из того же релиза
out.mp4 и result.json. Черновые релизы не публикуются и не создают теги.

Переменные окружения: GITHUB_TOKEN, REPO (owner/name), RELEASE_ID, PARAMS (JSON).

Режимы (PARAMS.mode):
  render  (по умолчанию) — монтаж одного ролика → out.mp4 + result.json;
  analyze — поиск лучших моментов в длинном ролике → result.json (moments, candidates) + preview.jpg.
Исходник: файл source* в релизе или PARAMS.source_url — ссылка Google Drive («доступ по ссылке»), прямая ссылка на файл
или страница площадки (Twitch, YouTube, VK Видео, Kick…) — её качает yt-dlp, только если канал есть в PARAMS.allowed.
"""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import render_core  # noqa: E402

API = os.environ.get("GH_API", "https://api.github.com")
UPLOADS = os.environ.get("GH_UPLOADS", "https://uploads.github.com")
TOKEN, REPO, RID = os.environ["GITHUB_TOKEN"], os.environ["REPO"], os.environ["RELEASE_ID"].strip()


class _NoAuthOnOtherHost(urllib.request.HTTPRedirectHandler):
    """Скачивание файла релиза уходит редиректом на хранилище GitHub: туда токен передавать нельзя."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for h in ("Authorization", "authorization"):
                new.headers.pop(h, None)
                new.unredirected_hdrs.pop(h, None)
        return new


OPENER = urllib.request.build_opener(_NoAuthOnOtherHost)
HDR = {"Authorization": f"Bearer {TOKEN}", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hochu-plachu-video"}


def call(method, url, data=None, headers=None, raw=False, tries=3):
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers={**HDR, **(headers or {})})
        try:
            with OPENER.open(req, timeout=300) as r:
                body = r.read()
                return body if raw else (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == tries - 1:
                raise RuntimeError(f"{method} {url} → {e.code}: {e.read()[:300]!r}")
        except urllib.error.URLError:
            if i == tries - 1:
                raise
        time.sleep(3 * (i + 1))


def upload(path, name, ctype):
    data = Path(path).read_bytes()
    url = f"{UPLOADS}/repos/{REPO}/releases/{RID}/assets?name={name}"
    return call("POST", url, data=data, headers={"Content-Type": ctype})


def ensure_packages(*names):
    import importlib.util, subprocess  # noqa: E401
    need = [n for n in names if importlib.util.find_spec(n.replace("-", "_")) is None]
    if need:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *need], check=True)


def drive_id(url):
    import re
    m = re.search(r"(?:/d/|[?&]id=)([\w-]{20,})", url)
    return m.group(1) if m else ""


def download_url(url, dest):
    """Скачивает файл по ссылке (Google Drive — через прямую ссылку без страницы-предупреждения)."""
    fid = drive_id(url) if "google.com" in url else ""
    if fid:
        url = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 hochu-plachu-video"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        if "text/html" in (r.headers.get("Content-Type") or ""):
            raise RuntimeError("по ссылке открылась страница, а не файл: проверьте доступ «Все, у кого есть ссылка»")
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    if Path(dest).stat().st_size < 10000:
        raise RuntimeError("скачался слишком маленький файл — ссылка не на видео")


VIDEO_SITES = ("twitch.tv", "youtube.com", "youtu.be", "vkvideo.ru", "vk.com", "vk.ru", "kick.com", "rutube.ru",
               "tiktok.com", "instagram.com")


def is_video_page(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    return any(host == s or host.endswith("." + s) for s in VIDEO_SITES)


def allowed_channel(info, allowed):
    """Канал ролика есть в белом списке SOURCE_CHANNELS (ссылка на канал, @ник или ник)."""
    import re
    ids = " ".join(str(info.get(k) or "") for k in
                   ("uploader_id", "uploader_url", "channel_id", "channel_url", "channel", "uploader")).lower()
    for a in allowed:
        token = str(a).strip().lower().rstrip("/").split("?")[0].split("/")[-1].lstrip("@")
        if len(token) >= 3 and re.search(r"(^|[/@\s])" + re.escape(token) + r"($|[/\s])", ids):
            return True
    return False


def ytdlp(url, dest, params, section=None):
    """Скачивает видео со страницы площадки через yt-dlp — только с каналов из белого списка."""
    import subprocess
    ensure_packages("yt-dlp")
    base = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings"]
    r = subprocess.run(base + ["-j", url], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("yt-dlp не открыл ссылку: " + (r.stderr.strip().splitlines() or ["?"])[-1][:300])
    info = json.loads(r.stdout.splitlines()[0])
    who = info.get("channel") or info.get("uploader") or info.get("uploader_id") or "?"
    if not allowed_channel(info, params.get("allowed") or []):
        raise RuntimeError(f"канал «{who}» не в белом списке SOURCE_CHANNELS: скачиваем только свои каналы "
                           "и каналы авторов, давших согласие")
    if info.get("is_live"):
        raise RuntimeError("эфир ещё идёт — дождитесь записи (VOD) и пришлите ссылку на неё")
    h = int(params.get("max_height") or 1080)
    cmd = base + ["-f", f"bv*[height<={h}]+ba/b[height<={h}]/b", "--merge-output-format", "mp4",
                  "--retries", "5", "--concurrent-fragments", "8", "-o", str(dest), "--force-overwrites"]
    if section:
        cmd += ["--download-sections", f"*{section[0]:.2f}-{section[1]:.2f}", "--force-keyframes-at-cuts"]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    if r.returncode != 0 or not Path(dest).exists():
        raise RuntimeError("yt-dlp не скачал видео: " + (r.stderr.strip().splitlines() or ["?"])[-1][:300])
    print(f"yt-dlp: {who} — {info.get('title', '')[:80]} ({info.get('duration')} с)")
    return info


def get_source(params, work):
    if params.get("source_url"):
        src = work / "source.mp4"
        url = params["source_url"]
        if is_video_page(url):
            section = None
            if params.get("mode") == "analyze":
                params.setdefault("max_height", 720)  # для поиска моментов хватает 720p, качается быстрее
            elif float(params.get("start", -1) or -1) >= 0:
                # для монтажа качаем только нужный кусок (+1 с с краёв), начало сдвигаем
                st, dur = float(params["start"]), float(params.get("duration") or 30)
                a = max(0.0, st - 1.0)
                section, params["start"] = (a, st + dur + 1.0), round(st - a, 2)
            ytdlp(url, src, params, section)
        else:
            download_url(url, src)
        return src
    assets = call("GET", f"{API}/repos/{REPO}/releases/{RID}/assets?per_page=100")
    src_asset = next((a for a in assets if a["name"].startswith("source")), None)
    if not src_asset:
        raise RuntimeError("в релизе нет файла source* и не задан source_url")
    src = work / src_asset["name"]
    src.write_bytes(call("GET", f"{API}/repos/{REPO}/releases/assets/{src_asset['id']}",
                         headers={"Accept": "application/octet-stream"}, raw=True))
    return src


def analyze(src, params, work):
    ensure_packages("numpy", "faster-whisper")
    import highlights  # noqa: PLC0415
    res = highlights.find_moments(src, work, k=int(params.get("k") or 5),
                                  min_dur=float(params.get("min_dur") or 7), max_dur=float(params.get("max_dur") or 20),
                                  target=float(params.get("duration") or 15),
                                  layout=str(params.get("layout") or "clip"))
    times = [m["start"] + min(2.0, m["duration"] / 2) for m in res["candidates"]]
    sheet = highlights.contact_sheet(src, work / "preview.jpg", times)
    if sheet:
        upload(sheet, "preview.jpg", "image/jpeg")
    probe = render_core.probe(src)
    res.update({"width": probe[2], "height": probe[3], "has_audio": probe[1],
                "orientation": "vertical" if probe[3] >= probe[2] * 1.2 else "horizontal"})
    return res


def main():
    work = Path("work")
    work.mkdir(exist_ok=True)
    result = {"ok": False}
    try:
        params = json.loads(os.environ.get("PARAMS") or "{}")
        src = get_source(params, work)
        if params.get("mode") == "analyze":
            result = {"ok": True, "id": params.get("id", ""), "mode": "analyze", **analyze(src, params, work)}
        else:
            if str(params.get("subs", "")).lower() in ("1", "true", "yes", "да"):
                ensure_packages("numpy", "faster-whisper")
            out = work / "out.mp4"
            meta = render_core.render_video(src, out, **params)
            upload(out, "out.mp4", "video/mp4")
            result = {"ok": True, "id": params.get("id", ""), **meta}
    except render_core.RenderError as e:
        result = {"ok": False, "error": e.detail, "code": e.code}
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:800]}
    (work / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    upload(work / "result.json", "result.json", "application/json")
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
