#!/usr/bin/env python3
"""
YT → Rutube Transfer Platform v5.0
Multi-user web app with authentication and admin panel.
Run: python app.py  →  http://localhost:5000
Default admin: admin / admin123  (change after first login!)
"""

import os, json, subprocess, threading, time, re, sys, uuid, sqlite3, secrets
import urllib.request, urllib.error, urllib.parse
from pathlib import Path
from datetime import datetime
from collections import deque
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (Flask, jsonify, request, send_from_directory,
                   render_template_string, session, redirect, url_for)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 30  # 30 days

# ── Directories ───────────────────────────────────────────
DOWNLOAD_DIR = Path("downloads")
SHORTS_DIR   = Path("shorts")
DUBBED_DIR   = Path("dubbed")
for _d in [DOWNLOAD_DIR, SHORTS_DIR, DUBBED_DIR]:
    _d.mkdir(exist_ok=True)

DB_PATH            = Path("platform.db")
MONITOR_STATE_FILE = Path("monitor_state.json")

# ── WebSocket ──────────────────────────────────────────────
try:
    from flask_sock import Sock
    sock = Sock(app)
    _ws_available = True
except ImportError:
    sock = None
    _ws_available = False

ws_clients: set = set()
ws_lock = threading.Lock()

def ws_broadcast(msg: dict) -> None:
    data = json.dumps(msg, default=str)
    dead: set = set()
    with ws_lock:
        for ws in ws_clients:
            try:
                ws.send(data)
            except Exception:
                dead.add(ws)
        ws_clients.difference_update(dead)

# ══════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def default_config() -> dict:
    return {
        "yt_use_cookies": False,
        "yt_cookies_from_browser": "chrome",
        "yt_cookie_file": "yt_cookies.txt",
        "rutube_user": "",
        "rutube_pass": "",
        "rutube_use_cookies": False,
        "rutube_cookie_file": "rutube_cookies.txt",
        "quality": "best",
        "keep_files": True,
        "proxy": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "telegram_enabled": False,
        "monitor_channels": [],
        "monitor_interval": 15,
        "monitor_enabled": False,
        "dub_source_lang": "en",
        "dub_target_lang": "ru",
        "dub_mix_original": 0.15,
        "dub_whisper_model": "base",
        "shorts_count": 3,
        "shorts_duration": 60,
        "shorts_strategy": "energy",
    }

def init_db() -> None:
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user',
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_login    TEXT
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id     INTEGER PRIMARY KEY,
                config_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS job_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                ts          TEXT NOT NULL,
                youtube_url TEXT DEFAULT '',
                title       TEXT DEFAULT '',
                rutube_url  TEXT DEFAULT '',
                status      TEXT DEFAULT '',
                mode        TEXT DEFAULT 'transfer',
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            db.execute(
                "INSERT INTO users (username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                ("admin", "admin@localhost",
                 generate_password_hash("admin123"), "admin",
                 datetime.now().isoformat())
            )
            db.execute(
                "INSERT INTO user_settings (user_id,config_json) VALUES(1,?)",
                (json.dumps(default_config()),)
            )
            db.commit()
            print("  [init] Admin created: admin / admin123  ← change this!")

init_db()

# ══════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════
def get_user(uid: int) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

def get_user_by_username(username: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

def get_user_by_email(email: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None

def current_user() -> dict | None:
    uid = session.get("user_id")
    return get_user(uid) if uid else None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        user = get_user(uid)
        if not user or user["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Forbidden"}), 403
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

# ── Per-user config ────────────────────────────────────────
def load_user_config(user_id: int) -> dict:
    defaults = default_config()
    with get_db() as db:
        row = db.execute(
            "SELECT config_json FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        if row:
            try:
                saved = json.loads(row["config_json"])
                merged = dict(defaults)
                for k in defaults:
                    if k in saved:
                        merged[k] = saved[k]
                return merged
            except Exception:
                pass
    return defaults

def save_user_config(user_id: int, cfg: dict) -> None:
    with get_db() as db:
        exists = db.execute(
            "SELECT 1 FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        if exists:
            db.execute(
                "UPDATE user_settings SET config_json=? WHERE user_id=?",
                (json.dumps(cfg, ensure_ascii=False), user_id)
            )
        else:
            db.execute(
                "INSERT INTO user_settings (user_id,config_json) VALUES(?,?)",
                (user_id, json.dumps(cfg, ensure_ascii=False))
            )
        db.commit()

# ── Per-user history ───────────────────────────────────────
def save_history(user_id: int, entry: dict) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO job_history (user_id,ts,youtube_url,title,rutube_url,status,mode)"
            " VALUES(?,?,?,?,?,?,?)",
            (user_id, entry.get("ts",""), entry.get("youtube_url",""),
             entry.get("title",""), entry.get("rutube_url",""),
             entry.get("status",""), entry.get("mode","transfer"))
        )
        db.commit()

def load_history(user_id: int, limit: int = 100) -> list:
    with get_db() as db:
        rows = db.execute(
            "SELECT ts,youtube_url,title,rutube_url,status,mode"
            " FROM job_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

# ══════════════════════════════════════════════════════════
# JOB QUEUE
# ══════════════════════════════════════════════════════════
jobs: dict = {}
MAX_CONCURRENT = 2
active_count   = 0
active_lock    = threading.Lock()
job_queue: deque = deque()

def safe_filename(name: str, default: str = "file.txt") -> str:
    name = Path(name.strip()).name
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return default
    return name

def _queue_worker():
    global active_count
    while True:
        time.sleep(0.3)
        with active_lock:
            if active_count >= MAX_CONCURRENT or not job_queue:
                continue
            job_id, url, cfg, user_id = job_queue.popleft()
            active_count += 1
            jobs[job_id]["status"] = "starting"
        t = threading.Thread(
            target=_run_and_release, args=(job_id, url, cfg, user_id), daemon=True
        )
        t.start()

def _run_and_release(job_id, url, cfg, user_id):
    global active_count
    try:
        run_transfer(job_id, url, cfg, user_id)
    finally:
        with active_lock:
            active_count -= 1

threading.Thread(target=_queue_worker, daemon=True).start()

def enqueue_job(url: str, cfg: dict, user_id: int) -> str:
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "user_id": user_id, "status": "queued",
        "progress": 0, "log": [], "meta": {}, "stage": "queued"
    }
    job_queue.append((job_id, url, cfg, user_id))
    return job_id

def enqueue_dub_job(url: str, cfg: dict, user_id: int) -> str:
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "user_id": user_id, "status": "queued", "progress": 0,
        "log": [], "meta": {}, "stage": "queued", "mode": "dub"
    }
    threading.Thread(
        target=run_dub_transfer, args=(job_id, url, cfg, user_id), daemon=True
    ).start()
    return job_id

def enqueue_shorts_job(url: str, cfg: dict, user_id: int) -> str:
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "user_id": user_id, "status": "queued", "progress": 0,
        "log": [], "meta": {}, "stage": "queued", "mode": "shorts", "shorts": []
    }
    threading.Thread(
        target=run_shorts_transfer, args=(job_id, url, cfg, user_id), daemon=True
    ).start()
    return job_id

# ── URL validation ─────────────────────────────────────────
YT_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/(watch\?.*v=|shorts/|live/|embed/)"
    r"|youtu\.be/|m\.youtube\.com/)"
)

def is_valid_yt_url(url: str) -> bool:
    return bool(YT_URL_RE.match(url))

# ── System helpers ─────────────────────────────────────────
def ytdlp_available() -> bool:
    for cmd in [["yt-dlp","--version"], [sys.executable,"-m","yt_dlp","--version"]]:
        try:
            if subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    return False

def ytdlp_cmd() -> list:
    try:
        if subprocess.run(["yt-dlp","--version"], capture_output=True, timeout=5).returncode == 0:
            return ["yt-dlp"]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return [sys.executable, "-m", "yt_dlp"]

def ffmpeg_available() -> bool:
    try:
        return subprocess.run(["ffmpeg","-version"], capture_output=True, timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False

def ytdlp_auth_args(cfg: dict) -> list:
    if not cfg.get("yt_use_cookies"):
        return []
    cookie_file = cfg.get("yt_cookie_file", "").strip()
    if cookie_file and Path(cookie_file).exists():
        try:
            first = Path(cookie_file).read_text(encoding="utf-8", errors="ignore")[:50].strip()
            if not (first.startswith("[") or first.startswith("{")):
                return ["--cookies", cookie_file]
        except OSError:
            pass
    browser = cfg.get("yt_cookies_from_browser", "").strip()
    return ["--cookies-from-browser", browser] if browser else []

def rutube_auth_cookie_file(cfg: dict) -> str | None:
    if cfg.get("rutube_use_cookies"):
        f = cfg.get("rutube_cookie_file", "").strip()
        if f and Path(f).exists():
            return f
    return None


# ══════════════════════════════════════════════════════════
# AI DUBBING ENGINE
# ══════════════════════════════════════════════════════════
def check_whisper() -> bool:
    try:
        import whisper; return True
    except ImportError:
        return False

def check_edge_tts() -> bool:
    try:
        return subprocess.run(
            [sys.executable,"-m","edge_tts","--list-voices"],
            capture_output=True, timeout=10
        ).returncode == 0
    except Exception:
        return False

def run_ai_dubbing(video_path: Path, cfg: dict, log_fn) -> Path | None:
    source_lang  = cfg.get("dub_source_lang", "en")
    target_lang  = cfg.get("dub_target_lang", "ru")
    mix_ratio    = float(cfg.get("dub_mix_original", 0.15))
    whisper_model = cfg.get("dub_whisper_model", "base")
    vid_id       = video_path.stem
    log_fn(f"Dubbing: {source_lang} → {target_lang}, model={whisper_model}", "info")

    audio_path = DUBBED_DIR / f"{vid_id}_audio.wav"
    log_fn("Extracting audio...", "info")
    r = subprocess.run(
        ["ffmpeg","-y","-i",str(video_path),"-vn","-acodec","pcm_s16le",
         "-ar","16000","-ac","1",str(audio_path)],
        capture_output=True, timeout=300
    )
    if r.returncode != 0 or not audio_path.exists():
        raise Exception("Audio extraction failed")
    log_fn("Audio extracted", "ok")

    log_fn(f"Transcribing with Whisper ({whisper_model})...", "info")
    try:
        import whisper as wh
    except ImportError:
        log_fn("Installing openai-whisper...", "info")
        subprocess.run(
            [sys.executable,"-m","pip","install","openai-whisper",
             "--break-system-packages","-q"], timeout=600
        )
        import whisper as wh
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    log_fn(f"Device: {device.upper()}", "info")
    model  = wh.load_model(whisper_model, device=device)
    result = model.transcribe(str(audio_path), language=source_lang,
                               verbose=False, fp16=(device=="cuda"))
    segments = result.get("segments", [])
    log_fn(f"Transcribed {len(segments)} segments", "ok")
    if not segments:
        log_fn("No speech detected", "warn")
        return None

    log_fn("Translating...", "info")
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        subprocess.run(
            [sys.executable,"-m","pip","install","deep-translator",
             "--break-system-packages","-q"], timeout=120
        )
        from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source=source_lang, target=target_lang)
    translated = []
    for i in range(0, len(segments), 8):
        batch = segments[i:i+8]
        texts = [s["text"].strip() for s in batch if s["text"].strip()]
        if not texts:
            continue
        try:
            tr    = translator.translate(" ||| ".join(texts))
            parts = tr.split("|||")
            for j, seg in enumerate(batch):
                translated.append({
                    "start": seg["start"], "end": seg["end"],
                    "text": parts[j].strip() if j < len(parts) else seg["text"]
                })
        except Exception:
            for seg in batch:
                translated.append({"start": seg["start"], "end": seg["end"], "text": seg["text"]})
    log_fn(f"Translated {len(translated)} segments", "ok")

    log_fn("Generating voiceover...", "info")
    try:
        subprocess.run([sys.executable,"-m","edge_tts","--version"],
                       capture_output=True, timeout=10)
    except Exception:
        subprocess.run([sys.executable,"-m","pip","install","edge-tts",
                        "--break-system-packages","-q"], timeout=120)
    voice_map = {
        "ru":"ru-RU-DmitryNeural","en":"en-US-GuyNeural",
        "es":"es-ES-AlvaroNeural","de":"de-DE-ConradNeural",
        "fr":"fr-FR-HenriNeural","zh":"zh-CN-YunxiNeural",
        "ja":"ja-JP-KeitaNeural","ko":"ko-KR-InJoonNeural"
    }
    voice   = voice_map.get(target_lang, "ru-RU-DmitryNeural")
    tts_dir = DUBBED_DIR / f"{vid_id}_tts"
    tts_dir.mkdir(exist_ok=True)
    tts_segs = []
    for i, seg in enumerate(translated):
        out_f = tts_dir / f"seg{i:04d}.mp3"
        if not seg["text"].strip():
            continue
        try:
            subprocess.run(
                [sys.executable,"-m","edge_tts","--voice",voice,
                 "--text",seg["text"],"--write-media",str(out_f)],
                capture_output=True, timeout=30
            )
            if out_f.exists() and out_f.stat().st_size > 100:
                tts_segs.append({"file":str(out_f),"start":seg["start"]})
        except Exception:
            pass
        if i % 20 == 0 and i > 0:
            log_fn(f"TTS: {i}/{len(translated)}", "info")
    log_fn(f"Generated {len(tts_segs)} TTS clips", "ok")
    if not tts_segs:
        return None

    log_fn("Mixing audio...", "info")
    orig_aac = DUBBED_DIR / f"{vid_id}_orig.aac"
    subprocess.run(
        ["ffmpeg","-y","-i",str(video_path),"-vn","-acodec","aac",str(orig_aac)],
        capture_output=True, timeout=120
    )
    fi = ["-i", str(orig_aac)]
    for ts in tts_segs:
        fi += ["-i", ts["file"]]
    fp = [f"[0:a]volume={mix_ratio}[bg]"]
    ll = []
    for i, ts in enumerate(tts_segs):
        ms = int(ts["start"] * 1000)
        fp.append(f"[{i+1}:a]adelay={ms}|{ms},volume=1.0[t{i}]")
        ll.append(f"[t{i}]")
    fp.append(
        f"[bg]{''.join(ll)}amix=inputs={len(tts_segs)+1}"
        f":duration=first:dropout_transition=2[out]"
    )
    mixed = DUBBED_DIR / f"{vid_id}_mixed.aac"
    r = subprocess.run(
        ["ffmpeg","-y"] + fi + ["-filter_complex",";".join(fp),
         "-map","[out]","-acodec","aac","-b:a","192k",str(mixed)],
        capture_output=True, timeout=600
    )
    if r.returncode != 0 or not mixed.exists():
        log_fn("Audio mix failed", "warn")
        return None
    log_fn("Audio mixed", "ok")

    log_fn("Merging video...", "info")
    dubbed = DUBBED_DIR / f"{vid_id}_dubbed.mp4"
    subprocess.run(
        ["ffmpeg","-y","-i",str(video_path),"-i",str(mixed),
         "-c:v","copy","-map","0:v:0","-map","1:a:0","-shortest",str(dubbed)],
        capture_output=True, timeout=300
    )
    if not dubbed.exists():
        raise Exception("Video merge failed")
    try:
        import shutil
        shutil.rmtree(tts_dir, ignore_errors=True)
        for f in [audio_path, orig_aac, mixed]:
            f.unlink(missing_ok=True)
    except Exception:
        pass
    log_fn(f"Dubbed: {dubbed.name} ({dubbed.stat().st_size/1024/1024:.1f} MB)", "ok")
    return dubbed

