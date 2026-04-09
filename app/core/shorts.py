"""
Shorts generator: auto-cut vertical clips from YouTube videos.
"""
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import Config
from app.core.helpers import ytdlp_cmd, ytdlp_auth_args
from app.core.queue import jobs
from app.database import save_history
from app.services.telegram import _send_telegram_notify

SHORTS_DIR = Config.SHORTS_DIR


def _ws_broadcast(msg: dict) -> None:
    try:
        from app import ws_broadcast
        ws_broadcast(msg)
    except Exception:
        pass


def _make_log(job: dict, job_id: str, user_id: int):
    def log(msg: str, level: str = "info"):
        from datetime import datetime
        entry = {"t": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
        job["log"].append(entry)
        _ws_broadcast({
            "type": "job_update", "job_id": job_id, "user_id": user_id,
            "log_entry": entry, "progress": job.get("progress", 0),
            "stage": job.get("stage", ""), "status": job.get("status", "running"),
            "mode": job.get("mode", "shorts")
        })
    return log


def generate_shorts(video_path: Path, cfg: dict, log_fn) -> list:
    count = min(int(cfg.get("shorts_count", 3)), 10)
    duration = min(int(cfg.get("shorts_duration", 60)), 90)
    strategy = cfg.get("shorts_strategy", "energy")
    vid_id = video_path.stem
    log_fn(f"Generating {count} shorts ({duration}s, strategy={strategy})", "info")

    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True, timeout=30
    )
    total_dur = float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0
    if total_dur < duration + 10:
        log_fn(f"Video too short ({total_dur:.0f}s)", "warn")
        return []

    timestamps = []
    if strategy == "energy":
        log_fn("Analyzing audio peaks...", "info")
        r = subprocess.run(
            ["ffmpeg", "-i", str(video_path), "-af",
             "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120
        )
        energy = []
        for line in (r.stdout + r.stderr).split("\n"):
            if "RMS_level" in line:
                try:
                    energy.append(float(line.split("=")[-1].strip()))
                except Exception:
                    pass
        if len(energy) > 10:
            import statistics
            avg = statistics.mean(energy)
            std = statistics.stdev(energy) if len(energy) > 1 else 1
            candidates = sorted(
                [(i, e) for i, e in enumerate(energy)
                 if e > avg + 0.3 * std and 5 < i < len(energy) - duration],
                key=lambda x: x[1], reverse=True
            )
            for ts, _ in candidates:
                if all(abs(ts - t) > duration + 5 for t in timestamps):
                    timestamps.append(ts)
                if len(timestamps) >= count:
                    break
        if not timestamps:
            strategy = "uniform"

    if strategy == "scene":
        log_fn("Scene detection...", "info")
        r = subprocess.run(
            ["ffmpeg", "-i", str(video_path), "-vf",
             "select='gt(scene,0.3)',showinfo", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120
        )
        scenes = []
        for line in r.stderr.split("\n"):
            if "pts_time:" in line:
                try:
                    t = float(re.search(r"pts_time:([\d.]+)", line).group(1))
                    if 5 < t < total_dur - duration:
                        scenes.append(t)
                except Exception:
                    pass
        if scenes:
            step = max(1, len(scenes) // count)
            timestamps = [int(scenes[i * step]) for i in range(min(count, len(scenes)))]
        else:
            strategy = "uniform"

    if strategy == "uniform" or not timestamps:
        gap = (total_dur - duration) / (count + 1)
        timestamps = [int(gap * (i + 1)) for i in range(count)]

    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, timeout=15
    )
    try:
        w, h = map(int, r.stdout.strip().split(","))
    except Exception:
        w, h = 1920, 1080

    if w / h > 9 / 16:
        new_w = int(h * 9 / 16)
        crop = f"crop={new_w}:{h}:({w}-{new_w})/2:0,scale=1080:1920"
    else:
        crop = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:-1:-1:color=black"

    shorts_list = []
    for i, start in enumerate(timestamps[:count]):
        out = SHORTS_DIR / f"{vid_id}_short{i + 1}.mp4"
        log_fn(f"Cutting #{i + 1} at {start}s...", "info")
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-i", str(video_path),
             "-t", str(duration), "-vf", crop,
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
            capture_output=True, timeout=180
        )
        if r.returncode == 0 and out.exists():
            mb = out.stat().st_size / 1024 / 1024
            shorts_list.append({
                "file": str(out), "filename": out.name,
                "start": start, "duration": duration,
                "size_mb": round(mb, 1), "index": i + 1
            })
            log_fn(f"Short #{i + 1}: {out.name} ({mb:.1f} MB)", "ok")
        else:
            log_fn(f"Short #{i + 1} failed", "warn")
    return shorts_list


def run_shorts_transfer(job_id: str, url: str, cfg: dict, user_id: int) -> None:
    from app.core.transfer import _download_video
    job = jobs[job_id]
    job["status"] = "running"
    job["log"] = []
    job["shorts"] = []
    job["mode"] = "shorts"
    log = _make_log(job, job_id, user_id)
    try:
        job["stage"] = "metadata"
        job["progress"] = 5
        log("Fetching metadata...")
        meta_cmd = ytdlp_cmd() + ["--dump-json", "--no-playlist", "--no-warnings"] + ytdlp_auth_args(cfg) + [url]
        result = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise Exception("yt-dlp metadata failed")
        meta = json.loads(result.stdout)
        job["meta"] = {
            "title":    meta.get("title", "")[:80],
            "channel":  meta.get("uploader", ""),
            "duration": meta.get("duration", 0),
            "tags":     meta.get("tags", [])[:20],
        }
        vid_id = meta.get("id", "video")
        log(f"Video: {job['meta']['title']} ({meta.get('duration', 0)}s)", "ok")

        video_path = _download_video(job, log, url, cfg, vid_id, include_thumb=False)
        job["progress"] = 42
        job["stage"] = "cutting"
        log("Cutting shorts...")
        shorts = generate_shorts(video_path, cfg, log)
        job["shorts"] = shorts
        log(f"Created {len(shorts)} shorts", "ok")
        job["progress"] = 100
        job["status"] = "done"
        log("Done! Shorts ready for download.", "ok")
        _ws_broadcast({"type": "job_done", "job_id": job_id, "user_id": user_id,
                       "mode": "shorts", "shorts_count": len(shorts)})
        save_history(user_id, {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url": url, "title": "[SHORTS] " + job["meta"]["title"],
            "rutube_url": f"{len(shorts)} shorts", "status": "success", "mode": "shorts"
        })
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        log(f"Error: {e}", "error")
        _ws_broadcast({"type": "job_error", "job_id": job_id, "user_id": user_id, "error": str(e)})
        save_history(user_id, {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url": url, "title": "", "rutube_url": "",
            "status": f"error: {e}", "mode": "shorts"
        })
    finally:
        _send_telegram_notify(cfg, job, url)
