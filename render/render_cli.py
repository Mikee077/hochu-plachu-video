"""Монтаж одного ролика из командной строки (для GitHub Actions).

python3 render_cli.py --src source.mp4 --out out.mp4 --params '{"hook": "...", "overlay": "logo"}' --result result.json
"""
import argparse, json, sys
import render_core

ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--params", default="{}")
ap.add_argument("--result", default="result.json")
a = ap.parse_args()
try:
    params = json.loads(a.params or "{}")
    meta = render_core.render_video(a.src, a.out, **params)
    meta["ok"] = True
    meta["id"] = params.get("id", "")
except render_core.RenderError as e:
    meta = {"ok": False, "error": e.detail, "code": e.code}
except Exception as e:  # noqa: BLE001
    meta = {"ok": False, "error": f"{type(e).__name__}: {e}"}
open(a.result, "w", encoding="utf-8").write(json.dumps(meta, ensure_ascii=False))
print(json.dumps(meta, ensure_ascii=False))
sys.exit(0 if meta.get("ok") else 1)