# ══════════════════════════════════════════════════════════
# SHORTS GENERATOR
# ══════════════════════════════════════════════════════════
def generate_shorts(video_path: Path, cfg: dict, log_fn) -> list:
    count    = min(int(cfg.get("shorts_count", 3)), 10)
    duration = min(int(cfg.get("shorts_duration", 60)), 90)
    strategy = cfg.get("shorts_strategy", "energy")
    vid_id   = video_path.stem
    log_fn(f"Generating {count} shorts ({duration}s, strategy={strategy})", "info")

    r = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1",str(video_path)],
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
            ["ffmpeg","-i",str(video_path),"-af",
             "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f","null","-"],
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
                 if e > avg + 0.3*std and 5 < i < len(energy)-duration],
                key=lambda x: x[1], reverse=True
            )
            for ts, _ in candidates:
                if all(abs(ts-t) > duration+5 for t in timestamps):
                    timestamps.append(ts)
                if len(timestamps) >= count:
                    break
        if not timestamps:
            strategy = "uniform"

    if strategy == "scene":
        log_fn("Scene detection...", "info")
        r = subprocess.run(
            ["ffmpeg","-i",str(video_path),"-vf",
             "select='gt(scene,0.3)',showinfo","-f","null","-"],
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
            step = max(1, len(scenes)//count)
            timestamps = [int(scenes[i*step]) for i in range(min(count, len(scenes)))]
        else:
            strategy = "uniform"

    if strategy == "uniform" or not timestamps:
        gap = (total_dur - duration) / (count + 1)
        timestamps = [int(gap*(i+1)) for i in range(count)]

    r = subprocess.run(
        ["ffprobe","-v","error","-select_streams","v:0",
         "-show_entries","stream=width,height","-of","csv=p=0",str(video_path)],
        capture_output=True, text=True, timeout=15
    )
    try:
        w, h = map(int, r.stdout.strip().split(","))
    except Exception:
        w, h = 1920, 1080

    if w/h > 9/16:
        new_w = int(h * 9/16)
        crop  = f"crop={new_w}:{h}:({w}-{new_w})/2:0,scale=1080:1920"
    else:
        crop = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:-1:-1:color=black"

    shorts_list = []
    for i, start in enumerate(timestamps[:count]):
        out = SHORTS_DIR / f"{vid_id}_short{i+1}.mp4"
        log_fn(f"Cutting #{i+1} at {start}s...", "info")
        r = subprocess.run(
            ["ffmpeg","-y","-ss",str(start),"-i",str(video_path),
             "-t",str(duration),"-vf",crop,
             "-c:v","libx264","-preset","fast","-crf","23",
             "-c:a","aac","-b:a","128k","-movflags","+faststart",str(out)],
            capture_output=True, timeout=180
        )
        if r.returncode == 0 and out.exists():
            mb = out.stat().st_size / 1024 / 1024
            shorts_list.append({
                "file":str(out),"filename":out.name,
                "start":start,"duration":duration,
                "size_mb":round(mb,1),"index":i+1
            })
            log_fn(f"Short #{i+1}: {out.name} ({mb:.1f} MB)", "ok")
        else:
            log_fn(f"Short #{i+1} failed", "warn")
    return shorts_list


# ══════════════════════════════════════════════════════════
# JOB RUNNERS
# ══════════════════════════════════════════════════════════
def _make_log(job: dict, job_id: str, user_id: int):
    def log(msg: str, level: str = "info"):
        entry = {"t": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
        job["log"].append(entry)
        ws_broadcast({
            "type": "job_update", "job_id": job_id, "user_id": user_id,
            "log_entry": entry, "progress": job.get("progress", 0),
            "stage": job.get("stage", ""), "status": job.get("status", "running"),
            "mode": job.get("mode", "transfer")
        })
    return log

def _download_video(job, log, url, cfg, vid_id, include_thumb=True) -> Path:
    job["stage"]    = "download"
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
    fmt      = quality_map.get(cfg.get("quality","best"), quality_map["best"])
    out_tmpl = str(DOWNLOAD_DIR / f"{vid_id}.%(ext)s")
    dl_cmd   = (ytdlp_cmd() + ["-f",fmt,"--merge-output-format","mp4",
                "--no-playlist","--no-warnings","--newline"] +
               (["--write-thumbnail","--convert-thumbnails","jpg"] if include_thumb else []) +
               ytdlp_auth_args(cfg) + ["-o",out_tmpl,url])
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
            if f.suffix in (".mp4",".mkv",".webm"):
                video_path = f; break
    if not video_path.exists():
        raise Exception("Video file not found after download")
    log(f"Downloaded: {video_path.name} ({video_path.stat().st_size/1024/1024:.1f} MB)", "ok")
    return video_path

def run_dub_transfer(job_id: str, url: str, cfg: dict, user_id: int) -> None:
    job = jobs[job_id]
    job["status"] = "running"
    job["log"]    = []
    job["mode"]   = "dub"
    log = _make_log(job, job_id, user_id)
    try:
        job["stage"] = "metadata"; job["progress"] = 5
        log("Fetching metadata...")
        meta_cmd = ytdlp_cmd() + ["--dump-json","--no-playlist","--no-warnings"] + ytdlp_auth_args(cfg) + [url]
        result   = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise Exception("yt-dlp metadata failed")
        meta = json.loads(result.stdout)
        job["meta"] = {
            "title":       meta.get("title","")[:80],
            "channel":     meta.get("uploader",""),
            "duration":    meta.get("duration",0),
            "tags":        meta.get("tags",[])[:20],
            "description": meta.get("description","")[:4500],
            "thumbnail":   meta.get("thumbnail",""),
        }
        vid_id = meta.get("id","video")
        log(f"Video: {job['meta']['title']}", "ok")

        video_path = _download_video(job, log, url, cfg, vid_id, include_thumb=False)
        job["progress"] = 42; job["stage"] = "dubbing"
        log("Starting AI dubbing...")
        dubbed = run_ai_dubbing(video_path, cfg, log)
        job["dubbed_file"] = str(dubbed) if dubbed and dubbed.exists() else str(video_path)
        if dubbed and dubbed.exists():
            log("Dubbing complete!", "ok")
        else:
            log("Dubbing failed — original preserved", "warn")

        job["progress"] = 100; job["status"] = "done"
        log("Done! File ready for download.", "ok")
        ws_broadcast({"type":"job_done","job_id":job_id,"user_id":user_id,"mode":"dub"})
        save_history(user_id,{
            "ts":datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url":url,"title":"[DUB] "+job["meta"]["title"],
            "rutube_url":"dubbed","status":"success","mode":"dub"
        })
    except Exception as e:
        job["status"]="error"; job["error"]=str(e)
        log(f"Error: {e}","error")
        ws_broadcast({"type":"job_error","job_id":job_id,"user_id":user_id,"error":str(e)})
        save_history(user_id,{
            "ts":datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url":url,"title":"","rutube_url":"",
            "status":f"error: {e}","mode":"dub"
        })
    finally:
        _send_telegram_notify(cfg, job, url)

def run_shorts_transfer(job_id: str, url: str, cfg: dict, user_id: int) -> None:
    job = jobs[job_id]
    job["status"] = "running"
    job["log"]    = []
    job["shorts"] = []
    job["mode"]   = "shorts"
    log = _make_log(job, job_id, user_id)
    try:
        job["stage"] = "metadata"; job["progress"] = 5
        log("Fetching metadata...")
        meta_cmd = ytdlp_cmd() + ["--dump-json","--no-playlist","--no-warnings"] + ytdlp_auth_args(cfg) + [url]
        result   = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise Exception("yt-dlp metadata failed")
        meta = json.loads(result.stdout)
        job["meta"] = {
            "title":    meta.get("title","")[:80],
            "channel":  meta.get("uploader",""),
            "duration": meta.get("duration",0),
            "tags":     meta.get("tags",[])[:20],
        }
        vid_id = meta.get("id","video")
        log(f"Video: {job['meta']['title']} ({meta.get('duration',0)}s)", "ok")

        video_path = _download_video(job, log, url, cfg, vid_id, include_thumb=False)
        job["progress"] = 42; job["stage"] = "cutting"
        log("Cutting shorts...")
        shorts       = generate_shorts(video_path, cfg, log)
        job["shorts"] = shorts
        log(f"Created {len(shorts)} shorts", "ok")
        job["progress"] = 100; job["status"] = "done"
        log("Done! Shorts ready for download.", "ok")
        ws_broadcast({"type":"job_done","job_id":job_id,"user_id":user_id,
                      "mode":"shorts","shorts_count":len(shorts)})
        save_history(user_id,{
            "ts":datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url":url,"title":"[SHORTS] "+job["meta"]["title"],
            "rutube_url":f"{len(shorts)} shorts","status":"success","mode":"shorts"
        })
    except Exception as e:
        job["status"]="error"; job["error"]=str(e)
        log(f"Error: {e}","error")
        ws_broadcast({"type":"job_error","job_id":job_id,"user_id":user_id,"error":str(e)})
        save_history(user_id,{
            "ts":datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url":url,"title":"","rutube_url":"",
            "status":f"error: {e}","mode":"shorts"
        })
    finally:
        _send_telegram_notify(cfg, job, url)

def run_transfer(job_id: str, url: str, cfg: dict, user_id: int) -> None:
    job = jobs[job_id]
    job["status"] = "running"
    job["log"]    = []
    log = _make_log(job, job_id, user_id)
    try:
        # ── 1. Metadata ───────────────────────────────────
        job["stage"] = "metadata"; job["progress"] = 5
        log("Fetching video metadata...")
        meta_cmd = ytdlp_cmd() + ["--dump-json","--no-playlist","--no-warnings"] + ytdlp_auth_args(cfg) + [url]
        result   = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise Exception("yt-dlp metadata failed: " + result.stderr[:300])
        meta = json.loads(result.stdout)
        job["meta"] = {
            "title":       meta.get("title","Untitled")[:80],
            "channel":     meta.get("uploader",""),
            "duration":    meta.get("duration",0),
            "view_count":  meta.get("view_count",0),
            "like_count":  meta.get("like_count",0),
            "tags":        meta.get("tags",[])[:20],
            "description": meta.get("description","")[:4500],
            "thumbnail":   meta.get("thumbnail",""),
            "upload_date": meta.get("upload_date",""),
        }
        vid_id = meta.get("id","video")
        log(f"Title: {job['meta']['title']}", "ok")
        log(f"Channel: {job['meta']['channel']}", "ok")
        log(f"Tags: {len(job['meta']['tags'])} tags found", "ok")

        # ── 2. Download ───────────────────────────────────
        video_path = _download_video(job, log, url, cfg, vid_id, include_thumb=True)
        thumb_path = DOWNLOAD_DIR / f"{vid_id}.jpg"
        size_mb    = video_path.stat().st_size / 1024 / 1024
        job["progress"] = 70

        # ── 3. Rutube upload via Playwright ───────────────
        log("Connecting to Rutube...", "info")
        job["stage"] = "upload"
        headers_base = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer":    "https://rutube.ru/",
            "Origin":     "https://rutube.ru",
        }
        access_token  = ""
        rutube_cookie = rutube_auth_cookie_file(cfg)
        if rutube_cookie:
            log("Rutube: cookie file auth", "ok")
            try:
                for line in Path(rutube_cookie).read_text(encoding="utf-8",errors="ignore").splitlines():
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
            auth_data = json.dumps({"username":cfg["rutube_user"],"password":cfg["rutube_pass"]}).encode()
            req = urllib.request.Request(
                "https://rutube.ru/api/accounts/token/", data=auth_data,
                headers={**headers_base,"Content-Type":"application/json"}, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read())
                    access_token = resp.get("access_token", resp.get("token",""))
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
                cookie_file = cfg.get("rutube_cookie_file","").strip()
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled","--no-sandbox"]
                    )
                    ctx = browser.new_context(
                        viewport={"width":1280,"height":900},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                                   " AppleWebKit/537.36 (KHTML, like Gecko)"
                                   " Chrome/125.0.0.0 Safari/537.36",
                    )
                    captured_ids = []
                    up = {"total":0,"done":0,"chunks":0,"last":time.time(),"finished":False}

                    def on_response(response):
                        try:
                            s, m = response.status, response.request.method
                            if "rutube.ru" in response.url and m in ("POST","PUT","PATCH") and s in (200,201):
                                ct = response.headers.get("content-type","")
                                if "json" in ct:
                                    body = response.text()
                                    for pat in [r'"video_id"\s*:\s*"([a-f0-9]{32})"',
                                                r'"id"\s*:\s*"([a-f0-9]{32})"']:
                                        m2 = re.search(pat, body)
                                        if m2 and m2.group(1) not in captured_ids:
                                            captured_ids.append(m2.group(1))
                            if s in (201,) and m == "POST":
                                loc = response.headers.get("location","")
                                if loc:
                                    m2 = re.search(r"/([a-f0-9]{32})(?:/|$|\?)", loc)
                                    if m2 and m2.group(1) not in captured_ids:
                                        captured_ids.append(m2.group(1))
                            if s in (200,201,204,308):
                                off_h = response.headers.get("upload-offset","")
                                len_h = response.headers.get("upload-length","")
                                if off_h or len_h or m == "PATCH":
                                    up["last"] = time.time()
                                if off_h:
                                    up["done"]   = max(up["done"], int(off_h))
                                    up["chunks"] += 1
                                if len_h:
                                    up["total"] = max(up["total"], int(len_h))
                                if up["total"] > 0 and up["done"] >= up["total"]:
                                    up["finished"] = True
                        except Exception:
                            pass

                    if cookie_file and Path(cookie_file).exists():
                        cookies = []
                        for line in Path(cookie_file).read_text(encoding="utf-8",errors="ignore").splitlines():
                            if line.startswith("#") or not line.strip():
                                continue
                            parts = line.strip().split("\t")
                            if len(parts) < 7:
                                continue
                            dom,_,path,secure,_,name,value = parts[:7]
                            cookies.append({
                                "name":name,"value":value,
                                "domain":dom if dom.startswith(".") else dom.lstrip("."),
                                "path":path,"secure":secure.upper()=="TRUE","sameSite":"None"
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
                        for sel in ['button:has-text("Загрузить")','button:has-text("Добавить")']:
                            try:
                                b = page.query_selector(sel)
                                if b and b.is_visible():
                                    b.click(); page.wait_for_timeout(2000); break
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
                    title  = job["meta"]["title"][:80]
                    tags   = job["meta"].get("tags",[])
                    desc   = job["meta"].get("description","")[:4500]
                    if tags:
                        desc += "\n\n" + " ".join(f"#{t.replace(' ','_')}" for t in tags)
                    filled = False
                    for sel in [
                        'input[name="title"]','input[placeholder*="азван"]',
                        'input[placeholder*="itle"]','input[aria-label*="азван"]',
                        '[class*="upload"] input[type="text"]',
                        '[class*="modal"] input[type="text"]',
                    ]:
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                el.click(); el.fill(""); el.type(title, delay=10)
                                log(f"Title filled: {title[:50]}", "ok")
                                filled = True; break
                        except Exception:
                            pass
                    if not filled:
                        for inp in page.query_selector_all('input[type="text"],input:not([type])'):
                            if inp.is_visible():
                                inp.click(); inp.fill(""); inp.type(title, delay=10)
                                log("Title filled (fallback)", "ok"); break

                    # Fill description
                    if desc:
                        for sel in ['textarea[name="description"]',
                                    'textarea[placeholder*="писан"]',
                                    'textarea[placeholder*="escription"]',
                                    '[class*="upload"] textarea','[class*="modal"] textarea']:
                            try:
                                el = page.query_selector(sel)
                                if el and el.is_visible():
                                    el.click(); el.fill(desc)
                                    log(f"Description filled ({len(desc)} chars)", "ok"); break
                            except Exception:
                                pass

                    # Attach thumbnail
                    if thumb_path.exists():
                        try:
                            for fi2 in page.query_selector_all('input[type="file"]'):
                                acc = fi2.get_attribute("accept") or ""
                                if "image" in acc:
                                    fi2.set_input_files(str(thumb_path.resolve()))
                                    log("Thumbnail attached", "ok"); break
                        except Exception:
                            pass

                    # Category
                    page.wait_for_timeout(1000)
                    for cat in ["Развлечения","Юмор","Блоги","Люди и блоги","Другое"]:
                        try:
                            opt = page.query_selector(f'text="{cat}"')
                            if opt and opt.is_visible():
                                opt.click(); log(f"Category: {cat}","ok"); break
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
                            pct = min(95, int(up["chunks"] / max(1, size_mb/5) * 100))
                        else:
                            pct = 0
                        job["progress"] = 75 + int(min(pct,100) * 0.17)
                        if pct >= last_pct + 10:
                            if up["total"] > 0:
                                log(f"Upload: {up['done']//1024//1024}/{up['total']//1024//1024} MB ({pct}%) — {elapsed}s", "info")
                            else:
                                log(f"Upload: {up['chunks']} chunks — {elapsed}s", "info")
                            last_pct = pct
                        if up["finished"]:
                            log(f"File transfer complete ({elapsed}s)", "ok"); break
                        idle = time.time() - up["last"]
                        if idle > 120 and elapsed > 60:
                            if up["chunks"] > 5:
                                log(f"No activity {idle:.0f}s — upload assumed finished", "warn")
                                up["finished"] = True; break
                            elif up["chunks"] == 0 and elapsed > 90:
                                log("Upload did not start — check auth cookies", "warn"); break

                    if up["finished"]:
                        page.wait_for_timeout(8000)

                    # Publish
                    for sel in ['button:has-text("Опубликовать")','button:has-text("Publish")']:
                        try:
                            b = page.query_selector(sel)
                            if b and b.is_visible():
                                b.click(); log("Clicked Publish","ok")
                                page.wait_for_timeout(5000); break
                        except Exception:
                            pass

                    # Extract video ID
                    cur_url = page.url
                    for pat in [r'/video/([a-f0-9]{32})',r'([a-f0-9]{32})']:
                        m2 = re.search(pat, cur_url)
                        if m2:
                            rutube_video_id = m2.group(1)
                            log(f"Video ID (URL): {rutube_video_id}","ok"); break
                    if not rutube_video_id:
                        try:
                            for pat in [r'video/([a-f0-9]{32})',r'"video_id"\s*:\s*"([a-f0-9]{32})"']:
                                m2 = re.search(pat, page.content())
                                if m2:
                                    rutube_video_id = m2.group(1)
                                    log(f"Video ID (page): {rutube_video_id}","ok"); break
                        except Exception:
                            pass
                    if not rutube_video_id and captured_ids:
                        rutube_video_id = captured_ids[-1]
                        log(f"Video ID (network): {rutube_video_id}","info")
                    if not rutube_video_id:
                        log("Could not get video ID — check https://studio.rutube.ru manually","warn")
                    browser.close()
            except Exception as e:
                log(f"Browser upload error: {e}","warn")

        job["progress"] = 92

        # API thumbnail upload
        if thumb_path.exists() and rutube_video_id and access_token:
            log("Uploading thumbnail via API...", "info")
            subprocess.run([
                "curl","-s","-X","POST",
                f"https://rutube.ru/api/video/{rutube_video_id}/thumbnail/",
                "-F",f"file=@{thumb_path}",
                "-H",f"User-Agent: {headers_base['User-Agent']}",
                "-H",f"Authorization: Bearer {access_token}",
            ], capture_output=True, timeout=60)
            log("Thumbnail uploaded","ok")

        if not cfg.get("keep_files", True):
            video_path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)

        rutube_url       = (f"https://rutube.ru/video/{rutube_video_id}/"
                            if rutube_video_id else "https://rutube.ru")
        job["rutube_url"] = rutube_url
        job["progress"]   = 100
        job["status"]     = "done"
        log(f"Done! {rutube_url}","ok")
        ws_broadcast({"type":"job_done","job_id":job_id,"user_id":user_id})
        save_history(user_id,{
            "ts":datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url":url,"title":job["meta"]["title"],
            "rutube_url":rutube_url,"status":"success","mode":"transfer"
        })
    except Exception as e:
        job["status"]="error"; job["error"]=str(e)
        log(f"Error: {e}","error")
        ws_broadcast({"type":"job_error","job_id":job_id,"user_id":user_id,"error":str(e)})
        save_history(user_id,{
            "ts":datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url":url,"title":"","rutube_url":"",
            "status":f"error: {e}","mode":"transfer"
        })
    finally:
        _send_telegram_notify(cfg, job, url)


# ══════════════════════════════════════════════════════════
# TELEGRAM + CHANNEL MONITOR
# ══════════════════════════════════════════════════════════
def tg_send(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> dict | None:
    try:
        payload = json.dumps({
            "chat_id":chat_id,"text":text,
            "parse_mode":parse_mode,"disable_web_page_preview":False
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type":"application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[warn] tg_send: {e}")
        return None

def _send_telegram_notify(cfg: dict, job: dict, url: str) -> None:
    try:
        if not (cfg.get("telegram_enabled") and cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")):
            return
        token, chat_id = cfg["telegram_bot_token"], cfg["telegram_chat_id"]
        if job.get("status") == "done":
            title = job.get("meta",{}).get("title","—")
            rutube_url = job.get("rutube_url","")
            text = (f"✅ <b>Перенос завершён</b>\n\n"
                    f"📹 <b>{title}</b>\n▶️ {url}\n📺 {rutube_url}\n"
                    f"🕐 {datetime.now().strftime('%H:%M:%S')}")
        else:
            text = (f"❌ <b>Ошибка переноса</b>\n\n"
                    f"▶️ {url}\n💥 {job.get('error','?')[:300]}\n"
                    f"🕐 {datetime.now().strftime('%H:%M:%S')}")
        tg_send(token, chat_id, text)
    except Exception:
        pass

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
            cmd    = ytdlp_cmd() + ["--print","channel_id","--playlist-items","1","--no-warnings",
                                    f"https://www.youtube.com/@{m.group(1)}/videos"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            cid    = result.stdout.strip()
            if cid.startswith("UC"):
                return cid
        except Exception:
            pass
    return url_or_id

def fetch_rss_videos(channel_id: str) -> list:
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent":"Mozilla/5.0 (compatible; YT2RT)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            content = r.read().decode("utf-8", errors="ignore")
        entries = []
        for m in re.finditer(r"<entry>(.*?)</entry>", content, re.DOTALL):
            xml  = m.group(1)
            vid  = re.search(r"<yt:videoId>([\w-]+)</yt:videoId>", xml)
            tit  = re.search(r"<title>(.*?)</title>", xml)
            pub  = re.search(r"<published>(.*?)</published>", xml)
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

_monitor_started = False

def _monitor_loop():
    while True:
        try:
            with get_db() as db:
                admin = db.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
                admin_id = admin["id"] if admin else 1
            cfg = load_user_config(admin_id)
            if not cfg.get("monitor_enabled"):
                time.sleep(30); continue
            channels = cfg.get("monitor_channels", [])
            if not channels:
                time.sleep(30); continue
            interval = max(5, cfg.get("monitor_interval", 15)) * 60
            state    = json.loads(MONITOR_STATE_FILE.read_text()) if MONITOR_STATE_FILE.exists() else {}
            for ch in channels:
                ch_url     = ch if isinstance(ch, str) else ch.get("url","")
                channel_id = extract_channel_id(ch_url)
                videos     = fetch_rss_videos(channel_id)
                known      = set(state.get(channel_id,[]))
                for vid in videos:
                    if vid["video_id"] not in known:
                        print(f"[monitor] New video: {vid['title']}")
                        if cfg.get("telegram_enabled") and cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"):
                            tg_send(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                                    f"🆕 <b>Новое видео!</b>\n📹 <b>{vid['title']}</b>\n"
                                    f"▶️ {vid['url']}\n🚀 Запускаю перенос...")
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

start_monitor()

# ══════════════════════════════════════════════════════════
# FLASK ROUTES — AUTH
# ══════════════════════════════════════════════════════════
@app.route("/login", methods=["GET","POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        identifier = request.form.get("username","").strip()
        password   = request.form.get("password","")
        user = get_user_by_username(identifier) or get_user_by_email(identifier)
        if user and user["is_active"] and check_password_hash(user["password_hash"], password):
            session.permanent = True
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["role"]     = user["role"]
            with get_db() as db:
                db.execute("UPDATE users SET last_login=? WHERE id=?",
                           (datetime.now().isoformat(), user["id"]))
                db.commit()
            return redirect(url_for("index"))
        error = "Неверный логин или пароль"
    return render_template_string(LOGIN_HTML, error=error, mode="login")

@app.route("/register", methods=["GET","POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username  = request.form.get("username","").strip()
        email     = request.form.get("email","").strip().lower()
        password  = request.form.get("password","")
        password2 = request.form.get("password2","")
        if not username or not email or not password:
            error = "Заполните все поля"
        elif len(username) < 3:
            error = "Имя пользователя: минимум 3 символа"
        elif not re.match(r"^[a-zA-Z0-9_.-]+$", username):
            error = "Имя пользователя: только латиница, цифры, _ . -"
        elif not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            error = "Некорректный email"
        elif len(password) < 6:
            error = "Пароль: минимум 6 символов"
        elif password != password2:
            error = "Пароли не совпадают"
        elif get_user_by_username(username):
            error = "Имя пользователя уже занято"
        elif get_user_by_email(email):
            error = "Email уже зарегистрирован"
        else:
            with get_db() as db:
                cur = db.execute(
                    "INSERT INTO users (username,email,password_hash,role,created_at) VALUES(?,?,?,'user',?)",
                    (username, email, generate_password_hash(password), datetime.now().isoformat())
                )
                new_id = cur.lastrowid
                db.execute("INSERT INTO user_settings (user_id,config_json) VALUES(?,?)",
                           (new_id, json.dumps(default_config())))
                db.commit()
            session.permanent = True
            session["user_id"]  = new_id
            session["username"] = username
            session["role"]     = "user"
            return redirect(url_for("index"))
    return render_template_string(LOGIN_HTML, error=error, mode="register")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Main route ─────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    user = current_user()
    user_json = json.dumps({
        "id": user["id"], "username": user["username"], "role": user["role"]
    })
    inject = f'<script>const SERVER_USER = {user_json};</script>'
    return HTML.replace("<!-- USER_INJECT -->", inject)

# ══════════════════════════════════════════════════════════
# API ROUTES (user-scoped)
# ══════════════════════════════════════════════════════════
@app.route("/api/status")
@login_required
def api_status():
    uid = session["user_id"]
    return jsonify({
        "ytdlp": ytdlp_available(),
        "ffmpeg": ffmpeg_available(),
        "config": load_user_config(uid)
    })

@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    data = request.json or {}
    url  = data.get("url","").strip()
    if not url:
        return jsonify({"error":"No URL"}), 400
    if not is_valid_yt_url(url):
        return jsonify({"error":"Invalid YouTube URL"}), 400
    uid    = session["user_id"]
    job_id = enqueue_job(url, load_user_config(uid), uid)
    return jsonify({"job_id": job_id})

@app.route("/api/job/<job_id>")
@login_required
def api_job(job_id):
    j = jobs.get(job_id)
    if not j:
        return jsonify({"error":"Not found"}), 404
    if j.get("user_id") != session["user_id"] and session.get("role") != "admin":
        return jsonify({"error":"Forbidden"}), 403
    return jsonify(j)

@app.route("/api/history")
@login_required
def api_history():
    return jsonify(load_history(session["user_id"]))

@app.route("/api/config", methods=["GET","POST"])
@login_required
def api_config():
    uid = session["user_id"]
    if request.method == "POST":
        save_user_config(uid, request.json)
        return jsonify({"ok": True})
    return jsonify(load_user_config(uid))

@app.route("/api/export_cookies", methods=["POST"])
@login_required
def api_export_cookies():
    import shutil
    data     = request.json or {}
    browser  = data.get("browser","chrome").strip()
    out_file = data.get("cookie_file","yt_cookies.txt").strip()
    cmd = (["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable,"-m","yt_dlp"]) + [
        "--cookies-from-browser",browser,"--cookies",out_file,
        "--skip-download","https://www.youtube.com/",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if Path(out_file).exists():
            return jsonify({"ok":True,"path":out_file})
        return jsonify({"ok":False,"error":result.stderr[:300]})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/save_cookies", methods=["POST"])
@login_required
def api_save_cookies():
    data        = request.json or {}
    cookie_text = data.get("cookies","").strip()
    cookie_file = safe_filename(data.get("cookie_file","cookies.txt"), "cookies.txt")
    try:
        Path(cookie_file).write_text(cookie_text, encoding="utf-8")
        return jsonify({"ok":True,"path":str(Path(cookie_file).resolve())})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 500

@app.route("/api/batch", methods=["POST"])
@login_required
def api_batch():
    data    = request.json or {}
    urls    = [u.strip() for u in data.get("urls","").split("\n") if u.strip()]
    ids, invalid = [], []
    uid     = session["user_id"]
    cfg     = load_user_config(uid)
    for url in urls:
        if not is_valid_yt_url(url):
            invalid.append(url); continue
        ids.append({"job_id": enqueue_job(url, cfg, uid), "url": url})
    return jsonify({"jobs":ids,"invalid":invalid,"total":len(urls)})

@app.route("/api/preview", methods=["POST"])
@login_required
def api_preview():
    data = request.json or {}
    url  = data.get("url","").strip()
    if not url or not is_valid_yt_url(url):
        return jsonify({"error":"Invalid YouTube URL"}), 400
    try:
        cfg  = load_user_config(session["user_id"])
        cmd  = ytdlp_cmd() + ["--dump-json","--no-playlist","--no-warnings"] + ytdlp_auth_args(cfg) + [url]
        res  = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            return jsonify({"error":"Could not fetch metadata"}), 400
        meta = json.loads(res.stdout)
        return jsonify({
            "title":       meta.get("title","Untitled")[:120],
            "channel":     meta.get("uploader",""),
            "duration":    meta.get("duration",0),
            "view_count":  meta.get("view_count",0),
            "like_count":  meta.get("like_count",0),
            "tags":        meta.get("tags",[])[:20],
            "description": meta.get("description","")[:300],
            "thumbnail":   meta.get("thumbnail",""),
            "upload_date": meta.get("upload_date",""),
        })
    except Exception as e:
        return jsonify({"error":str(e)[:200]}), 500

@app.route("/api/telegram/test", methods=["POST"])
@login_required
def api_telegram_test():
    cfg     = load_user_config(session["user_id"])
    token   = cfg.get("telegram_bot_token","").strip()
    chat_id = cfg.get("telegram_chat_id","").strip()
    if not token or not chat_id:
        return jsonify({"ok":False,"error":"Не указан токен бота или chat_id"}), 400
    result = tg_send(token, chat_id,
                     "✅ <b>YT→Rutube Transfer</b>\n\nТестовое сообщение — уведомления работают!")
    if result and result.get("ok"):
        return jsonify({"ok":True})
    return jsonify({"ok":False,"error":str(result)}), 400

@app.route("/api/monitor/channels")
@login_required
def api_monitor_channels():
    cfg = load_user_config(session["user_id"])
    return jsonify({"channels":cfg.get("monitor_channels",[]),
                    "enabled":cfg.get("monitor_enabled",False),
                    "interval":cfg.get("monitor_interval",15)})

@app.route("/api/monitor/add", methods=["POST"])
@login_required
def api_monitor_add():
    data = request.json or {}
    url  = data.get("url","").strip()
    if not url:
        return jsonify({"error":"No URL"}), 400
    uid  = session["user_id"]
    cfg  = load_user_config(uid)
    channels   = cfg.get("monitor_channels",[])
    channel_id = extract_channel_id(url)
    channel_name = url
    try:
        cmd = ytdlp_cmd()+["--print","channel","--playlist-items","1","--no-warnings",
                           url if url.startswith("http") else f"https://www.youtube.com/channel/{url}"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode == 0 and res.stdout.strip():
            channel_name = res.stdout.strip()
    except Exception:
        pass
    existing = {c.get("channel_id","") for c in channels if isinstance(c,dict)}
    if channel_id in existing:
        return jsonify({"ok":False,"error":"Канал уже отслеживается"})
    entry = {"url":url,"channel_id":channel_id,"name":channel_name,
             "added":datetime.now().strftime("%Y-%m-%d %H:%M")}
    channels.append(entry)
    cfg["monitor_channels"] = channels
    save_user_config(uid, cfg)
    state = json.loads(MONITOR_STATE_FILE.read_text()) if MONITOR_STATE_FILE.exists() else {}
    if channel_id not in state:
        videos = fetch_rss_videos(channel_id)
        state[channel_id] = [v["video_id"] for v in videos]
        MONITOR_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return jsonify({"ok":True,"channel":entry,"seeded_videos":len(state.get(channel_id,[]))})

@app.route("/api/monitor/remove", methods=["POST"])
@login_required
def api_monitor_remove():
    data       = request.json or {}
    channel_id = data.get("channel_id","").strip()
    uid        = session["user_id"]
    cfg        = load_user_config(uid)
    cfg["monitor_channels"] = [
        c for c in cfg.get("monitor_channels",[])
        if (c.get("channel_id") if isinstance(c,dict) else c) != channel_id
    ]
    save_user_config(uid, cfg)
    return jsonify({"ok":True})

@app.route("/api/monitor/toggle", methods=["POST"])
@login_required
def api_monitor_toggle():
    data = request.json or {}
    uid  = session["user_id"]
    cfg  = load_user_config(uid)
    cfg["monitor_enabled"]  = bool(data.get("enabled",False))
    if "interval" in data:
        cfg["monitor_interval"] = max(5, int(data["interval"]))
    save_user_config(uid, cfg)
    return jsonify({"ok":True,"enabled":cfg["monitor_enabled"],"interval":cfg["monitor_interval"]})

# ── Dub API ────────────────────────────────────────────────
@app.route("/api/dub/start", methods=["POST"])
@login_required
def api_dub_start():
    data = request.json or {}
    url  = data.get("url","").strip()
    if not url or not is_valid_yt_url(url):
        return jsonify({"error":"Invalid YouTube URL"}), 400
    uid  = session["user_id"]
    cfg  = load_user_config(uid)
    for k in ["dub_source_lang","dub_target_lang","dub_whisper_model","dub_mix_original"]:
        if k in data: cfg[k] = data[k]
    return jsonify({"job_id": enqueue_dub_job(url, cfg, uid)})

@app.route("/api/dub/status")
@login_required
def api_dub_status():
    return jsonify({"whisper":check_whisper(),"edge_tts":check_edge_tts(),"ffmpeg":ffmpeg_available()})

@app.route("/api/dub/install", methods=["POST"])
@login_required
def api_dub_install():
    results = {}
    for pkg in ["openai-whisper","edge-tts","deep-translator"]:
        r = subprocess.run([sys.executable,"-m","pip","install",pkg,"--break-system-packages","-q"],
                           capture_output=True, text=True, timeout=600)
        results[pkg] = r.returncode == 0
    return jsonify({"ok":all(results.values()),"results":results})

@app.route("/api/dub/download/<job_id>")
@login_required
def api_dub_download(job_id):
    j = jobs.get(job_id)
    if not j or not j.get("dubbed_file"):
        return jsonify({"error":"Not found"}), 404
    if j.get("user_id") != session["user_id"] and session.get("role") != "admin":
        return jsonify({"error":"Forbidden"}), 403
    p = Path(j["dubbed_file"])
    if not p.exists():
        return jsonify({"error":"File not found"}), 404
    return send_from_directory(str(p.parent), p.name, as_attachment=True)

# ── Shorts API ─────────────────────────────────────────────
@app.route("/api/shorts/start", methods=["POST"])
@login_required
def api_shorts_start():
    data = request.json or {}
    url  = data.get("url","").strip()
    if not url or not is_valid_yt_url(url):
        return jsonify({"error":"Invalid YouTube URL"}), 400
    uid = session["user_id"]
    cfg = load_user_config(uid)
    for k in ["shorts_count","shorts_duration","shorts_strategy"]:
        if k in data: cfg[k] = data[k]
    return jsonify({"job_id": enqueue_shorts_job(url, cfg, uid)})

@app.route("/api/shorts/<job_id>")
@login_required
def api_shorts_list(job_id):
    j = jobs.get(job_id)
    if not j: return jsonify({"error":"Not found"}), 404
    return jsonify({"shorts": j.get("shorts",[])})

@app.route("/api/shorts/download/<filename>")
@login_required
def api_shorts_download(filename):
    filename = safe_filename(filename, "clip.mp4")
    return send_from_directory(str(SHORTS_DIR), filename, as_attachment=True)

# ── Dashboard API ──────────────────────────────────────────
@app.route("/api/dashboard")
@login_required
def api_dashboard():
    uid   = session["user_id"]
    role  = session.get("role")
    active, recent = [], []
    stats = {"total":0,"done":0,"error":0,"running":0,"queued":0}
    for jid, j in list(jobs.items()):
        if j.get("user_id") != uid and role != "admin":
            continue
        stats["total"] += 1
        s = j.get("status","unknown")
        if s in stats: stats[s] += 1
        entry = {"job_id":jid,"status":s,"progress":j.get("progress",0),
                 "stage":j.get("stage",""),"meta":j.get("meta",{}),
                 "mode":j.get("mode","transfer"),"shorts_count":len(j.get("shorts",[]))}
        if s in ("running","starting","queued"): active.append(entry)
        else: recent.append(entry)
    stats["queue_size"]   = len(job_queue)
    stats["ws_clients"]   = len(ws_clients)
    stats["ws_available"] = _ws_available
    return jsonify({"active":active,"recent":recent[-20:],"stats":stats})

# ── WebSocket ──────────────────────────────────────────────
if _ws_available and sock:
    @sock.route("/ws")
    def ws_handler(ws):
        if not session.get("user_id"):
            return
        with ws_lock:
            ws_clients.add(ws)
        try:
            while True:
                data = ws.receive(timeout=30)
                if data is None: break
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        ws.send(json.dumps({"type":"pong"}))
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            with ws_lock:
                ws_clients.discard(ws)


# ══════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════
@app.route("/admin")
@admin_required
def admin_panel():
    return ADMIN_HTML.replace("<!-- USER_INJECT -->",
        f'<script>const SERVER_USER = {json.dumps({"id":session["user_id"],"username":session["username"],"role":"admin"})};</script>')

@app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    with get_db() as db:
        total_users   = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_users  = db.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0]
        admin_count   = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        total_history = db.execute("SELECT COUNT(*) FROM job_history").fetchone()[0]
        ok_history    = db.execute("SELECT COUNT(*) FROM job_history WHERE status='success'").fetchone()[0]
    active_jobs = sum(1 for j in jobs.values() if j.get("status") not in ("done","error"))
    return jsonify({
        "users":  {"total":total_users,"active":active_users,"admins":admin_count},
        "jobs":   {"total_history":total_history,"success_history":ok_history,
                   "active_now":active_jobs,"queue_size":len(job_queue)},
        "system": {"ytdlp":ytdlp_available(),"ffmpeg":ffmpeg_available(),"ws_clients":len(ws_clients)}
    })

@app.route("/api/admin/users")
@admin_required
def api_admin_users():
    with get_db() as db:
        rows = db.execute(
            "SELECT id,username,email,role,is_active,created_at,last_login FROM users ORDER BY id"
        ).fetchall()
    result = []
    for row in rows:
        u = dict(row)
        u["job_count"] = sum(1 for j in jobs.values() if j.get("user_id")==u["id"])
        result.append(u)
    return jsonify(result)

@app.route("/api/admin/users/create", methods=["POST"])
@admin_required
def api_admin_create_user():
    data     = request.json or {}
    username = data.get("username","").strip()
    email    = data.get("email","").strip().lower()
    password = data.get("password","").strip()
    role     = data.get("role","user")
    if not username or not email or not password:
        return jsonify({"error":"All fields required"}), 400
    if role not in ("user","admin"):
        role = "user"
    if get_user_by_username(username) or get_user_by_email(email):
        return jsonify({"error":"Username or email already exists"}), 400
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO users (username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
            (username, email, generate_password_hash(password), role, datetime.now().isoformat())
        )
        new_id = cur.lastrowid
        db.execute("INSERT INTO user_settings (user_id,config_json) VALUES(?,?)",
                   (new_id, json.dumps(default_config())))
        db.commit()
    return jsonify({"ok":True,"user_id":new_id})

@app.route("/api/admin/users/<int:uid>/toggle", methods=["POST"])
@admin_required
def api_admin_toggle_user(uid):
    if uid == session["user_id"]:
        return jsonify({"error":"Cannot deactivate yourself"}), 400
    with get_db() as db:
        row = db.execute("SELECT is_active FROM users WHERE id=?", (uid,)).fetchone()
        if not row: return jsonify({"error":"User not found"}), 404
        new_val = 0 if row["is_active"] else 1
        db.execute("UPDATE users SET is_active=? WHERE id=?", (new_val, uid))
        db.commit()
    return jsonify({"ok":True,"is_active":new_val})

@app.route("/api/admin/users/<int:uid>/role", methods=["POST"])
@admin_required
def api_admin_change_role(uid):
    if uid == session["user_id"]:
        return jsonify({"error":"Cannot change your own role"}), 400
    data = request.json or {}
    role = data.get("role","user")
    if role not in ("user","admin"):
        return jsonify({"error":"Invalid role"}), 400
    with get_db() as db:
        db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        db.commit()
    return jsonify({"ok":True})

@app.route("/api/admin/users/<int:uid>/reset_password", methods=["POST"])
@admin_required
def api_admin_reset_password(uid):
    data     = request.json or {}
    new_pass = data.get("password","").strip()
    if len(new_pass) < 6:
        return jsonify({"error":"Password must be at least 6 characters"}), 400
    with get_db() as db:
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(new_pass), uid))
        db.commit()
    return jsonify({"ok":True})

@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_admin_delete_user(uid):
    if uid == session["user_id"]:
        return jsonify({"error":"Cannot delete yourself"}), 400
    with get_db() as db:
        db.execute("DELETE FROM user_settings WHERE user_id=?", (uid,))
        db.execute("DELETE FROM job_history WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()
    return jsonify({"ok":True})

@app.route("/api/admin/jobs")
@admin_required
def api_admin_jobs():
    return jsonify([{
        "job_id":  jid,
        "user_id": j.get("user_id"),
        "status":  j.get("status"),
        "progress":j.get("progress",0),
        "stage":   j.get("stage",""),
        "mode":    j.get("mode","transfer"),
        "title":   j.get("meta",{}).get("title","—"),
        "error":   j.get("error",""),
    } for jid, j in list(jobs.items())])

@app.route("/api/admin/history")
@admin_required
def api_admin_history():
    with get_db() as db:
        rows = db.execute(
            "SELECT jh.*,u.username FROM job_history jh"
            " JOIN users u ON jh.user_id=u.id ORDER BY jh.id DESC LIMIT 200"
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/clear_jobs", methods=["POST"])
@admin_required
def api_admin_clear_jobs():
    to_rm = [jid for jid,j in list(jobs.items()) if j.get("status") in ("done","error")]
    for jid in to_rm:
        jobs.pop(jid, None)
    return jsonify({"ok":True,"removed":len(to_rm)})


# ══════════════════════════════════════════════════════════
# HTML — LOGIN / REGISTER PAGE
# ══════════════════════════════════════════════════════════
LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT → Rutube — Вход</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#07080f;--surface:rgba(255,255,255,0.05);--glass:rgba(255,255,255,0.07);
  --border:rgba(255,255,255,0.09);--border2:rgba(255,255,255,0.16);
  --text:#f0f1ff;--text2:#9098c0;--text3:#464d72;
  --red:#ff4d6a;--blue:#63b3ff;--green:#34e09a;
  --redglow:rgba(255,77,106,0.2);--blueglow:rgba(99,179,255,0.18);
  --r:12px;--r2:20px;--blur:blur(24px) saturate(160%);
}
html,body{min-height:100vh;font-family:'Outfit',sans-serif;background:var(--bg);color:var(--text);
  display:flex;align-items:center;justify-content:center;
  background:
    radial-gradient(ellipse 70% 50% at 20% 15%,rgba(63,100,220,0.14) 0%,transparent 60%),
    radial-gradient(ellipse 50% 60% at 80% 80%,rgba(99,179,255,0.09) 0%,transparent 55%),
    var(--bg);
}
.wrap{width:100%;max-width:420px;padding:20px}
.logo{font-family:'Syne',sans-serif;font-size:26px;font-weight:800;text-align:center;
  margin-bottom:32px;letter-spacing:-0.5px}
.logo .yt{color:var(--red);text-shadow:0 0 24px var(--redglow)}
.logo .arrow{color:var(--text3);margin:0 4px}
.logo .rt{color:var(--blue);text-shadow:0 0 24px var(--blueglow)}
.logo .sub{display:block;font-family:'Outfit',sans-serif;font-size:13px;font-weight:400;
  color:var(--text3);margin-top:4px}
.card{background:var(--glass);border:1px solid var(--border);border-radius:var(--r2);
  padding:32px;backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
  box-shadow:0 8px 40px rgba(0,0,0,0.4);position:relative;overflow:hidden}
.card::before{content:'';position:absolute;inset:0;border-radius:var(--r2);
  background:linear-gradient(135deg,rgba(255,255,255,0.05) 0%,transparent 50%);pointer-events:none}
.tabs{display:flex;gap:0;margin-bottom:28px;background:rgba(0,0,0,0.2);
  border-radius:var(--r);padding:4px;border:1px solid var(--border)}
.tab{flex:1;padding:9px;border:none;background:none;color:var(--text2);
  font-family:'Outfit',sans-serif;font-size:14px;font-weight:500;
  border-radius:9px;cursor:pointer;transition:all .2s;text-align:center;text-decoration:none;display:block}
.tab:hover{color:var(--text)}
.tab.active{background:var(--glass);color:var(--text);
  border:1px solid var(--border2);box-shadow:0 2px 8px rgba(0,0,0,0.3)}
.field{margin-bottom:18px}
.field label{display:block;font-size:12px;color:var(--text2);margin-bottom:7px;font-weight:500}
.field input{width:100%;background:rgba(0,0,0,0.3);border:1px solid var(--border);
  color:var(--text);padding:12px 16px;border-radius:var(--r);
  font-family:'Outfit',sans-serif;font-size:14px;outline:none;
  transition:border-color .15s,box-shadow .15s}
.field input:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(99,179,255,0.12)}
.field input::placeholder{color:var(--text3)}
.btn-submit{width:100%;padding:13px;border-radius:var(--r);border:none;
  background:linear-gradient(135deg,var(--red),#c82848);color:#fff;
  font-family:'Outfit',sans-serif;font-size:15px;font-weight:600;
  cursor:pointer;transition:all .2s;margin-top:4px;
  box-shadow:0 4px 18px rgba(255,77,106,0.35)}
.btn-submit:hover{transform:translateY(-1px);box-shadow:0 6px 24px rgba(255,77,106,0.45)}
.btn-submit:active{transform:none}
.error{background:rgba(255,77,106,0.1);border:1px solid rgba(255,77,106,0.3);
  border-radius:var(--r);padding:11px 14px;font-size:13px;color:var(--red);
  margin-bottom:18px;display:flex;align-items:center;gap:8px}
.footer{text-align:center;margin-top:20px;font-size:12px;color:var(--text3)}
.footer a{color:var(--blue);text-decoration:none}
</style>
</head>
<body>
<div class="wrap">
  <div class="logo">
    <span class="yt">YT</span><span class="arrow">→</span><span class="rt">Rutube</span>
    <span class="sub">Transfer Platform</span>
  </div>
  <div class="card">
    <div class="tabs">
      <a class="tab {% if mode=='login' %}active{% endif %}" href="/login">Войти</a>
      <a class="tab {% if mode=='register' %}active{% endif %}" href="/register">Регистрация</a>
    </div>
    {% if error %}
    <div class="error">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>
      {{ error }}
    </div>
    {% endif %}
    {% if mode == 'login' %}
    <form method="POST" action="/login">
      <div class="field"><label>Логин или Email</label>
        <input type="text" name="username" placeholder="username или email" required autocomplete="username"></div>
      <div class="field"><label>Пароль</label>
        <input type="password" name="password" placeholder="••••••••" required autocomplete="current-password"></div>
      <button class="btn-submit" type="submit">Войти</button>
    </form>
    {% else %}
    <form method="POST" action="/register">
      <div class="field"><label>Имя пользователя</label>
        <input type="text" name="username" placeholder="myusername" required autocomplete="username"
               pattern="[a-zA-Z0-9_.\-]+" minlength="3"></div>
      <div class="field"><label>Email</label>
        <input type="email" name="email" placeholder="you@example.com" required autocomplete="email"></div>
      <div class="field"><label>Пароль</label>
        <input type="password" name="password" placeholder="минимум 6 символов" required minlength="6"></div>
      <div class="field"><label>Повторите пароль</label>
        <input type="password" name="password2" placeholder="повторите пароль" required></div>
      <button class="btn-submit" type="submit">Создать аккаунт</button>
    </form>
    {% endif %}
  </div>
  <div class="footer">YT→Rutube Transfer Platform v5.0</div>
</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════
# HTML — ADMIN PANEL
# ══════════════════════════════════════════════════════════
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT → Rutube — Админ</title>
<!-- USER_INJECT -->
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=JetBrains+Mono:wght@400;500&family=Outfit:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#07080f;--surface:rgba(255,255,255,0.04);--glass:rgba(255,255,255,0.06);
  --glass2:rgba(255,255,255,0.09);--border:rgba(255,255,255,0.08);--border2:rgba(255,255,255,0.14);
  --text:#f0f1ff;--text2:#9098c0;--text3:#464d72;
  --red:#ff4d6a;--blue:#63b3ff;--green:#34e09a;--yellow:#ffd666;--purple:#b39dff;--orange:#ff8c42;
  --redglow:rgba(255,77,106,0.18);--blueglow:rgba(99,179,255,0.15);--greenglow:rgba(52,224,154,0.15);
  --mono:'JetBrains Mono',monospace;--sans:'Outfit',sans-serif;--display:'Syne',sans-serif;
  --r:10px;--r2:16px;--r3:22px;--blur:blur(20px) saturate(180%);--shadow:0 8px 32px rgba(0,0,0,0.4);
}
html{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh}
body{max-width:1200px;margin:0 auto;padding:24px 20px 80px;
  background:radial-gradient(ellipse 60% 40% at 20% 10%,rgba(63,100,220,0.1) 0%,transparent 60%),
    radial-gradient(ellipse 50% 50% at 80% 80%,rgba(99,179,255,0.07) 0%,transparent 55%),var(--bg)}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;
  padding-bottom:16px;border-bottom:1px solid var(--border)}
.logo{font-family:var(--display);font-size:22px;font-weight:800;display:flex;align-items:center;gap:6px}
.logo .yt{color:var(--red)}.logo .arrow{color:var(--text3);font-size:16px}.logo .rt{color:var(--blue)}
.logo .badge{font-family:var(--mono);font-size:10px;background:rgba(255,77,106,0.15);
  color:var(--red);border:1px solid rgba(255,77,106,0.3);padding:2px 8px;border-radius:8px;margin-left:6px}
.header-nav{display:flex;align-items:center;gap:10px}
.nav-link{font-size:13px;color:var(--text2);text-decoration:none;padding:6px 14px;
  border:1px solid var(--border);border-radius:var(--r2);background:var(--glass);
  transition:all .2s;font-family:var(--sans)}
.nav-link:hover{color:var(--text);border-color:var(--border2)}
.nav-link.active{color:var(--blue);border-color:rgba(99,179,255,0.3);background:rgba(99,179,255,0.06)}
.btn-logout{font-size:12px;color:var(--red);border-color:rgba(255,77,106,0.25)}
.btn-logout:hover{background:rgba(255,77,106,0.08)}
.tabs{display:flex;gap:3px;margin-bottom:24px;background:var(--glass);
  border-radius:var(--r3);padding:5px;border:1px solid var(--border);backdrop-filter:var(--blur)}
.tab{flex:1;padding:9px 14px;border:none;background:none;color:var(--text2);
  cursor:pointer;font-family:var(--sans);font-size:13px;font-weight:500;
  border-radius:var(--r2);transition:all .2s}
.tab:hover{color:var(--text);background:var(--glass2)}
.tab.active{background:var(--glass2);color:var(--text);border:1px solid var(--border2);
  box-shadow:0 2px 12px rgba(0,0,0,0.3)}
.panel{display:none}.panel.active{display:block}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.stat-card{background:var(--glass);border:1px solid var(--border);border-radius:var(--r2);
  padding:20px;backdrop-filter:var(--blur);text-align:center}
.stat-num{font-family:var(--display);font-size:36px;font-weight:800;line-height:1;margin-bottom:6px}
.stat-lbl{font-size:11px;color:var(--text3);font-family:var(--mono);letter-spacing:.5px;text-transform:uppercase}
.card{background:var(--glass);border:1px solid var(--border);border-radius:var(--r3);
  padding:22px;margin-bottom:16px;backdrop-filter:var(--blur);box-shadow:var(--shadow)}
.card-title{font-size:11px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;
  color:var(--text3);margin-bottom:18px;font-family:var(--mono);display:flex;align-items:center;
  justify-content:space-between}
table{width:100%;border-collapse:collapse}
th{font-size:10px;font-family:var(--mono);letter-spacing:1px;text-transform:uppercase;
  color:var(--text3);padding:8px 12px;border-bottom:1px solid var(--border);text-align:left;font-weight:600}
td{padding:10px 12px;border-bottom:1px solid rgba(255,255,255,0.04);font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,0.02)}
.badge{display:inline-flex;align-items:center;font-size:10px;font-family:var(--mono);
  font-weight:600;padding:3px 10px;border-radius:10px;letter-spacing:.3px}
.badge-admin{background:rgba(255,77,106,0.12);color:var(--red);border:1px solid rgba(255,77,106,0.25)}
.badge-user{background:rgba(99,179,255,0.1);color:var(--blue);border:1px solid rgba(99,179,255,0.2)}
.badge-ok{background:rgba(52,224,154,0.1);color:var(--green);border:1px solid rgba(52,224,154,0.25)}
.badge-off{background:rgba(255,255,255,0.05);color:var(--text3);border:1px solid var(--border)}
.badge-run{background:rgba(255,214,102,0.1);color:var(--yellow);border:1px solid rgba(255,214,102,0.25)}
.badge-err{background:rgba(255,77,106,0.1);color:var(--red);border:1px solid rgba(255,77,106,0.25)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px}
.dot-green{background:var(--green);box-shadow:0 0 6px var(--greenglow)}
.dot-red{background:var(--red);box-shadow:0 0 6px var(--redglow)}
.dot-grey{background:var(--text3)}
.action-btn{font-size:11px;padding:4px 10px;border-radius:8px;border:1px solid var(--border);
  background:none;color:var(--text2);cursor:pointer;font-family:var(--sans);transition:all .15s}
.action-btn:hover{color:var(--text);border-color:var(--border2);background:var(--glass2)}
.action-btn.danger{color:var(--red);border-color:rgba(255,77,106,0.25)}
.action-btn.danger:hover{background:rgba(255,77,106,0.08)}
.action-btn.success{color:var(--green);border-color:rgba(52,224,154,0.25)}
.btn{display:inline-flex;align-items:center;gap:7px;padding:9px 18px;border-radius:var(--r2);
  font-family:var(--sans);font-size:13px;font-weight:500;cursor:pointer;border:none;transition:all .2s}
.btn-primary{background:linear-gradient(135deg,var(--blue),#3a8ee0);color:#fff;
  box-shadow:0 4px 14px rgba(99,179,255,0.25)}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(99,179,255,0.35)}
.btn-red{background:linear-gradient(135deg,var(--red),#c82848);color:#fff;
  box-shadow:0 4px 14px rgba(255,77,106,0.25)}
input[type=text],input[type=email],input[type=password],select{
  background:rgba(0,0,0,0.25);border:1px solid var(--border);color:var(--text);
  padding:9px 14px;border-radius:var(--r2);font-family:var(--sans);font-size:13px;
  outline:none;transition:border-color .15s,box-shadow .15s;-webkit-appearance:none}
input:focus,select:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(99,179,255,0.1)}
.form-row{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:14px}
.form-row input,.form-row select{flex:1;min-width:140px}
.prog-bar{height:4px;background:rgba(255,255,255,0.06);border-radius:4px;overflow:hidden;margin-top:4px}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--purple));border-radius:4px;transition:width .4s}
.prog-fill.done{background:linear-gradient(90deg,var(--green),#6effc0)}
.prog-fill.error{background:var(--red)}
.system-row{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)}
.system-row:last-child{border-bottom:none}
.empty{text-align:center;padding:40px;color:var(--text3);font-size:13px}
.toast{position:fixed;bottom:24px;right:24px;background:var(--glass2);border:1px solid var(--border2);
  border-radius:var(--r2);padding:12px 18px;font-size:13px;backdrop-filter:var(--blur);
  box-shadow:0 8px 24px rgba(0,0,0,0.4);opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}
.toast.show{opacity:1}
.toast.ok{border-color:rgba(52,224,154,0.35);color:var(--green)}
.toast.err{border-color:rgba(255,77,106,0.35);color:var(--red)}
@media(max-width:640px){.stats-grid{grid-template-columns:1fr 1fr}.form-row{flex-direction:column}}
</style>
</head>
<body>
<div class="header">
  <div class="logo">
    <span class="yt">YT</span><span class="arrow">→</span><span class="rt">Rutube</span>
    <span class="badge">ADMIN</span>
  </div>
  <div class="header-nav">
    <a class="nav-link" href="/">← Приложение</a>
    <a class="nav-link btn-logout" href="/logout">Выйти</a>
  </div>
</div>

<div class="tabs">
  <button class="tab active" onclick="switchTab('overview',this)">📊 Обзор</button>
  <button class="tab" onclick="switchTab('users',this)">👤 Пользователи</button>
  <button class="tab" onclick="switchTab('jobs',this)">⚙ Задачи</button>
  <button class="tab" onclick="switchTab('history',this)">📜 История</button>
</div>

<!-- OVERVIEW -->
<div class="panel active" id="tab-overview">
  <div class="stats-grid" id="stats-grid">
    <div class="stat-card"><div class="stat-num" id="s-users" style="color:var(--blue)">—</div><div class="stat-lbl">Пользователей</div></div>
    <div class="stat-card"><div class="stat-num" id="s-active-users" style="color:var(--green)">—</div><div class="stat-lbl">Активных</div></div>
    <div class="stat-card"><div class="stat-num" id="s-history" style="color:var(--purple)">—</div><div class="stat-lbl">Всего переносов</div></div>
    <div class="stat-card"><div class="stat-num" id="s-ok" style="color:var(--green)">—</div><div class="stat-lbl">Успешных</div></div>
    <div class="stat-card"><div class="stat-num" id="s-active-jobs" style="color:var(--yellow)">—</div><div class="stat-lbl">Активных задач</div></div>
    <div class="stat-card"><div class="stat-num" id="s-queue" style="color:var(--orange)">—</div><div class="stat-lbl">В очереди</div></div>
  </div>
  <div class="card">
    <div class="card-title">Система</div>
    <div id="sys-deps"></div>
  </div>
</div>

<!-- USERS -->
<div class="panel" id="tab-users">
  <div class="card">
    <div class="card-title">Создать пользователя
      <button class="btn btn-primary" id="create-user-btn" onclick="toggleCreateForm()">+ Новый</button>
    </div>
    <div id="create-form" style="display:none;margin-bottom:20px;padding:16px;background:rgba(0,0,0,0.2);border-radius:var(--r2);border:1px solid var(--border)">
      <div class="form-row">
        <input type="text" id="new-username" placeholder="Логин">
        <input type="email" id="new-email" placeholder="Email">
        <input type="password" id="new-password" placeholder="Пароль">
        <select id="new-role"><option value="user">user</option><option value="admin">admin</option></select>
        <button class="btn btn-primary" onclick="createUser()">Создать</button>
      </div>
      <div id="create-status" style="font-size:12px;margin-top:6px;display:none"></div>
    </div>
    <table>
      <thead><tr><th>ID</th><th>Логин</th><th>Email</th><th>Роль</th><th>Статус</th><th>Создан</th><th>Задачи</th><th>Действия</th></tr></thead>
      <tbody id="users-tbody"><tr><td colspan="8" class="empty">Загрузка...</td></tr></tbody>
    </table>
  </div>
</div>

<!-- JOBS -->
<div class="panel" id="tab-jobs">
  <div class="card">
    <div class="card-title">Активные задачи
      <button class="action-btn" onclick="loadJobs()">↻ Обновить</button>
    </div>
    <div id="jobs-list"><div class="empty">Загрузка...</div></div>
  </div>
  <div style="text-align:right;margin-bottom:8px">
    <button class="action-btn danger" onclick="clearJobs()">Очистить завершённые</button>
  </div>
</div>

<!-- HISTORY -->
<div class="panel" id="tab-history">
  <div class="card">
    <div class="card-title">История переносов (последние 200)
      <button class="action-btn" onclick="loadHistory()">↻ Обновить</button>
    </div>
    <table>
      <thead><tr><th>Время</th><th>Пользователь</th><th>Название</th><th>Тип</th><th>Статус</th></tr></thead>
      <tbody id="history-tbody"><tr><td colspan="5" class="empty">Загрузка...</td></tr></tbody>
    </table>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function toast(msg,type='ok'){
  const t=document.getElementById('toast');
  t.textContent=msg;t.className='toast show '+(type==='ok'?'ok':'err');
  setTimeout(()=>t.className='toast',3000);
}
function switchTab(name,btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  if(btn) btn.classList.add('active');
  if(name==='users') loadUsers();
  if(name==='jobs') loadJobs();
  if(name==='history') loadHistory();
}
async function api(path,method='GET',body=null){
  const opts={method,headers:{'Content-Type':'application/json'}};
  if(body) opts.body=JSON.stringify(body);
  const r=await fetch(path,opts);
  return r.json();
}
async function loadStats(){
  const d=await api('/api/admin/stats');
  document.getElementById('s-users').textContent=d.users.total;
  document.getElementById('s-active-users').textContent=d.users.active;
  document.getElementById('s-history').textContent=d.jobs.total_history;
  document.getElementById('s-ok').textContent=d.jobs.success_history;
  document.getElementById('s-active-jobs').textContent=d.jobs.active_now;
  document.getElementById('s-queue').textContent=d.jobs.queue_size;
  document.getElementById('sys-deps').innerHTML=`
    <div class="system-row"><span class="dot ${d.system.ytdlp?'dot-green':'dot-red'}"></span>
      <span>yt-dlp</span><span class="badge ${d.system.ytdlp?'badge-ok':'badge-err'}" style="margin-left:auto">${d.system.ytdlp?'OK':'MISSING'}</span></div>
    <div class="system-row"><span class="dot ${d.system.ffmpeg?'dot-green':'dot-red'}"></span>
      <span>ffmpeg</span><span class="badge ${d.system.ffmpeg?'badge-ok':'badge-err'}" style="margin-left:auto">${d.system.ffmpeg?'OK':'MISSING'}</span></div>
    <div class="system-row"><span class="dot dot-green"></span>
      <span>WebSocket клиентов: ${d.system.ws_clients}</span></div>`;
}
async function loadUsers(){
  const users=await api('/api/admin/users');
  if(!users.length){
    document.getElementById('users-tbody').innerHTML='<tr><td colspan="8" class="empty">Пользователей нет</td></tr>';return;
  }
  document.getElementById('users-tbody').innerHTML=users.map(u=>`
    <tr id="urow-${u.id}">
      <td><span style="font-family:var(--mono);color:var(--text3)">#${u.id}</span></td>
      <td><strong>${esc(u.username)}</strong></td>
      <td style="color:var(--text2)">${esc(u.email)}</td>
      <td><span class="badge ${u.role==='admin'?'badge-admin':'badge-user'}">${u.role}</span></td>
      <td><span class="dot ${u.is_active?'dot-green':'dot-grey'}"></span>${u.is_active?'Активен':'Отключён'}</td>
      <td style="color:var(--text3);font-size:12px">${(u.created_at||'').slice(0,10)}</td>
      <td style="color:var(--text3)">${u.job_count||0}</td>
      <td>
        <div style="display:flex;gap:5px;flex-wrap:wrap">
          <button class="action-btn ${u.is_active?'danger':'success'}" onclick="toggleUser(${u.id},${u.is_active})">${u.is_active?'Откл':'Вкл'}</button>
          <button class="action-btn" onclick="changeRole(${u.id},'${u.role}')">${u.role==='admin'?'→ user':'→ admin'}</button>
          <button class="action-btn" onclick="resetPwd(${u.id})">Пароль</button>
          <button class="action-btn danger" onclick="deleteUser(${u.id},'${esc(u.username)}')">Удалить</button>
        </div>
      </td>
    </tr>`).join('');
}
async function toggleUser(uid,isActive){
  const d=await api(`/api/admin/users/${uid}/toggle`,'POST');
  if(d.ok){toast(d.is_active?'Пользователь активирован':'Пользователь отключён');loadUsers();}
  else toast(d.error||'Ошибка','err');
}
async function changeRole(uid,currentRole){
  const newRole=currentRole==='admin'?'user':'admin';
  if(!confirm(`Изменить роль на ${newRole}?`)) return;
  const d=await api(`/api/admin/users/${uid}/role`,'POST',{role:newRole});
  if(d.ok){toast(`Роль изменена на ${newRole}`);loadUsers();}
  else toast(d.error||'Ошибка','err');
}
async function resetPwd(uid){
  const newPwd=prompt('Новый пароль (минимум 6 символов):');
  if(!newPwd||newPwd.length<6){toast('Пароль слишком короткий','err');return;}
  const d=await api(`/api/admin/users/${uid}/reset_password`,'POST',{password:newPwd});
  if(d.ok) toast('Пароль изменён');
  else toast(d.error||'Ошибка','err');
}
async function deleteUser(uid,username){
  if(!confirm(`Удалить пользователя "${username}"? Это действие необратимо.`)) return;
  const d=await api(`/api/admin/users/${uid}`,'DELETE');
  if(d.ok){toast('Пользователь удалён');loadUsers();}
  else toast(d.error||'Ошибка','err');
}
function toggleCreateForm(){
  const f=document.getElementById('create-form');
  f.style.display=f.style.display==='none'?'block':'none';
}
async function createUser(){
  const username=document.getElementById('new-username').value.trim();
  const email=document.getElementById('new-email').value.trim();
  const password=document.getElementById('new-password').value;
  const role=document.getElementById('new-role').value;
  const st=document.getElementById('create-status');
  if(!username||!email||!password){st.style.display='block';st.style.color='var(--red)';st.textContent='Заполните все поля';return;}
  const d=await api('/api/admin/users/create','POST',{username,email,password,role});
  if(d.ok){
    toast('Пользователь создан');
    document.getElementById('new-username').value='';
    document.getElementById('new-email').value='';
    document.getElementById('new-password').value='';
    st.style.display='none';
    loadUsers();
  } else {
    st.style.display='block';st.style.color='var(--red)';st.textContent=d.error||'Ошибка';
  }
}
async function loadJobs(){
  const jobs=await api('/api/admin/jobs');
  if(!jobs.length){document.getElementById('jobs-list').innerHTML='<div class="empty">Нет активных задач</div>';return;}
  const modeIcon={transfer:'📹',dub:'🎙',shorts:'✂'};
  const statusBadge={done:'badge-ok',error:'badge-err',running:'badge-run',queued:'badge-off',starting:'badge-run'};
  document.getElementById('jobs-list').innerHTML=jobs.map(j=>`
    <div style="background:rgba(0,0,0,0.15);border:1px solid var(--border);border-radius:var(--r2);padding:14px 18px;margin-bottom:8px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <span>${modeIcon[j.mode]||'📹'}</span>
        <span style="font-size:13px;font-weight:500;flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${esc(j.title||j.job_id)}</span>
        <span class="badge ${statusBadge[j.status]||'badge-off'}">${esc(j.status)}</span>
        <span style="font-size:11px;color:var(--text3);font-family:var(--mono)">uid:${j.user_id}</span>
      </div>
      <div class="prog-bar"><div class="prog-fill ${j.status==='done'?'done':j.status==='error'?'error':''}" style="width:${j.progress}%"></div></div>
      ${j.error?`<div style="font-size:11px;color:var(--red);margin-top:6px">${esc(j.error)}</div>`:''}
    </div>`).join('');
}
async function clearJobs(){
  const d=await api('/api/admin/clear_jobs','POST');
  if(d.ok){toast(`Удалено ${d.removed} завершённых задач`);loadJobs();}
}
async function loadHistory(){
  const hist=await api('/api/admin/history');
  if(!hist.length){document.getElementById('history-tbody').innerHTML='<tr><td colspan="5" class="empty">История пуста</td></tr>';return;}
  const modeLabel={transfer:'Перенос',dub:'Дубляж',shorts:'Shorts'};
  document.getElementById('history-tbody').innerHTML=hist.map(h=>`
    <tr>
      <td style="font-family:var(--mono);font-size:11px;color:var(--text3)">${esc(h.ts)}</td>
      <td><span style="color:var(--blue)">${esc(h.username||'?')}</span></td>
      <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.title||h.youtube_url||'—')}</td>
      <td><span class="badge badge-user">${modeLabel[h.mode]||h.mode}</span></td>
      <td><span class="dot ${h.status==='success'?'dot-green':'dot-red'}"></span>${esc(h.status)}</td>
    </tr>`).join('');
}
loadStats();
setInterval(loadStats, 10000);
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════
# HTML — MAIN APPLICATION
# ══════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT → Rutube Transfer</title>
<!-- USER_INJECT -->
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect rx='18' width='100' height='100' fill='%2307080f'/><text x='12' y='68' font-size='48' font-weight='bold' fill='%23ff4d6a'>Y</text><text x='55' y='68' font-size='48' font-weight='bold' fill='%2363b3ff'>R</text></svg>">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=JetBrains+Mono:wght@400;500&family=Outfit:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#07080f;--surface:rgba(255,255,255,0.04);--glass:rgba(255,255,255,0.05);
  --glass2:rgba(255,255,255,0.08);--glass3:rgba(255,255,255,0.11);
  --border:rgba(255,255,255,0.08);--border2:rgba(255,255,255,0.14);
  --border-accent:rgba(99,179,255,0.3);
  --text:#f0f1ff;--text2:#9098c0;--text3:#464d72;
  --red:#ff4d6a;--red2:#ff7a90;--redglow:rgba(255,77,106,0.18);
  --blue:#63b3ff;--blue2:#93ccff;--blueglow:rgba(99,179,255,0.15);
  --green:#34e09a;--green2:#6effc0;--greenglow:rgba(52,224,154,0.15);
  --yellow:#ffd666;--yellowglow:rgba(255,214,102,0.15);
  --purple:#b39dff;--purpleglow:rgba(179,157,255,0.15);
  --orange:#ff8c42;
  --mono:'JetBrains Mono',monospace;--sans:'Outfit',sans-serif;--display:'Syne',sans-serif;
  --r:10px;--r2:16px;--r3:22px;
  --blur:blur(20px) saturate(180%);--shadow:0 8px 32px rgba(0,0,0,0.4);
}
html{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh}
body{max-width:1040px;margin:0 auto;padding:28px 20px 80px;
  background:
    radial-gradient(ellipse 60% 40% at 20% 10%,rgba(63,100,220,0.12) 0%,transparent 60%),
    radial-gradient(ellipse 50% 50% at 85% 80%,rgba(99,179,255,0.08) 0%,transparent 55%),
    var(--bg);min-height:100vh}
/* ── Header ── */
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:32px;
  padding-bottom:18px;border-bottom:1px solid var(--border)}
.logo{font-family:var(--display);font-size:22px;font-weight:800;display:flex;align-items:center;gap:6px}
.logo .yt{color:var(--red);text-shadow:0 0 20px var(--redglow)}
.logo .arrow{color:var(--text3);font-size:16px;margin:0 2px}
.logo .rt{color:var(--blue);text-shadow:0 0 20px var(--blueglow)}
.header-right{display:flex;align-items:center;gap:8px}
.user-chip{display:flex;align-items:center;gap:7px;background:var(--glass);
  border:1px solid var(--border);border-radius:20px;padding:5px 12px 5px 8px;font-size:12px}
.user-chip .avatar{width:24px;height:24px;border-radius:50%;background:linear-gradient(135deg,var(--blue),var(--purple));
  display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#fff}
.chip-btn{font-size:12px;padding:5px 12px;border:1px solid var(--border);border-radius:20px;
  background:var(--glass);color:var(--text2);cursor:pointer;text-decoration:none;
  font-family:var(--sans);transition:all .2s}
.chip-btn:hover{color:var(--text);border-color:var(--border2);background:var(--glass2)}
.chip-btn.admin{color:var(--red);border-color:rgba(255,77,106,0.25)}
.chip-btn.admin:hover{background:rgba(255,77,106,0.08)}
.chip-btn.logout{color:var(--text3)}
/* ── Nav ── */
.nav{display:flex;gap:3px;margin-bottom:24px;background:var(--glass);border-radius:var(--r3);
  padding:5px;border:1px solid var(--border);backdrop-filter:var(--blur);flex-wrap:wrap}
.nav-btn{flex:1;min-width:80px;padding:9px 12px;border:none;background:none;color:var(--text2);
  cursor:pointer;font-family:var(--sans);font-size:13px;font-weight:500;
  border-radius:var(--r2);transition:all .2s;white-space:nowrap}
.nav-btn:hover{color:var(--text);background:var(--glass2)}
.nav-btn.active{background:var(--glass2);color:var(--text);border:1px solid var(--border2);
  box-shadow:0 2px 12px rgba(0,0,0,0.3)}
/* ── Panels ── */
.panel{display:none;animation:fadeIn .25s ease}.panel.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
/* ── Cards ── */
.card{background:var(--glass);border:1px solid var(--border);border-radius:var(--r3);
  padding:22px;margin-bottom:14px;backdrop-filter:var(--blur);box-shadow:var(--shadow);
  position:relative;overflow:hidden}
.card::before{content:'';position:absolute;inset:0;border-radius:var(--r3);
  background:linear-gradient(135deg,rgba(255,255,255,0.04) 0%,transparent 50%);pointer-events:none}
.card-label{font-family:var(--mono);font-size:10px;letter-spacing:1.5px;text-transform:uppercase;
  color:var(--text3);margin-bottom:14px;display:flex;align-items:center;gap:6px}
.card-label-dot{width:5px;height:5px;border-radius:50%;background:var(--blue);box-shadow:0 0 8px var(--blue)}
/* ── Inputs ── */
.input-group{margin-bottom:14px}
.input-group label{display:block;font-size:12px;color:var(--text2);margin-bottom:6px;font-weight:500}
input[type=text],input[type=password],textarea,select{
  width:100%;background:rgba(0,0,0,0.25);border:1px solid var(--border);color:var(--text);
  padding:11px 15px;border-radius:var(--r2);font-family:var(--sans);font-size:14px;
  transition:border-color .15s,box-shadow .15s;outline:none;-webkit-appearance:none}
input:focus,textarea:focus,select:focus{border-color:var(--blue);
  box-shadow:0 0 0 3px rgba(99,179,255,0.12);background:rgba(99,179,255,0.03)}
textarea{resize:vertical;min-height:90px;font-family:var(--mono);font-size:12px;line-height:1.7}
select option{background:#12141f}
/* ── URL input ── */
.url-hero{position:relative;margin-bottom:8px}
.url-hero input{font-size:15px;padding:15px 52px 15px 17px;border-radius:var(--r2);
  background:rgba(0,0,0,0.35);border:1px solid var(--border2)}
.url-hero input::placeholder{color:var(--text3)}
.url-hero .paste-btn{position:absolute;right:12px;top:50%;transform:translateY(-50%);
  background:var(--glass2);border:1px solid var(--border);border-radius:var(--r);
  color:var(--text2);cursor:pointer;font-size:13px;padding:5px 9px;transition:all .15s}
.url-hero .paste-btn:hover{color:var(--text);background:var(--glass3)}
.url-hint{font-size:11px;margin-top:5px;height:15px;font-family:var(--mono);transition:all .2s}
.url-hint.valid{color:var(--green)}.url-hint.invalid{color:var(--red)}
input.url-valid{border-color:var(--green)!important;box-shadow:0 0 0 3px rgba(52,224,154,0.1)!important}
input.url-invalid{border-color:var(--red)!important;box-shadow:0 0 0 3px rgba(255,77,106,0.1)!important}
/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:7px;padding:10px 20px;border-radius:var(--r2);
  font-family:var(--sans);font-size:14px;font-weight:500;cursor:pointer;border:none;transition:all .2s}
.btn-primary{background:linear-gradient(135deg,var(--red),#c82848);color:#fff;
  box-shadow:0 4px 14px rgba(255,77,106,0.3)}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 22px rgba(255,77,106,0.4)}
.btn-primary:disabled{opacity:.35;cursor:not-allowed;transform:none;box-shadow:none}
.btn-secondary{background:var(--glass2);color:var(--text);border:1px solid var(--border2);backdrop-filter:var(--blur)}
.btn-secondary:hover{border-color:var(--blue);color:var(--blue)}
.btn-ghost{background:none;color:var(--text2);border:1px solid var(--border);transition:all .2s}
.btn-ghost:hover{color:var(--text);border-color:var(--border2);background:var(--glass)}
.btn-blue{background:linear-gradient(135deg,var(--blue),#3a8ee0);color:#fff;
  box-shadow:0 4px 14px rgba(99,179,255,0.25)}
.btn-blue:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(99,179,255,0.35)}
.btn-blue:disabled{opacity:.35;cursor:not-allowed;transform:none;box-shadow:none}
/* ── Progress ── */
.progress-wrap{margin:14px 0}
.progress-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:7px}
.progress-label{font-size:12px;font-weight:500;color:var(--text2)}
.progress-pct{font-family:var(--mono);font-size:12px;color:var(--blue);font-weight:500}
.progress-bar{height:3px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--purple));
  border-radius:3px;transition:width .4s cubic-bezier(.4,0,.2,1);box-shadow:0 0 8px var(--blueglow)}
.progress-fill.done{background:linear-gradient(90deg,var(--green),var(--green2));box-shadow:0 0 8px var(--greenglow)}
.progress-fill.error{background:var(--red);box-shadow:0 0 8px var(--redglow)}
/* ── Stages ── */
.stage-row{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0 14px}
.stage{font-size:11px;padding:4px 12px;border-radius:20px;font-weight:500;
  font-family:var(--mono);letter-spacing:.5px;
  background:rgba(255,255,255,0.04);color:var(--text3);border:1px solid var(--border);transition:all .3s}
.stage.active{background:rgba(99,179,255,0.1);color:var(--blue);border-color:rgba(99,179,255,0.3);
  animation:stagePulse 1.5s ease-in-out infinite}
.stage.done{background:rgba(52,224,154,0.1);color:var(--green);border-color:rgba(52,224,154,0.3)}
.stage.error{background:var(--redglow);color:var(--red);border-color:rgba(255,77,106,0.3)}
@keyframes stagePulse{0%,100%{box-shadow:0 0 8px rgba(99,179,255,0.1)}50%{box-shadow:0 0 18px rgba(99,179,255,0.3)}}
/* ── Log ── */
.log-wrap{background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:var(--r2);
  padding:12px;max-height:180px;overflow-y:auto;font-family:var(--mono);font-size:11.5px;line-height:1.8}
.log-line{display:flex;gap:10px;padding:1px 0}
.log-line .ts{color:var(--text3);flex-shrink:0;font-size:10.5px}
.log-line.ok .msg{color:var(--green)}.log-line.error .msg{color:var(--red)}
.log-line.warn .msg{color:var(--yellow)}.log-line.info .msg{color:var(--text2)}
/* ── Meta grid ── */
.meta-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
.meta-item{background:rgba(0,0,0,0.2);border-radius:var(--r);padding:10px 13px;border:1px solid var(--border)}
.meta-item .key{font-size:10px;color:var(--text3);margin-bottom:3px;font-family:var(--mono);
  letter-spacing:.5px;text-transform:uppercase}
.meta-item .val{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* ── Result banner ── */
.result-banner{background:rgba(52,224,154,0.06);border:1px solid rgba(52,224,154,0.25);
  border-radius:var(--r2);padding:15px 18px;display:flex;align-items:center;gap:12px;
  box-shadow:0 0 20px rgba(52,224,154,0.08)}
.result-banner.error-banner{background:rgba(255,77,106,0.06);border-color:rgba(255,77,106,0.25);
  box-shadow:0 0 20px rgba(255,77,106,0.08)}
.result-url{font-family:var(--mono);font-size:12px;color:var(--green2);word-break:break-all}
.result-url a{color:inherit;text-decoration:none;border-bottom:1px solid rgba(52,224,154,0.3)}
/* ── History ── */
.history-item{background:var(--glass);border:1px solid var(--border);border-radius:var(--r2);
  padding:12px 16px;margin-bottom:8px;display:flex;align-items:center;gap:12px;
  backdrop-filter:var(--blur);transition:border-color .15s}
.history-item:hover{border-color:var(--border2)}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.success{background:var(--green);box-shadow:0 0 8px var(--greenglow)}
.dot.error-dot{background:var(--red);box-shadow:0 0 8px var(--redglow)}
.history-info{flex:1;min-width:0}
.history-title{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.history-meta{font-size:11px;color:var(--text3);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.history-ts{font-family:var(--mono);font-size:11px;color:var(--text3);flex-shrink:0}
/* ── Settings ── */
.settings-section{margin-bottom:24px}
.settings-section h3{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;
  color:var(--text3);margin-bottom:12px;font-weight:600;font-family:var(--mono);
  display:flex;align-items:center;gap:8px}
.settings-section h3::after{content:'';flex:1;height:1px;background:var(--border)}
.toggle-row{display:flex;align-items:center;justify-content:space-between;
  padding:11px 0;border-bottom:1px solid var(--border)}
.toggle-row label{font-size:14px;color:var(--text2)}
.toggle{position:relative;width:40px;height:22px}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:rgba(255,255,255,0.08);border-radius:11px;
  cursor:pointer;transition:.25s;border:1px solid var(--border)}
.slider:before{content:'';position:absolute;left:4px;top:4px;width:12px;height:12px;
  background:var(--text3);border-radius:50%;transition:.25s}
.toggle input:checked+.slider{background:rgba(99,179,255,0.15);border-color:rgba(99,179,255,0.4)}
.toggle input:checked+.slider:before{transform:translateX(18px);background:var(--blue);
  box-shadow:0 0 8px var(--blueglow)}
/* ── Dep check ── */
.dep-item{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--border)}
.dep-ok{background:var(--greenglow);color:var(--green);border:1px solid rgba(52,224,154,0.25);
  font-size:11px;font-weight:600;padding:2px 9px;border-radius:6px;font-family:var(--mono)}
.dep-missing{background:var(--redglow);color:var(--red);border:1px solid rgba(255,77,106,0.25);
  font-size:11px;font-weight:600;padding:2px 9px;border-radius:6px;font-family:var(--mono)}
/* ── Dashboard ── */
.dash-stat{text-align:center;background:var(--glass);border:1px solid var(--border);
  border-radius:var(--r2);padding:18px 10px}
.dash-num{font-family:var(--display);font-size:34px;font-weight:800;line-height:1;margin-bottom:4px}
.dash-lbl{font-size:10px;color:var(--text3);font-family:var(--mono);letter-spacing:.5px;text-transform:uppercase}
/* ── Cookie area ── */
.cookie-area{width:100%;background:rgba(0,0,0,0.35);border:1px solid var(--border);
  color:var(--text2);padding:11px 13px;border-radius:var(--r2);font-family:var(--mono);
  font-size:11px;line-height:1.6;resize:vertical;min-height:110px;outline:none;
  transition:border-color .15s}
.cookie-area:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(99,179,255,0.1)}
.cookie-saved{font-size:12px;color:var(--green);display:none;font-family:var(--mono)}
/* ── Batch ── */
.batch-stat .num{font-family:var(--display);font-size:26px;font-weight:800;line-height:1}
.batch-stat .lbl{font-size:10px;color:var(--text3);font-family:var(--mono);letter-spacing:.5px;text-transform:uppercase;margin-top:3px}
/* ── Preview card ── */
.preview-card{display:none;margin-top:12px;background:rgba(0,0,0,0.2);
  border:1px solid var(--border2);border-radius:var(--r2);overflow:hidden;animation:fadeIn .3s ease}
.preview-card.visible{display:block}
.preview-inner{display:flex;gap:14px;padding:14px}
.preview-thumb{width:160px;height:90px;flex-shrink:0;border-radius:var(--r);
  background:var(--surface);overflow:hidden;position:relative}
.preview-thumb img{width:100%;height:100%;object-fit:cover}
.preview-thumb .dur-badge{position:absolute;bottom:5px;right:5px;background:rgba(0,0,0,0.8);
  color:#fff;font-family:var(--mono);font-size:10px;padding:2px 5px;border-radius:4px}
.preview-info{flex:1;min-width:0;display:flex;flex-direction:column;gap:5px}
.preview-title{font-size:13px;font-weight:600;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.preview-channel{font-size:12px;color:var(--text2)}
.preview-stats{display:flex;gap:12px;font-size:11px;color:var(--text3);font-family:var(--mono);margin-top:auto}
.preview-loading{padding:18px;text-align:center;font-size:12px;color:var(--text3);font-family:var(--mono)}
.preview-loading .spinner{display:inline-block;width:13px;height:13px;
  border:2px solid var(--border2);border-top-color:var(--blue);border-radius:50%;
  animation:spin .7s linear infinite;vertical-align:middle;margin-right:7px}
@keyframes spin{to{transform:rotate(360deg)}}
/* ── Empty state ── */
.empty-state{text-align:center;padding:48px 24px;color:var(--text3)}
.empty-icon{width:80px;height:80px;margin:0 auto 20px;background:var(--surface);
  border:1.5px dashed var(--border2);border-radius:50%;display:flex;align-items:center;
  justify-content:center;animation:emptyFloat 4s ease-in-out infinite}
@keyframes emptyFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
.empty-title{font-family:var(--display);font-size:17px;font-weight:700;color:var(--text2);margin-bottom:7px}
.empty-desc{font-size:13px;line-height:1.6;max-width:360px;margin:0 auto}
/* ── Monitor channel list ── */
.mon-channel{display:flex;align-items:center;gap:10px;padding:11px 0;border-bottom:1px solid var(--border)}
.mon-channel:last-child{border-bottom:none}
/* ── Scrollbar ── */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px}
/* ── Drag drop ── */
.drop-zone{position:relative;transition:all .3s}
.drop-zone.drag-over{border-color:var(--blue)!important;background:rgba(99,179,255,0.05)!important;
  box-shadow:0 0 28px rgba(99,179,255,0.12)!important}
@media(max-width:640px){
  .meta-grid{grid-template-columns:1fr}.preview-inner{flex-direction:column}
  .preview-thumb{width:100%;height:auto;aspect-ratio:16/9}
  .nav{flex-direction:row;flex-wrap:wrap}
  .nav-btn{min-width:calc(50% - 6px)}
}
</style>
</head>
<body>
<div class="header">
  <div class="logo">
    <span class="yt">YT</span><span class="arrow">→</span><span class="rt">Rutube</span>
  </div>
  <div class="header-right" id="header-right">
    <div class="user-chip">
      <div class="avatar" id="user-avatar">?</div>
      <span id="user-name" style="font-size:13px;color:var(--text2)">...</span>
    </div>
    <a class="chip-btn admin" href="/admin" id="admin-link" style="display:none">⚙ Админ</a>
    <a class="chip-btn logout" href="/logout">Выйти</a>
  </div>
</div>
<div class="nav">
  <button class="nav-btn active" onclick="switchTab('transfer',this)">📹 Перенос</button>
  <button class="nav-btn" onclick="switchTab('dub',this)">🎙 Дубляж</button>
  <button class="nav-btn" onclick="switchTab('shorts',this)">✂ Shorts</button>
  <button class="nav-btn" onclick="switchTab('dashboard',this)">📊 Live</button>
  <button class="nav-btn" onclick="switchTab('batch',this)">📦 Пакет</button>
  <button class="nav-btn" onclick="switchTab('history',this)">📜 История</button>
  <button class="nav-btn" onclick="switchTab('monitor',this)">👁 Мониторинг</button>
  <button class="nav-btn" onclick="switchTab('settings',this)">⚙ Настройки</button>
</div>
<!-- ═══ TRANSFER ═══ -->
<div class="panel active" id="tab-transfer">
<div class="card drop-zone" id="drop-zone">
  <div class="card-label"><span class="card-label-dot"></span>YouTube URL</div>
  <div class="url-hero">
    <input type="text" id="yt-url" placeholder="https://youtube.com/watch?v=..."
      onkeydown="if(event.key==='Enter')startTransfer()" oninput="validateUrl()">
    <button class="paste-btn" onclick="pasteUrl()" title="Вставить">📋</button>
  </div>
  <div class="url-hint" id="url-hint"></div>
  <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
    <button class="btn btn-primary" id="start-btn" onclick="startTransfer()">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
      Начать перенос
    </button>
    <button class="btn btn-ghost" id="preview-btn" onclick="fetchPreview()" style="display:none">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/></svg>
      Предпросмотр
    </button>
    <button class="btn btn-ghost" onclick="clearUrl()">Очистить</button>
  </div>
  <div class="preview-card" id="preview-card">
    <div class="preview-loading" id="preview-loading">
      <span class="spinner"></span>Загрузка метаданных...
    </div>
    <div class="preview-inner" id="preview-inner" style="display:none">
      <div class="preview-thumb">
        <img id="preview-img" src="" alt="">
        <span class="dur-badge" id="preview-dur">0:00</span>
      </div>
      <div class="preview-info">
        <div class="preview-title" id="preview-title"></div>
        <div class="preview-channel" id="preview-channel"></div>
        <div class="preview-stats">
          <span id="preview-views">👁 —</span>
          <span id="preview-likes">👍 —</span>
          <span id="preview-tags">🏷 —</span>
        </div>
      </div>
    </div>
  </div>
</div>
<div class="card" id="empty-state">
  <div class="empty-state">
    <div class="empty-icon">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--text3)" stroke-width="1.2">
        <path d="M15 5.5L18 3l3 3-2.5 2.5M9 12l6.5-6.5M3 21l3.5-1L18 8.5 15.5 6 4 17.5 3 21z"/>
      </svg>
    </div>
    <div class="empty-title">Перенеси видео на Rutube</div>
    <div class="empty-desc">Вставь ссылку YouTube или перетащи её сюда. Мы перенесём видео со всеми метаданными, тегами и обложкой.</div>
  </div>
</div>
<div class="card" id="job-card" style="display:none">
  <div class="stage-row">
    <span class="stage" id="s-queued">очередь</span>
    <span class="stage" id="s-metadata">метаданные</span>
    <span class="stage" id="s-download">загрузка</span>
    <span class="stage" id="s-upload">выгрузка</span>
  </div>
  <div class="progress-wrap">
    <div class="progress-header">
      <span class="progress-label" id="prog-label">...</span>
      <span class="progress-pct" id="prog-pct">0%</span>
    </div>
    <div class="progress-bar"><div class="progress-fill" id="prog-fill" style="width:0%"></div></div>
  </div>
  <div id="meta-block"></div>
  <div class="log-wrap" id="log-wrap"></div>
  <div id="result-block" style="margin-top:12px"></div>
</div>
</div>

<!-- ═══ DUB ═══ -->
<div class="panel" id="tab-dub">
<div class="card">
  <div class="card-label"><span class="card-label-dot" style="background:var(--orange)"></span>🎙 AI-Дубляж</div>
  <div style="font-size:13px;color:var(--text2);margin-bottom:14px">Whisper транскрибирует → Google Translate переводит → Edge-TTS озвучивает</div>
  <div id="dub-deps-bar" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px"></div>
  <div class="url-hero">
    <input type="text" id="dub-url" placeholder="https://youtube.com/watch?v=..." onkeydown="if(event.key==='Enter')startDub()">
    <button class="paste-btn" onclick="navigator.clipboard.readText().then(t=>document.getElementById('dub-url').value=t)">📋</button>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0">
    <div class="input-group" style="margin-bottom:0">
      <label>Язык оригинала</label>
      <select id="dub-src"><option value="en" selected>English</option><option value="es">Español</option><option value="de">Deutsch</option><option value="fr">Français</option><option value="zh">中文</option><option value="ja">日本語</option><option value="ko">한국어</option></select>
    </div>
    <div class="input-group" style="margin-bottom:0">
      <label>Язык дубляжа</label>
      <select id="dub-tgt"><option value="ru" selected>Русский</option><option value="en">English</option><option value="es">Español</option><option value="de">Deutsch</option></select>
    </div>
    <div class="input-group" style="margin-bottom:0">
      <label>Модель Whisper</label>
      <select id="dub-model"><option value="tiny">tiny (быстро)</option><option value="base" selected>base (баланс)</option><option value="small">small (лучше)</option><option value="medium">medium (точно)</option></select>
    </div>
    <div class="input-group" style="margin-bottom:0">
      <label>Громкость оригинала</label>
      <select id="dub-mix"><option value="0">0% — только дубляж</option><option value="0.1">10%</option><option value="0.15" selected>15% — лёгкий фон</option><option value="0.25">25%</option><option value="0.4">40%</option></select>
    </div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <button class="btn btn-primary" id="dub-start-btn" onclick="startDub()" style="background:linear-gradient(135deg,var(--orange),#e06020)">
      🎙 Дубляж + скачать
    </button>
    <button class="btn btn-ghost" onclick="installDubDeps()" id="dub-install-btn">📦 Установить зависимости</button>
  </div>
  <div id="dub-install-status" style="margin-top:8px;font-size:12px;display:none"></div>
</div>
<div class="card" id="dub-job-card" style="display:none">
  <div class="stage-row">
    <span class="stage" id="ds-metadata">метаданные</span>
    <span class="stage" id="ds-download">загрузка</span>
    <span class="stage" id="ds-dubbing">🎙 дубляж</span>
    <span class="stage" id="ds-done">готово</span>
  </div>
  <div class="progress-wrap">
    <div class="progress-header"><span class="progress-label" id="dub-prog-label">...</span><span class="progress-pct" id="dub-prog-pct">0%</span></div>
    <div class="progress-bar"><div class="progress-fill" id="dub-prog-fill" style="width:0%"></div></div>
  </div>
  <div id="dub-meta"></div>
  <div class="log-wrap" id="dub-log"></div>
  <div id="dub-result" style="margin-top:12px"></div>
</div>
</div>

<!-- ═══ SHORTS ═══ -->
<div class="panel" id="tab-shorts">
<div class="card">
  <div class="card-label"><span class="card-label-dot" style="background:var(--purple)"></span>✂ Авто-нарезка Shorts</div>
  <div style="font-size:13px;color:var(--text2);margin-bottom:14px">Скачиваем → находим лучшие моменты → нарезаем вертикальные клипы 9:16</div>
  <div class="url-hero">
    <input type="text" id="shorts-url" placeholder="https://youtube.com/watch?v=..." onkeydown="if(event.key==='Enter')startShorts()">
    <button class="paste-btn" onclick="navigator.clipboard.readText().then(t=>document.getElementById('shorts-url').value=t)">📋</button>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin:12px 0">
    <div class="input-group" style="margin-bottom:0">
      <label>Кол-во</label>
      <select id="shorts-n"><option value="1">1</option><option value="2">2</option><option value="3" selected>3</option><option value="5">5</option><option value="10">10</option></select>
    </div>
    <div class="input-group" style="margin-bottom:0">
      <label>Длительность</label>
      <select id="shorts-dur"><option value="15">15с</option><option value="30">30с</option><option value="45">45с</option><option value="60" selected>60с</option><option value="90">90с</option></select>
    </div>
    <div class="input-group" style="margin-bottom:0">
      <label>Стратегия</label>
      <select id="shorts-strat"><option value="energy" selected>🔊 Пики</option><option value="scene">🎬 Сцены</option><option value="uniform">📏 Равном.</option></select>
    </div>
  </div>
  <button class="btn btn-primary" id="shorts-start-btn" onclick="startShorts()" style="background:linear-gradient(135deg,var(--purple),#8060e0)">
    ✂ Нарезать Shorts
  </button>
</div>
<div class="card" id="shorts-job-card" style="display:none">
  <div class="stage-row">
    <span class="stage" id="ss-metadata">метаданные</span>
    <span class="stage" id="ss-download">загрузка</span>
    <span class="stage" id="ss-cutting">✂ нарезка</span>
    <span class="stage" id="ss-done">готово</span>
  </div>
  <div class="progress-wrap">
    <div class="progress-header"><span class="progress-label" id="shorts-prog-label">...</span><span class="progress-pct" id="shorts-prog-pct">0%</span></div>
    <div class="progress-bar"><div class="progress-fill" id="shorts-prog-fill" style="width:0%"></div></div>
  </div>
  <div id="shorts-meta"></div>
  <div class="log-wrap" id="shorts-log"></div>
  <div id="shorts-result" style="margin-top:12px"></div>
</div>
</div>

<!-- ═══ DASHBOARD ═══ -->
<div class="panel" id="tab-dashboard">
<div class="card">
  <div class="card-label"><span class="card-label-dot" style="background:var(--green)"></span>Live Dashboard
    <span id="ws-status" style="margin-left:auto;font-size:11px;font-family:var(--mono);color:var(--text3)">connecting...</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:10px;margin-bottom:14px">
    <div class="dash-stat"><div class="dash-num" id="dash-total" style="color:var(--blue)">0</div><div class="dash-lbl">Всего</div></div>
    <div class="dash-stat"><div class="dash-num" id="dash-done" style="color:var(--green)">0</div><div class="dash-lbl">Готово</div></div>
    <div class="dash-stat"><div class="dash-num" id="dash-running" style="color:var(--yellow)">0</div><div class="dash-lbl">Активных</div></div>
    <div class="dash-stat"><div class="dash-num" id="dash-errors" style="color:var(--red)">0</div><div class="dash-lbl">Ошибок</div></div>
    <div class="dash-stat"><div class="dash-num" id="dash-queue" style="color:var(--purple)">0</div><div class="dash-lbl">Очередь</div></div>
  </div>
</div>
<div style="font-size:11px;color:var(--text3);font-family:var(--mono);letter-spacing:1px;text-transform:uppercase;margin-bottom:10px">🔴 АКТИВНЫЕ</div>
<div id="dash-active"></div>
<div style="font-size:11px;color:var(--text3);font-family:var(--mono);letter-spacing:1px;text-transform:uppercase;margin:14px 0 10px">ЗАВЕРШЁННЫЕ</div>
<div id="dash-recent"></div>
</div>

<!-- ═══ BATCH ═══ -->
<div class="panel" id="tab-batch">
<div class="card">
  <div class="card-label"><span class="card-label-dot" style="background:var(--purple)"></span>Пакетная загрузка</div>
  <div class="input-group">
    <label>По одному URL на строку</label>
    <textarea id="batch-urls" placeholder="https://youtube.com/watch?v=AAA&#10;https://youtube.com/watch?v=BBB&#10;https://youtu.be/CCC"></textarea>
  </div>
  <div id="batch-invalid" style="display:none;font-size:12px;color:var(--red);margin-bottom:10px;font-family:var(--mono)"></div>
  <button class="btn btn-primary" onclick="startBatch()">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z"/></svg>
    Запустить пакет
  </button>
</div>
<div id="batch-summary" style="display:none;background:var(--glass);border:1px solid var(--border);border-radius:var(--r2);padding:16px;margin-bottom:14px;backdrop-filter:var(--blur)">
  <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
    <div class="batch-stat" style="text-align:center;flex:1"><div class="num" id="bs-total" style="color:var(--blue)">0</div><div class="lbl">Всего</div></div>
    <div class="batch-stat" style="text-align:center;flex:1"><div class="num" id="bs-done" style="color:var(--green)">0</div><div class="lbl">Готово</div></div>
    <div class="batch-stat" style="text-align:center;flex:1"><div class="num" id="bs-active" style="color:var(--yellow)">0</div><div class="lbl">В работе</div></div>
    <div class="batch-stat" style="text-align:center;flex:1"><div class="num" id="bs-errors" style="color:var(--red)">0</div><div class="lbl">Ошибки</div></div>
    <div style="flex:2;min-width:120px">
      <div style="font-size:11px;color:var(--text3);font-family:var(--mono);display:flex;justify-content:space-between"><span>Прогресс</span><span id="bs-pct">0%</span></div>
      <div class="progress-bar" style="margin-top:6px"><div class="progress-fill" id="bs-fill" style="width:0%"></div></div>
    </div>
  </div>
</div>
<div id="batch-jobs"></div>
</div>

<!-- ═══ HISTORY ═══ -->
<div class="panel" id="tab-history">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
  <span style="font-family:var(--mono);font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text3)" id="hist-count">...</span>
  <button class="btn btn-ghost" style="font-size:12px;padding:6px 14px" onclick="loadHistory()">Обновить</button>
</div>
<div id="history-list"></div>
</div>

<!-- ═══ MONITOR ═══ -->
<div class="panel" id="tab-monitor">
<div class="card" style="margin-bottom:14px">
  <div class="card-label"><span class="card-label-dot" style="background:var(--green)"></span>Мониторинг каналов</div>
  <div style="font-size:13px;color:var(--text2);margin-bottom:14px">Автоматически отслеживает новые видео и переносит их на Rutube</div>
  <div class="toggle-row">
    <label>Включить мониторинг</label>
    <label class="toggle"><input type="checkbox" id="monitor-enabled" onchange="toggleMonitor()"><span class="slider"></span></label>
  </div>
  <div class="input-group" style="margin-top:12px">
    <label>Интервал проверки (минуты)</label>
    <input type="text" id="monitor-interval" value="15" placeholder="15" style="max-width:120px" onchange="saveMonitorInterval()">
  </div>
  <div id="monitor-status" style="margin-top:8px;font-size:12px;font-family:var(--mono);color:var(--text3)"></div>
</div>
<div class="card" style="margin-bottom:14px">
  <div class="card-label"><span class="card-label-dot" style="background:var(--purple)"></span>Добавить канал</div>
  <div class="input-group" style="margin-bottom:10px">
    <label>Ссылка или @handle</label>
    <input type="text" id="monitor-channel-url" placeholder="https://youtube.com/@channelname" onkeydown="if(event.key==='Enter')addChannel()">
  </div>
  <button class="btn btn-blue" id="add-channel-btn" onclick="addChannel()">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
    Добавить канал
  </button>
  <div id="add-channel-status" style="margin-top:8px;font-size:12px;display:none"></div>
</div>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
    <div class="card-label" style="margin-bottom:0"><span class="card-label-dot" style="background:var(--red)"></span>Отслеживаемые каналы</div>
    <button class="btn btn-ghost" style="font-size:11px;padding:5px 12px" onclick="loadMonitorChannels()">Обновить</button>
  </div>
  <div id="monitor-channel-list"></div>
</div>
</div>

<!-- ═══ SETTINGS ═══ -->
<div class="panel" id="tab-settings">
<div class="card" style="margin-bottom:14px">
  <div class="settings-section" style="margin-bottom:0">
    <h3>Зависимости</h3>
    <div id="deps-list"></div>
  </div>
</div>
<div class="card" style="margin-bottom:14px">
  <div class="settings-section" style="margin-bottom:0">
    <h3>🎬 YouTube — авторизация</h3>
    <div class="toggle-row">
      <label>Использовать куки YouTube</label>
      <label class="toggle"><input type="checkbox" id="yt-use-cookies" onchange="saveSettings();toggleYtSection()"><span class="slider"></span></label>
    </div>
    <div id="yt-cookie-section" style="margin-top:14px;display:none">
      <div style="background:rgba(0,0,0,0.15);border-radius:var(--r2);padding:14px;margin-bottom:12px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">Экспорт из браузера</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <select id="yt-cookies-from-browser" onchange="saveSettings()" style="flex:1">
            <option value="chrome">Chrome</option><option value="firefox">Firefox</option>
            <option value="edge">Edge</option><option value="brave">Brave</option><option value="chromium">Chromium</option>
          </select>
          <button class="btn btn-blue" onclick="exportCookies()" id="export-btn">⬇ Экспортировать</button>
        </div>
        <div id="export-status" style="font-size:12px;margin-top:8px;display:none"></div>
        <div style="font-size:11px;color:var(--text3);margin-top:6px">⚠ Закройте Chrome перед экспортом</div>
      </div>
      <div class="input-group">
        <label>Или вставьте куки вручную (Netscape формат)</label>
        <textarea class="cookie-area" id="yt-cookie-text" placeholder="# Netscape HTTP Cookie File&#10;.youtube.com TRUE / FALSE ..."></textarea>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="text" id="yt-cookie-file" placeholder="yt_cookies.txt" oninput="saveSettings()" style="flex:1">
        <button class="btn btn-blue" onclick="saveCookies('yt')">💾 Сохранить</button>
        <span class="cookie-saved" id="yt-cookie-saved">✓ Сохранено</span>
      </div>
    </div>
  </div>
</div>
<div class="card" style="margin-bottom:14px">
  <div class="settings-section" style="margin-bottom:0">
    <h3>📺 Rutube — авторизация</h3>
    <div id="rutube-login-section">
      <div class="input-group"><label>Логин</label><input type="text" id="rutube-user" placeholder="username" oninput="saveSettings()"></div>
      <div class="input-group"><label>Пароль</label><input type="password" id="rutube-pass" placeholder="••••••••" oninput="saveSettings()"></div>
    </div>
    <div class="toggle-row" style="margin-top:14px">
      <label>Использовать куки Rutube</label>
      <label class="toggle"><input type="checkbox" id="rutube-use-cookies" onchange="saveSettings();toggleRutubeSection()"><span class="slider"></span></label>
    </div>
    <div id="rutube-cookie-section" style="margin-top:14px;display:none">
      <div class="input-group">
        <label>Содержимое rutube_cookies.txt</label>
        <textarea class="cookie-area" id="rutube-cookie-text" placeholder="# Netscape HTTP Cookie File&#10;.rutube.ru TRUE / FALSE ..."></textarea>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="text" id="rutube-cookie-file" placeholder="rutube_cookies.txt" oninput="saveSettings()" style="flex:1">
        <button class="btn btn-blue" onclick="saveCookies('rutube')">💾 Сохранить</button>
        <span class="cookie-saved" id="rutube-cookie-saved">✓ Сохранено</span>
      </div>
    </div>
  </div>
</div>
<div class="card" style="margin-bottom:14px">
  <div class="settings-section" style="margin-bottom:0">
    <h3>🔔 Telegram — уведомления</h3>
    <div class="toggle-row">
      <label>Включить Telegram-уведомления</label>
      <label class="toggle"><input type="checkbox" id="tg-enabled" onchange="saveSettings()"><span class="slider"></span></label>
    </div>
    <div style="margin-top:14px">
      <div class="input-group"><label>Bot Token (от @BotFather)</label><input type="password" id="tg-bot-token" placeholder="123456:ABC..." oninput="saveSettings()"></div>
      <div class="input-group"><label>Chat ID (от @userinfobot)</label><input type="text" id="tg-chat-id" placeholder="-1001234567890" oninput="saveSettings()"></div>
      <button class="btn btn-blue" onclick="testTelegram()" id="tg-test-btn">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
        Тест уведомления
      </button>
      <div id="tg-test-status" style="margin-top:8px;font-size:12px;font-family:var(--mono);display:none"></div>
    </div>
  </div>
</div>
<div class="card">
  <div class="settings-section" style="margin-bottom:0">
    <h3>Загрузка</h3>
    <div class="input-group">
      <label>Качество видео</label>
      <select id="quality" onchange="saveSettings()">
        <option value="best">Лучшее доступное</option>
        <option value="1080p">1080p</option><option value="720p">720p</option><option value="480p">480p</option>
      </select>
    </div>
    <div class="input-group"><label>Прокси (необязательно)</label><input type="text" id="proxy" placeholder="http://host:port" oninput="saveSettings()"></div>
    <div class="toggle-row">
      <label>Хранить скачанные файлы</label>
      <label class="toggle"><input type="checkbox" id="keep-files" onchange="saveSettings()"><span class="slider"></span></label>
    </div>
    <div style="margin-top:18px">
      <button class="btn btn-secondary" onclick="saveSettings(true)">💾 Сохранить настройки</button>
    </div>
  </div>
</div>
</div>

<script>
/* ========== State ========== */
const USER = (typeof SERVER_USER !== 'undefined') ? SERVER_USER : {};
let activeTab = 'transfer';
let currentJobId = null;
let pollTimer = null;
let ws = null;
let wsReconnectTimer = null;
let dashJobs = {};
let monitorChannels = [];

/* ========== Tab switching ========== */
function switchTab(name) {
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  const btn = document.querySelector(`.nav-btn[data-tab="${name}"]`);
  if (btn) btn.classList.add('active');
  const panel = document.getElementById('tab-' + name);
  if (panel) panel.classList.add('active');
  activeTab = name;
  if (name === 'dashboard') { connectWS(); refreshDash(); }
  if (name === 'history') loadHistory();
  if (name === 'monitor') loadMonitor();
  if (name === 'settings') loadSettings();
}

/* ========== URL validation ========== */
function validateUrl(url) {
  return url && (url.includes('youtube.com') || url.includes('youtu.be'));
}

/* ========== Transfer tab ========== */
function startTransfer() {
  const url = document.getElementById('yt-url').value.trim();
  if (!validateUrl(url)) { showAlert('transfer-status', 'Введите корректный YouTube URL', 'error'); return; }
  const dub = document.getElementById('dub-toggle').checked;
  const shorts = document.getElementById('shorts-toggle').checked;
  const ruCookies = document.getElementById('ru-cookies').value.trim();
  showAlert('transfer-status', 'Запуск задачи...', 'info');
  fetch('/api/transfer', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ url, dub, shorts, rutube_cookies: ruCookies })
  }).then(r => r.json()).then(data => {
    if (data.error) { showAlert('transfer-status', data.error, 'error'); return; }
    currentJobId = data.job_id;
    showAlert('transfer-status', 'Задача запущена: ' + data.job_id, 'info');
    showJobPanel(data.job_id);
    pollJob(data.job_id);
  }).catch(e => showAlert('transfer-status', 'Ошибка: ' + e, 'error'));
}

