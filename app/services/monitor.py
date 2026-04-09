"""
Channel monitor: watches YouTube channels via RSS, auto-enqueues new videos.
"""
import json
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from config import Config
from app.core.helpers import ytdlp_cmd
from app.services.telegram import tg_send

MONITOR_STATE_FILE = Config.MONITOR_STATE_FILE
_monitor_started = False


def extract_channel_id(url_or_id: str) -> str:
    url_or_id = url_or_id.strip()
    if re.match(r"^UC[\w-]{22}$", url_or_id):
        return url_or_id
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", url_or_id)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/@([\w.-]+)", url_or_id)
    if m:
        try:
            cmd = ytdlp_cmd() + ["--print", "channel_id", "--playlist-items", "1", "--no-warnings",
                                  f"https://www.youtube.com/@{m.group(1)}/videos"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            cid = result.stdout.strip()
            if cid.startswith("UC"):
                return cid
        except Exception:
            pass
    return url_or_id


def fetch_rss_videos(channel_id: str) -> list:
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0 (compatible; YT2RT)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            content = r.read().decode("utf-8", errors="ignore")
        entries = []
        for m in re.finditer(r"<entry>(.*?)</entry>", content, re.DOTALL):
            xml = m.group(1)
            vid = re.search(r"<yt:videoId>([\w-]+)</yt:videoId>", xml)
            tit = re.search(r"<title>(.*?)</title>", xml)
            pub = re.search(r"<published>(.*?)</published>", xml)
            if vid:
                entries.append({
                    "video_id":  vid.group(1),
                    "title":     tit.group(1) if tit else "Untitled",
                    "published": pub.group(1) if pub else "",
                    "url":       f"https://www.youtube.com/watch?v={vid.group(1)}",
                })
        return entries
    except Exception as e:
        print(f"[monitor] RSS error ({channel_id}): {e}")
        return []


def _monitor_loop():
    while True:
        try:
            from app.database import get_db, load_user_config
            from app.core.queue import enqueue_job
            with get_db() as db:
                admin = db.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
                admin_id = admin["id"] if admin else 1
            cfg = load_user_config(admin_id)
            if not cfg.get("monitor_enabled"):
                time.sleep(30)
                continue
            channels = cfg.get("monitor_channels", [])
            if not channels:
                time.sleep(30)
                continue
            interval = max(5, cfg.get("monitor_interval", 15)) * 60
            state = json.loads(MONITOR_STATE_FILE.read_text()) if MONITOR_STATE_FILE.exists() else {}
            for ch in channels:
                ch_url = ch if isinstance(ch, str) else ch.get("url", "")
                channel_id = extract_channel_id(ch_url)
                videos = fetch_rss_videos(channel_id)
                known = set(state.get(channel_id, []))
                for vid in videos:
                    if vid["video_id"] not in known:
                        print(f"[monitor] New video: {vid['title']}")
                        if cfg.get("telegram_enabled") and cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"):
                            tg_send(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                                    f"New video!\n{vid['title']}\n{vid['url']}\nStarting transfer...")
                        enqueue_job(vid["url"], cfg, admin_id)
                        known.add(vid["video_id"])
                state[channel_id] = list(known)[-200:]
            MONITOR_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            time.sleep(interval)
        except Exception as e:
            print(f"[monitor] Error: {e}")
            time.sleep(60)


def start_monitor():
    global _monitor_started
    if not _monitor_started:
        _monitor_started = True
        threading.Thread(target=_monitor_loop, daemon=True).start()
