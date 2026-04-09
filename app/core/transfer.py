"""
Main transfer job: download from YouTube, upload to Rutube.
"""
import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from config import Config
from app.core.helpers import ytdlp_cmd, ytdlp_auth_args, rutube_auth_cookie_file, ffmpeg_available
from app.core.queue import jobs
from app.database import save_history
from app.services.telegram import _send_telegram_notify

DOWNLOAD_DIR = Config.DOWNLOAD_DIR


def _ws_broadcast(msg: dict) -> None:
    """Import ws_broadcast lazily to avoid circular imports."""
    try:
        from app import ws_broadcast
        ws_broadcast(msg)
    except Exception:
        pass


def _make_log(job: dict, job_id: str, user_id: int):
    def log(msg: str, level: str = "info"):
        entry = {"t": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
        job["log"].append(entry)
        _ws_broadcast({
            "type": "job_update", "job_id": job_id, "user_id": user_id,
            "log_entry": entry, "progress": job.get("progress", 0),
            "stage": job.get("stage", ""), "status": job.get("status", "running"),
            "mode": job.get("mode", "transfer")
        })
    return log


def _download_video(job, log, url, cfg, vid_id, include_thumb=True) -> Path:
    job["stage"] = "download"
    job["progress"] = 10
    log("Starting download...", "info")
    if not ffmpeg_available():
        raise Exception("ffmpeg not found. Install ffmpeg to continue.")
    quality_map = {
        "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio/best[height<=1080]",
        "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720]",
        "480p":  "bestvideo[height<=480][ext=mp4]+bestaudio/best[height<=480]",
    }
    fmt = quality_map.get(cfg.get("quality", "best"), quality_map["best"])
    out_tmpl = str(DOWNLOAD_DIR / f"{vid_id}.%(ext)s")
    dl_cmd = (ytdlp_cmd() + ["-f", fmt, "--merge-output-format", "mp4",
                              "--no-playlist", "--no-warnings", "--newline"] +
              (["--write-thumbnail", "--convert-thumbnails", "jpg"] if include_thumb else []) +
              ytdlp_auth_args(cfg) + ["-o", out_tmpl, url])
    if cfg.get("proxy"):
        dl_cmd += ["--proxy", cfg["proxy"]]
    proc = subprocess.Popen(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        m = re.search(r"(\d+\.?\d*)%", line.strip())
        if m:
            job["progress"] = 10 + int(float(m.group(1)) * 0.6)
        if "[download]" in line or "[ffmpeg]" in line:
            log(line.strip(), "info")
    proc.wait()
    if proc.returncode != 0:
        raise Exception("yt-dlp download failed")
    video_path = DOWNLOAD_DIR / f"{vid_id}.mp4"
    if not video_path.exists():
        for f in DOWNLOAD_DIR.glob(f"{vid_id}.*"):
            if f.suffix in (".mp4", ".mkv", ".webm"):
                video_path = f
                break
    if not video_path.exists():
        raise Exception("Video file not found after download")
    log(f"Downloaded: {video_path.name} ({video_path.stat().st_size / 1024 / 1024:.1f} MB)", "ok")
    return video_path


def run_transfer(job_id: str, url: str, cfg: dict, user_id: int) -> None:
    job = jobs[job_id]
    job["status"] = "running"
    job["log"] = []
    log = _make_log(job, job_id, user_id)
    try:
        # 1. Metadata
        job["stage"] = "metadata"
        job["progress"] = 5
        log("Fetching video metadata...")
        meta_cmd = ytdlp_cmd() + ["--dump-json", "--no-playlist", "--no-warnings"] + ytdlp_auth_args(cfg) + [url]
        result = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise Exception("yt-dlp metadata failed: " + result.stderr[:300])
        meta = json.loads(result.stdout)
        job["meta"] = {
            "title":       meta.get("title", "Untitled")[:80],
            "channel":     meta.get("uploader", ""),
            "duration":    meta.get("duration", 0),
            "view_count":  meta.get("view_count", 0),
            "like_count":  meta.get("like_count", 0),
            "tags":        meta.get("tags", [])[:20],
            "description": meta.get("description", "")[:4500],
            "thumbnail":   meta.get("thumbnail", ""),
            "upload_date": meta.get("upload_date", ""),
        }
        vid_id = meta.get("id", "video")
        log(f"Title: {job['meta']['title']}", "ok")
        log(f"Channel: {job['meta']['channel']}", "ok")
        log(f"Tags: {len(job['meta']['tags'])} tags found", "ok")

        # 2. Download
        video_path = _download_video(job, log, url, cfg, vid_id, include_thumb=True)
        thumb_path = DOWNLOAD_DIR / f"{vid_id}.jpg"
        size_mb = video_path.stat().st_size / 1024 / 1024
        job["progress"] = 70

        # 3. Rutube upload via Playwright
        log("Connecting to Rutube...", "info")
        job["stage"] = "upload"
        headers_base = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer":    "https://rutube.ru/",
            "Origin":     "https://rutube.ru",
        }
        access_token = ""
        rutube_cookie = rutube_auth_cookie_file(cfg)
        if rutube_cookie:
            log("Rutube: cookie file auth", "ok")
            try:
                for line in Path(rutube_cookie).read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.strip().split("\t")
                    if len(parts) >= 7 and parts[5] == "jwt":
                        access_token = parts[6].strip()
                        log("JWT extracted from cookies", "ok")
                        break
            except Exception as e:
                log(f"Cookie parse: {e}", "warn")
        elif cfg.get("rutube_user") and cfg.get("rutube_pass"):
            log("Authenticating with login/password...", "info")
            auth_data = json.dumps({"username": cfg["rutube_user"], "password": cfg["rutube_pass"]}).encode()
            req = urllib.request.Request(
                "https://rutube.ru/api/accounts/token/", data=auth_data,
                headers={**headers_base, "Content-Type": "application/json"}, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read())
                    access_token = resp.get("access_token", resp.get("token", ""))
                    if access_token:
                        log("Authentication OK", "ok")
            except Exception as e:
                log(f"Auth: {e}", "warn")
        else:
            log("No credentials — uploading as anonymous", "warn")

        job["progress"] = 75
        rutube_video_id = None

        try:
            from playwright.sync_api import sync_playwright
            _pw_ok = True
        except ImportError:
            _pw_ok = False
            log("Playwright not installed. Run: pip install playwright && playwright install chromium", "warn")

        if _pw_ok:
            try:
                cookie_file = cfg.get("rutube_cookie_file", "").strip()
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                    )
                    ctx = browser.new_context(
                        viewport={"width": 1280, "height": 900},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                                   " AppleWebKit/537.36 (KHTML, like Gecko)"
                                   " Chrome/125.0.0.0 Safari/537.36",
                    )
                    captured_ids = []
                    up = {"total": 0, "done": 0, "chunks": 0, "last": time.time(), "finished": False}

                    def on_response(response):
                        try:
                            s, m = response.status, response.request.method
                            if "rutube.ru" in response.url and m in ("POST", "PUT", "PATCH") and s in (200, 201):
                                ct = response.headers.get("content-type", "")
                                if "json" in ct:
                                    body = response.text()
                                    for pat in [r'"video_id"\s*:\s*"([a-f0-9]{32})"',
                                                r'"id"\s*:\s*"([a-f0-9]{32})"']:
                                        m2 = re.search(pat, body)
                                        if m2 and m2.group(1) not in captured_ids:
                                            captured_ids.append(m2.group(1))
                            if s in (201,) and m == "POST":
                                loc = response.headers.get("location", "")
                                if loc:
                                    m2 = re.search(r"/([a-f0-9]{32})(?:/|$|\?)", loc)
                                    if m2 and m2.group(1) not in captured_ids:
                                        captured_ids.append(m2.group(1))
                            if s in (200, 201, 204, 308):
                                off_h = response.headers.get("upload-offset", "")
                                len_h = response.headers.get("upload-length", "")
                                if off_h or len_h or m == "PATCH":
                                    up["last"] = time.time()
                                if off_h:
                                    up["done"] = max(up["done"], int(off_h))
                                    up["chunks"] += 1
                                if len_h:
                                    up["total"] = max(up["total"], int(len_h))
                                if up["total"] > 0 and up["done"] >= up["total"]:
                                    up["finished"] = True
                        except Exception:
                            pass

                    if cookie_file and Path(cookie_file).exists():
                        cookies = []
                        for line in Path(cookie_file).read_text(encoding="utf-8", errors="ignore").splitlines():
                            if line.startswith("#") or not line.strip():
                                continue
                            parts = line.strip().split("\t")
                            if len(parts) < 7:
                                continue
                            dom, _, path, secure, _, name, value = parts[:7]
                            cookies.append({
                                "name": name, "value": value,
                                "domain": dom if dom.startswith(".") else dom.lstrip("."),
                                "path": path, "secure": secure.upper() == "TRUE", "sameSite": "None"
                            })
                        ctx.add_cookies(cookies)
                        log(f"Loaded {len(cookies)} cookies", "ok")

                    page = ctx.new_page()
                    page.on("response", on_response)
                    page.goto("https://studio.rutube.ru/uploader/", timeout=30000)
                    page.wait_for_load_state("networkidle", timeout=20000)
                    page.wait_for_timeout(2000)

                    # Attach video
                    fi = page.query_selector('input[type="file"]')
                    if not fi:
                        for sel in ['button:has-text("Загрузить")', 'button:has-text("Добавить")']:
                            try:
                                b = page.query_selector(sel)
                                if b and b.is_visible():
                                    b.click()
                                    page.wait_for_timeout(2000)
                                    break
                            except Exception:
                                pass
                        fi = page.query_selector('input[type="file"]')
                    if fi:
                        fi.set_input_files(str(video_path.resolve()))
                        log("Video attached to uploader", "ok")
                    else:
                        log("File input not found — check Rutube Studio layout", "warn")

                    page.wait_for_timeout(6000)

                    # Fill title
                    title = job["meta"]["title"][:80]
                    tags = job["meta"].get("tags", [])
                    desc = job["meta"].get("description", "")[:4500]
                    if tags:
                        desc += "\n\n" + " ".join(f"#{t.replace(' ', '_')}" for t in tags)
                    filled = False
                    for sel in [
                        'input[name="title"]', 'input[placeholder*="азван"]',
                        'input[placeholder*="itle"]', 'input[aria-label*="азван"]',
                        '[class*="upload"] input[type="text"]',
                        '[class*="modal"] input[type="text"]',
                    ]:
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                el.click()
                                el.fill("")
                                el.type(title, delay=10)
                                log(f"Title filled: {title[:50]}", "ok")
                                filled = True
                                break
                        except Exception:
                            pass
                    if not filled:
                        for inp in page.query_selector_all('input[type="text"],input:not([type])'):
                            if inp.is_visible():
                                inp.click()
                                inp.fill("")
                                inp.type(title, delay=10)
                                log("Title filled (fallback)", "ok")
                                break

                    # Fill description
                    if desc:
                        for sel in ['textarea[name="description"]',
                                    'textarea[placeholder*="писан"]',
                                    'textarea[placeholder*="escription"]',
                                    '[class*="upload"] textarea', '[class*="modal"] textarea']:
                            try:
                                el = page.query_selector(sel)
                                if el and el.is_visible():
                                    el.click()
                                    el.fill(desc)
                                    log(f"Description filled ({len(desc)} chars)", "ok")
                                    break
                            except Exception:
                                pass

                    # Attach thumbnail
                    if thumb_path.exists():
                        try:
                            for fi2 in page.query_selector_all('input[type="file"]'):
                                acc = fi2.get_attribute("accept") or ""
                                if "image" in acc:
                                    fi2.set_input_files(str(thumb_path.resolve()))
                                    log("Thumbnail attached", "ok")
                                    break
                        except Exception:
                            pass

                    # Category
                    page.wait_for_timeout(1000)
                    for cat in ["Развлечения", "Юмор", "Блоги", "Люди и блоги", "Другое"]:
                        try:
                            opt = page.query_selector(f'text="{cat}"')
                            if opt and opt.is_visible():
                                opt.click()
                                log(f"Category: {cat}", "ok")
                                break
                        except Exception:
                            pass

                    # Wait for upload
                    page.wait_for_timeout(5000)
                    max_wait = max(300, int(size_mb * 2.0))
                    log(f"Uploading {size_mb:.0f} MB (timeout {max_wait}s)...", "info")
                    start_w = time.time()
                    last_pct = -1
                    while time.time() - start_w < max_wait:
                        page.wait_for_timeout(3000)
                        elapsed = int(time.time() - start_w)
                        if up["total"] > 0:
                            pct = int(up["done"] / up["total"] * 100)
                        elif up["chunks"] > 0:
                            pct = min(95, int(up["chunks"] / max(1, size_mb / 5) * 100))
                        else:
                            pct = 0
                        job["progress"] = 75 + int(min(pct, 100) * 0.17)
                        if pct >= last_pct + 10:
                            if up["total"] > 0:
                                log(f"Upload: {up['done'] // 1024 // 1024}/{up['total'] // 1024 // 1024} MB ({pct}%) — {elapsed}s", "info")
                            else:
                                log(f"Upload: {up['chunks']} chunks — {elapsed}s", "info")
                            last_pct = pct
                        if up["finished"]:
                            log(f"File transfer complete ({elapsed}s)", "ok")
                            break
                        idle = time.time() - up["last"]
                        if idle > 120 and elapsed > 60:
                            if up["chunks"] > 5:
                                log(f"No activity {idle:.0f}s — upload assumed finished", "warn")
                                up["finished"] = True
                                break
                            elif up["chunks"] == 0 and elapsed > 90:
                                log("Upload did not start — check auth cookies", "warn")
                                break

                    if up["finished"]:
                        page.wait_for_timeout(8000)

                    # Publish
                    for sel in ['button:has-text("Опубликовать")', 'button:has-text("Publish")']:
                        try:
                            b = page.query_selector(sel)
                            if b and b.is_visible():
                                b.click()
                                log("Clicked Publish", "ok")
                                page.wait_for_timeout(5000)
                                break
                        except Exception:
                            pass

                    # Extract video ID
                    cur_url = page.url
                    for pat in [r'/video/([a-f0-9]{32})', r'([a-f0-9]{32})']:
                        m2 = re.search(pat, cur_url)
                        if m2:
                            rutube_video_id = m2.group(1)
                            log(f"Video ID (URL): {rutube_video_id}", "ok")
                            break
                    if not rutube_video_id:
                        try:
                            for pat in [r'video/([a-f0-9]{32})', r'"video_id"\s*:\s*"([a-f0-9]{32})"']:
                                m2 = re.search(pat, page.content())
                                if m2:
                                    rutube_video_id = m2.group(1)
                                    log(f"Video ID (page): {rutube_video_id}", "ok")
                                    break
                        except Exception:
                            pass
                    if not rutube_video_id and captured_ids:
                        rutube_video_id = captured_ids[-1]
                        log(f"Video ID (network): {rutube_video_id}", "info")
                    if not rutube_video_id:
                        log("Could not get video ID — check https://studio.rutube.ru manually", "warn")
                    browser.close()
            except Exception as e:
                log(f"Browser upload error: {e}", "warn")

        job["progress"] = 92

        # API thumbnail upload
        if thumb_path.exists() and rutube_video_id and access_token:
            log("Uploading thumbnail via API...", "info")
            subprocess.run([
                "curl", "-s", "-X", "POST",
                f"https://rutube.ru/api/video/{rutube_video_id}/thumbnail/",
                "-F", f"file=@{thumb_path}",
                "-H", f"User-Agent: {headers_base['User-Agent']}",
                "-H", f"Authorization: Bearer {access_token}",
            ], capture_output=True, timeout=60)
            log("Thumbnail uploaded", "ok")

        if not cfg.get("keep_files", True):
            video_path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)

        rutube_url = (f"https://rutube.ru/video/{rutube_video_id}/"
                      if rutube_video_id else "https://rutube.ru")
        job["rutube_url"] = rutube_url
        job["progress"] = 100
        job["status"] = "done"
        log(f"Done! {rutube_url}", "ok")
        _ws_broadcast({"type": "job_done", "job_id": job_id, "user_id": user_id})
        save_history(user_id, {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url": url, "title": job["meta"]["title"],
            "rutube_url": rutube_url, "status": "success", "mode": "transfer"
        })
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        log(f"Error: {e}", "error")
        _ws_broadcast({"type": "job_error", "job_id": job_id, "user_id": user_id, "error": str(e)})
        save_history(user_id, {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url": url, "title": "", "rutube_url": "",
            "status": f"error: {e}", "mode": "transfer"
        })
    finally:
        _send_telegram_notify(cfg, job, url)