function showJobPanel(jobId) {
  const panel = document.getElementById('job-panel');
  if (panel) { panel.style.display = 'block'; }
  const idEl = document.getElementById('job-id-display');
  if (idEl) idEl.textContent = jobId;
  const log = document.getElementById('job-log');
  if (log) log.innerHTML = '';
}

function pollJob(jobId) {
  if (pollTimer) clearTimeout(pollTimer);
  fetch('/api/job/' + jobId).then(r => r.json()).then(job => {
    if (job.error) return;
    updateJobDisplay(job);
    if (job.status !== 'done' && job.status !== 'error') {
      pollTimer = setTimeout(() => pollJob(jobId), 2000);
    }
  }).catch(() => {
    pollTimer = setTimeout(() => pollJob(jobId), 5000);
  });
}

function updateJobDisplay(job) {
  const statusEl = document.getElementById('job-status-text');
  const progressEl = document.getElementById('job-progress-bar');
  const progressWrap = document.getElementById('job-progress-wrap');
  const logEl = document.getElementById('job-log');
  const resultEl = document.getElementById('job-result');

  if (statusEl) {
    statusEl.textContent = formatStatus(job.status);
    statusEl.className = 'status-badge status-' + (job.status || 'queued');
  }
  if (progressEl && progressWrap) {
    const pct = job.progress || 0;
    progressWrap.style.display = pct > 0 ? 'block' : 'none';
    progressEl.style.width = pct + '%';
    progressEl.textContent = pct + '%';
  }
  if (logEl && job.log) {
    logEl.innerHTML = job.log.map(e =>
      `<div class="log-line log-${e.level||'info'}"><span class="log-time">${e.t}</span>${escHtml(e.msg)}</div>`
    ).join('');
    logEl.scrollTop = logEl.scrollHeight;
  }
  if (resultEl && job.status === 'done' && job.result) {
    const r = job.result;
    let html = '<div class="result-card">';
    if (r.rutube_url) html += `<div><b>Rutube:</b> <a href="${r.rutube_url}" target="_blank">${r.rutube_url}</a></div>`;
    if (r.title) html += `<div><b>Название:</b> ${escHtml(r.title)}</div>`;
    if (r.shorts_urls && r.shorts_urls.length) {
      html += '<div><b>Shorts:</b><ul>' + r.shorts_urls.map(u => `<li><a href="${u}" target="_blank">${u}</a></li>`).join('') + '</ul></div>';
    }
    html += '</div>';
    resultEl.innerHTML = html;
    resultEl.style.display = 'block';
  }
}

