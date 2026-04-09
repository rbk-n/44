"""
AI Dubbing: Whisper transcription + Google Translate + Edge-TTS synthesis.
"""
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import Config
from app.core.helpers import ytdlp_cmd, ytdlp_auth_args, ffmpeg_available
from app.core.queue import jobs
from app.database import save_history
from app.services.telegram import _send_telegram_notify

DUBBED_DIR = Config.DUBBED_DIR


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
            "mode": job.get("mode", "dub")
        })
    return log


def run_ai_dubbing(video_path: Path, cfg: dict, log_fn) -> Path | None:
    source_lang = cfg.get("dub_source_lang", "en")
    target_lang = cfg.get("dub_target_lang", "ru")
    mix_ratio = float(cfg.get("dub_mix_original", 0.15))
    whisper_model = cfg.get("dub_whisper_model", "base")
    vid_id = video_path.stem
    log_fn(f"Dubbing: {source_lang} -> {target_lang}, model={whisper_model}", "info")

    audio_path = DUBBED_DIR / f"{vid_id}_audio.wav"
    log_fn("Extracting audio...", "info")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "pcm_s16le",
         "-ar", "16000", "-ac", "1", str(audio_path)],
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
            [sys.executable, "-m", "pip", "install", "openai-whisper",
             "--break-system-packages", "-q"], timeout=600
        )
        import whisper as wh
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    log_fn(f"Device: {device.upper()}", "info")
    model = wh.load_model(whisper_model, device=device)
    result = model.transcribe(str(audio_path), language=source_lang,
                               verbose=False, fp16=(device == "cuda"))
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
            [sys.executable, "-m", "pip", "install", "deep-translator",
             "--break-system-packages", "-q"], timeout=120
        )
        from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source=source_lang, target=target_lang)
    translated = []
    for i in range(0, len(segments), 8):
        batch = segments[i:i + 8]
        texts = [s["text"].strip() for s in batch if s["text"].strip()]
        if not texts:
            continue
        try:
            tr = translator.translate(" ||| ".join(texts))
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
        subprocess.run([sys.executable, "-m", "edge_tts", "--version"],
                       capture_output=True, timeout=10)
    except Exception:
        subprocess.run([sys.executable, "-m", "pip", "install", "edge-tts",
                        "--break-system-packages", "-q"], timeout=120)
    voice_map = {
        "ru": "ru-RU-DmitryNeural", "en": "en-US-GuyNeural",
        "es": "es-ES-AlvaroNeural", "de": "de-DE-ConradNeural",
        "fr": "fr-FR-HenriNeural", "zh": "zh-CN-YunxiNeural",
        "ja": "ja-JP-KeitaNeural", "ko": "ko-KR-InJoonNeural"
    }
    voice = voice_map.get(target_lang, "ru-RU-DmitryNeural")
    tts_dir = DUBBED_DIR / f"{vid_id}_tts"
    tts_dir.mkdir(exist_ok=True)
    tts_segs = []
    for i, seg in enumerate(translated):
        out_f = tts_dir / f"seg{i:04d}.mp3"
        if not seg["text"].strip():
            continue
        try:
            subprocess.run(
                [sys.executable, "-m", "edge_tts", "--voice", voice,
                 "--text", seg["text"], "--write-media", str(out_f)],
                capture_output=True, timeout=30
            )
            if out_f.exists() and out_f.stat().st_size > 100:
                tts_segs.append({"file": str(out_f), "start": seg["start"]})
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
        ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "aac", str(orig_aac)],
        capture_output=True, timeout=120
    )
    fi = ["-i", str(orig_aac)]
    for ts in tts_segs:
        fi += ["-i", ts["file"]]
    fp = [f"[0:a]volume={mix_ratio}[bg]"]
    ll = []
    for i, ts in enumerate(tts_segs):
        ms = int(ts["start"] * 1000)
        fp.append(f"[{i + 1}:a]adelay={ms}|{ms},volume=1.0[t{i}]")
        ll.append(f"[t{i}]")
    fp.append(
        f"[bg]{''.join(ll)}amix=inputs={len(tts_segs) + 1}"
        f":duration=first:dropout_transition=2[out]"
    )
    mixed = DUBBED_DIR / f"{vid_id}_mixed.aac"
    r = subprocess.run(
        ["ffmpeg", "-y"] + fi + ["-filter_complex", ";".join(fp),
                                  "-map", "[out]", "-acodec", "aac", "-b:a", "192k", str(mixed)],
        capture_output=True, timeout=600
    )
    if r.returncode != 0 or not mixed.exists():
        log_fn("Audio mix failed", "warn")
        return None
    log_fn("Audio mixed", "ok")

    log_fn("Merging video...", "info")
    dubbed = DUBBED_DIR / f"{vid_id}_dubbed.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-i", str(mixed),
         "-c:v", "copy", "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(dubbed)],
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
    log_fn(f"Dubbed: {dubbed.name} ({dubbed.stat().st_size / 1024 / 1024:.1f} MB)", "ok")
    return dubbed


def run_dub_transfer(job_id: str, url: str, cfg: dict, user_id: int) -> None:
    from app.core.transfer import _download_video
    job = jobs[job_id]
    job["status"] = "running"
    job["log"] = []
    job["mode"] = "dub"
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
            "title":       meta.get("title", "")[:80],
            "channel":     meta.get("uploader", ""),
            "duration":    meta.get("duration", 0),
            "tags":        meta.get("tags", [])[:20],
            "description": meta.get("description", "")[:4500],
            "thumbnail":   meta.get("thumbnail", ""),
        }
        vid_id = meta.get("id", "video")
        log(f"Video: {job['meta']['title']}", "ok")

        video_path = _download_video(job, log, url, cfg, vid_id, include_thumb=False)
        job["progress"] = 42
        job["stage"] = "dubbing"
        log("Starting AI dubbing...")
        dubbed = run_ai_dubbing(video_path, cfg, log)
        job["dubbed_file"] = str(dubbed) if dubbed and dubbed.exists() else str(video_path)
        if dubbed and dubbed.exists():
            log("Dubbing complete!", "ok")
        else:
            log("Dubbing failed — original preserved", "warn")

        job["progress"] = 100
        job["status"] = "done"
        log("Done! File ready for download.", "ok")
        _ws_broadcast({"type": "job_done", "job_id": job_id, "user_id": user_id, "mode": "dub"})
        save_history(user_id, {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url": url, "title": "[DUB] " + job["meta"]["title"],
            "rutube_url": "dubbed", "status": "success", "mode": "dub"
        })
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        log(f"Error: {e}", "error")
        _ws_broadcast({"type": "job_error", "job_id": job_id, "user_id": user_id, "error": str(e)})
        save_history(user_id, {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "youtube_url": url, "title": "", "rutube_url": "",
            "status": f"error: {e}", "mode": "dub"
        })
    finally:
        _send_telegram_notify(cfg, job, url)


def check_whisper() -> bool:
    try:
        import whisper
        return True
    except ImportError:
        return False


def check_edge_tts() -> bool:
    import subprocess, sys
    try:
        return subprocess.run(
            [sys.executable, '-m', 'edge_tts', '--list-voices'],
            capture_output=True, timeout=10
        ).returncode == 0
    except Exception:
        return False
