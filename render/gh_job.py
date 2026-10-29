"""Задание монтажа в GitHub Actions.

n8n кладёт исходник в черновой релиз репозитория (его видят только владельцы),
запускает сценарий render.yml с номером релиза и параметрами, а потом забирает из того же релиза
out.mp4 и result.json. Черновые релизы не публикуются и не создают теги.

Переменные окружения: GITHUB_TOKEN, REPO (owner/name), RELEASE_ID, PARAMS (JSON).
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


def main():
    work = Path("work")
    work.mkdir(exist_ok=True)
    result = {"ok": False}
    try:
        params = json.loads(os.environ.get("PARAMS") or "{}")
        assets = call("GET", f"{API}/repos/{REPO}/releases/{RID}/assets?per_page=100")
        src_asset = next((a for a in assets if a["name"].startswith("source")), None)
        if not src_asset:
            raise RuntimeError("в релизе нет файла source*")
        src = work / src_asset["name"]
        src.write_bytes(call("GET", f"{API}/repos/{REPO}/releases/assets/{src_asset['id']}",
                             headers={"Accept": "application/octet-stream"}, raw=True))
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