/* ========== Dub tab ========== */
function startDub() {
  const url = document.getElementById('dub-url').value.trim();
  if (!validateUrl(url)) { showAlert('dub-status', 'Введите корректный YouTube URL', 'error'); return; }
  const voice = document.getElementById('dub-voice').value;
  const mix = parseFloat(document.getElementById('dub-mix').value) || 0.15;
  const lang = document.getElementById('dub-lang').value;
  showAlert('dub-status', 'Запуск озвучки...', 'info');
  fetch('/api/dub', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ url, voice, original_volume: mix, target_lang: lang })
  }).then(r => r.json()).then(data => {
    if (data.error) { showAlert('dub-status', data.error, 'error'); return; }
    showAlert('dub-status', 'Задача: ' + data.job_id, 'info');
    pollDubJob(data.job_id);
  }).catch(e => showAlert('dub-status', 'Ошибка: ' + e, 'error'));
}

function pollDubJob(jobId) {
  const logEl = document.getElementById('dub-log');
  const interval = setInterval(() => {
    fetch('/api/job/' + jobId).then(r => r.json()).then(job => {
      if (job.error) return;
      if (logEl && job.log) {
        logEl.innerHTML = job.log.map(e =>
          `<div class="log-line log-${e.level||'info'}"><span class="log-time">${e.t}</span>${escHtml(e.msg)}</div>`
        ).join('');
        logEl.scrollTop = logEl.scrollHeight;
      }
      const statusEl = document.getElementById('dub-status');
      if (statusEl) statusEl.textContent = formatStatus(job.status);
      if (job.status === 'done' || job.status === 'error') {
        clearInterval(interval);
        if (job.status === 'done' && job.result) {
          const r = job.result;
          let msg = 'Готово!';
          if (r.rutube_url) msg += ' <a href="' + r.rutube_url + '" target="_blank">Смотреть на Rutube</a>';
          showAlert('dub-status', msg, 'success');
        }
      }
    });
  }, 2000);
}

/* ========== Shorts tab ========== */
function startShorts() {
  const url = document.getElementById('shorts-url').value.trim();
  if (!validateUrl(url)) { showAlert('shorts-status', 'Введите корректный YouTube URL', 'error'); return; }
  const strategy = document.getElementById('shorts-strategy').value;
  const count = parseInt(document.getElementById('shorts-count').value) || 3;
  const duration = parseInt(document.getElementById('shorts-duration').value) || 58;
  showAlert('shorts-status', 'Запуск нарезки...', 'info');
  fetch('/api/shorts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ url, strategy, count, duration })
  }).then(r => r.json()).then(data => {
    if (data.error) { showAlert('shorts-status', data.error, 'error'); return; }
    showAlert('shorts-status', 'Задача: ' + data.job_id, 'info');
    pollShortsJob(data.job_id);
  }).catch(e => showAlert('shorts-status', 'Ошибка: ' + e, 'error'));
}

function pollShortsJob(jobId) {
  const logEl = document.getElementById('shorts-log');
  const resultEl = document.getElementById('shorts-result');
  const interval = setInterval(() => {
    fetch('/api/job/' + jobId).then(r => r.json()).then(job => {
      if (job.error) return;
      if (logEl && job.log) {
        logEl.innerHTML = job.log.map(e =>
          `<div class="log-line log-${e.level||'info'}"><span class="log-time">${e.t}</span>${escHtml(e.msg)}</div>`
        ).join('');
        logEl.scrollTop = logEl.scrollHeight;
      }
      if (job.status === 'done' || job.status === 'error') {
        clearInterval(interval);
        if (job.status === 'done' && job.result && resultEl) {
          const urls = job.result.shorts_urls || [];
          if (urls.length) {
            resultEl.innerHTML = '<b>Готовые Shorts:</b><ul>' + urls.map(u => `<li><a href="${u}" target="_blank">${u}</a></li>`).join('') + '</ul>';
            resultEl.style.display = 'block';
          }
          showAlert('shorts-status', 'Shorts готовы: ' + urls.length + ' шт.', 'success');
        } else if (job.status === 'error') {
          showAlert('shorts-status', 'Ошибка при нарезке', 'error');
        }
      }
    });
  }, 2000);
}

/* ========== Dashboard / WebSocket ========== */
function connectWS() {
  if (ws && ws.readyState < 2) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onmessage = (e) => {
    try { handleWsMessage(JSON.parse(e.data)); } catch(_) {}
  };
  ws.onclose = () => {
    wsReconnectTimer = setTimeout(connectWS, 3000);
  };
  ws.onerror = () => ws.close();
}

function handleWsMessage(msg) {
  if (msg.type === 'job_update') {
    dashJobs[msg.job_id] = msg;
    if (activeTab === 'dashboard') renderDashJobs();
  }
}

function refreshDash() {
  fetch('/api/jobs').then(r => r.json()).then(data => {
    if (data.jobs) {
      dashJobs = {};
      data.jobs.forEach(j => dashJobs[j.id] = j);
      renderDashJobs();
    }
    const statsEl = document.getElementById('dash-queue-info');
    if (statsEl && data.queue) {
      statsEl.textContent = `Очередь: ${data.queue.pending} ожидают, ${data.queue.active} активных`;
    }
  });
}

function renderDashJobs() {
  const el = document.getElementById('dash-jobs');
  if (!el) return;
  const jobs = Object.values(dashJobs);
  if (!jobs.length) { el.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:32px">Нет активных задач</div>'; return; }
  el.innerHTML = jobs.map(j => `
    <div class="job-card" style="border-left:3px solid ${statusColor(j.status)}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <code style="font-size:11px;color:var(--text-muted)">${j.id}</code>
        <span class="status-badge status-${j.status}">${formatStatus(j.status)}</span>
      </div>
      <div style="font-size:13px;margin-bottom:6px">${escHtml(j.title || j.url || '')}</div>
      ${j.progress ? `<div class="progress-wrap" style="display:block"><div class="progress-bar" style="width:${j.progress}%">${j.progress}%</div></div>` : ''}
      ${j.log && j.log.length ? `<div class="log-mini">${escHtml(j.log[j.log.length-1].msg)}</div>` : ''}
    </div>
  `).join('');
}

/* ========== Batch tab ========== */
function addBatchUrl() {
  const input = document.getElementById('batch-url-input');
  if (!input) return;
  const url = input.value.trim();
  if (!url || !validateUrl(url)) return;
  const list = document.getElementById('batch-list');
  if (list) {
    const item = document.createElement('div');
    item.className = 'batch-item';
    item.dataset.url = url;
    item.innerHTML = `<span style="flex:1;font-size:13px">${escHtml(url)}</span><button class="btn btn-sm" onclick="this.parentElement.remove()">✕</button>`;
    list.appendChild(item);
  }
  input.value = '';
}

function startBatch() {
  const items = document.querySelectorAll('#batch-list .batch-item');
  if (!items.length) { showAlert('batch-status', 'Добавьте URLs', 'error'); return; }
  const urls = Array.from(items).map(i => i.dataset.url);
  const dub = document.getElementById('batch-dub').checked;
  const shorts = document.getElementById('batch-shorts').checked;
  showAlert('batch-status', `Запуск ${urls.length} задач...`, 'info');
  const promises = urls.map(url =>
    fetch('/api/transfer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url, dub, shorts })
    }).then(r => r.json())
  );
  Promise.all(promises).then(results => {
    const ok = results.filter(r => !r.error).length;
    const fail = results.length - ok;
    showAlert('batch-status', `Запущено: ${ok}, ошибок: ${fail}`, ok > 0 ? 'success' : 'error');
    document.getElementById('batch-list').innerHTML = '';
    switchTab('dashboard');
  });
}

/* ========== History tab ========== */
let historyPage = 1;
function loadHistory(page) {
  page = page || 1;
  historyPage = page;
  const search = document.getElementById('history-search') ? document.getElementById('history-search').value : '';
  fetch(`/api/history?page=${page}&search=${encodeURIComponent(search)}`).then(r => r.json()).then(data => {
    renderHistory(data.history || []);
    renderHistoryPager(data.total || 0, data.page || 1, data.per_page || 20);
  });
}

function renderHistory(items) {
  const el = document.getElementById('history-table-body');
  if (!el) return;
  if (!items.length) { el.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:24px">История пуста</td></tr>'; return; }
  el.innerHTML = items.map(h => `
    <tr>
      <td style="color:var(--text-muted);font-size:12px">${escHtml(h.ts||'')}</td>
      <td style="font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis">${escHtml(h.title||h.youtube_url||'')}</td>
      <td style="font-size:12px">${escHtml(h.mode||'transfer')}</td>
      <td><span class="status-badge status-${h.status}">${formatStatus(h.status)}</span></td>
      <td>${h.rutube_url ? `<a href="${h.rutube_url}" target="_blank" style="font-size:11px">Rutube ↗</a>` : '—'}</td>
    </tr>
  `).join('');
}

function renderHistoryPager(total, page, perPage) {
  const el = document.getElementById('history-pager');
  if (!el) return;
  const pages = Math.ceil(total / perPage);
  if (pages <= 1) { el.innerHTML = ''; return; }
  let html = '';
  for (let i = 1; i <= pages; i++) {
    html += `<button class="btn btn-sm ${i === page ? 'btn-blue' : 'btn-secondary'}" onclick="loadHistory(${i})">${i}</button> `;
  }
  el.innerHTML = html;
}

function clearHistory() {
  if (!confirm('Очистить всю историю?')) return;
  fetch('/api/history', {method: 'DELETE'}).then(() => loadHistory(1));
}

/* ========== Monitor tab ========== */
function loadMonitor() {
  fetch('/api/monitor').then(r => r.json()).then(data => {
    monitorChannels = data.channels || [];
    renderMonitorChannels();
  });
}

function renderMonitorChannels() {
  const el = document.getElementById('monitor-channels');
  if (!el) return;
  if (!monitorChannels.length) { el.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:24px">Нет отслеживаемых каналов</div>'; return; }
  el.innerHTML = monitorChannels.map((ch, i) => `
    <div class="channel-item" style="display:flex;align-items:center;gap:10px;padding:10px;background:var(--bg-tertiary);border-radius:6px;margin-bottom:8px">
      <div style="flex:1">
        <div style="font-size:13px;font-weight:500">${escHtml(ch.channel_id||ch.url||'')}</div>
        ${ch.last_check ? `<div style="font-size:11px;color:var(--text-muted)">Проверено: ${escHtml(ch.last_check)}</div>` : ''}
      </div>
      <span class="status-badge ${ch.active ? 'status-done' : 'status-error'}">${ch.active ? 'Активен' : 'Стоп'}</span>
      <button class="btn btn-sm" onclick="toggleMonitor(${i})">${ch.active ? 'Стоп' : 'Старт'}</button>
      <button class="btn btn-sm btn-danger" onclick="removeMonitor(${i})">✕</button>
    </div>
  `).join('');
}

function addMonitorChannel() {
  const input = document.getElementById('monitor-channel-input');
  if (!input) return;
  const url = input.value.trim();
  if (!url) return;
  fetch('/api/monitor', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ url })
  }).then(r => r.json()).then(data => {
    if (data.error) { showAlert('monitor-status', data.error, 'error'); return; }
    input.value = '';
    loadMonitor();
  });
}

function toggleMonitor(idx) {
  const ch = monitorChannels[idx];
  if (!ch) return;
  fetch('/api/monitor/' + encodeURIComponent(ch.channel_id || ch.url), {method: 'PATCH'})
    .then(() => loadMonitor());
}

function removeMonitor(idx) {
  const ch = monitorChannels[idx];
  if (!ch) return;
  fetch('/api/monitor/' + encodeURIComponent(ch.channel_id || ch.url), {method: 'DELETE'})
    .then(() => loadMonitor());
}

/* ========== Settings tab ========== */
function loadSettings() {
  fetch('/api/settings').then(r => r.json()).then(cfg => {
    setVal('yt-cookie-text', cfg.yt_cookies || '');
    setVal('yt-cookie-file', cfg.yt_cookie_file || 'yt_cookies.txt');
    setVal('rutube-cookie-text', cfg.rutube_cookies || '');
    setVal('rutube-cookie-file', cfg.rutube_cookie_file || 'rutube_cookies.txt');
    setVal('tg-bot-token', cfg.tg_token || '');
    setVal('tg-chat-id', cfg.tg_chat_id || '');
    setCheck('tg-enabled', !!cfg.tg_enabled);
    setVal('quality', cfg.quality || 'best');
    setVal('proxy', cfg.proxy || '');
    setCheck('keep-files', !!cfg.keep_files);
  });
}

function saveSettings(showFeedback) {
  const cfg = {
    yt_cookies: getVal('yt-cookie-text'),
    yt_cookie_file: getVal('yt-cookie-file'),
    rutube_cookies: getVal('rutube-cookie-text'),
    rutube_cookie_file: getVal('rutube-cookie-file'),
    tg_token: getVal('tg-bot-token'),
    tg_chat_id: getVal('tg-chat-id'),
    tg_enabled: getCheck('tg-enabled'),
    quality: getVal('quality'),
    proxy: getVal('proxy'),
    keep_files: getCheck('keep-files')
  };
  fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(cfg)
  }).then(r => r.json()).then(data => {
    if (showFeedback) showAlert('settings-status', 'Настройки сохранены', 'success');
  });
}

function saveCookies(type) {
  const textId = type + '-cookie-text';
  const savedId = type + '-cookie-saved';
  const text = document.getElementById(textId) ? document.getElementById(textId).value : '';
  const key = type === 'yt' ? 'yt_cookies' : 'rutube_cookies';
  fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ [key]: text })
  }).then(() => {
    const el = document.getElementById(savedId);
    if (el) { el.style.display = 'inline'; setTimeout(() => el.style.display = 'none', 2000); }
  });
}

function testTelegram() {
  saveSettings(false);
  const btn = document.getElementById('tg-test-btn');
  const statusEl = document.getElementById('tg-test-status');
  if (btn) btn.disabled = true;
  fetch('/api/telegram/test', {method: 'POST'}).then(r => r.json()).then(data => {
    if (statusEl) {
      statusEl.style.display = 'block';
      statusEl.style.color = data.ok ? 'var(--accent-green)' : 'var(--accent-red)';
      statusEl.textContent = data.ok ? '✓ Уведомление отправлено' : '✗ Ошибка: ' + (data.error || 'неизвестно');
    }
    if (btn) btn.disabled = false;
  }).catch(() => { if (btn) btn.disabled = false; });
}

/* ========== Helpers ========== */
function showAlert(containerId, msg, type) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `<div class="alert alert-${type}">${msg}</div>`;
}

function formatStatus(s) {
  const map = { queued:'В очереди', running:'Выполняется', done:'Готово', error:'Ошибка', paused:'Пауза' };
  return map[s] || (s || 'Неизвестно');
}

function statusColor(s) {
  const map = { queued:'var(--text-muted)', running:'var(--accent-blue)', done:'var(--accent-green)', error:'var(--accent-red)' };
  return map[s] || 'var(--text-muted)';
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function setVal(id, val) { const el = document.getElementById(id); if (el) el.value = val; }
function getVal(id) { const el = document.getElementById(id); return el ? el.value : ''; }
function setCheck(id, v) { const el = document.getElementById(id); if (el) el.checked = !!v; }
function getCheck(id) { const el = document.getElementById(id); return el ? el.checked : false; }

/* ========== Init ========== */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  switchTab('transfer');

  // Range sliders
  const mixRange = document.getElementById('dub-mix');
  const mixVal = document.getElementById('dub-mix-val');
  if (mixRange && mixVal) {
    mixRange.addEventListener('input', () => mixVal.textContent = mixRange.value);
  }
  const durRange = document.getElementById('shorts-duration');
  const durVal = document.getElementById('shorts-duration-val');
  if (durRange && durVal) {
    durRange.addEventListener('input', () => durVal.textContent = durRange.value + 'с');
  }

  // Batch URL input on Enter
  const batchInput = document.getElementById('batch-url-input');
  if (batchInput) batchInput.addEventListener('keydown', e => { if (e.key === 'Enter') addBatchUrl(); });

  // Monitor input on Enter
  const monInput = document.getElementById('monitor-channel-input');
  if (monInput) monInput.addEventListener('keydown', e => { if (e.key === 'Enter') addMonitorChannel(); });

  // History search debounce
  const hsearch = document.getElementById('history-search');
  if (hsearch) {
    let hTimer;
    hsearch.addEventListener('input', () => { clearTimeout(hTimer); hTimer = setTimeout(() => loadHistory(1), 400); });
  }
});
</script>
</body>
</html>

"""


# ─── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("  YT → Rutube Transfer Platform v4.0")
    print("  http://0.0.0.0:5000")
    print("  Default admin: admin / admin123")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)
