import re
import sys
import subprocess
from pathlib import Path


YT_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/(watch\?.*v=|shorts/|live/|embed/)"
    r"|youtu\.be/|m\.youtube\.com/)"
)


def is_valid_yt_url(url: str) -> bool:
    return bool(YT_URL_RE.match(url))


def safe_filename(name: str, default: str = "file.txt") -> str:
    name = Path(name.strip()).name
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return default
    return name


def ytdlp_available() -> bool:
    for cmd in [["yt-dlp", "--version"], [sys.executable, "-m", "yt_dlp", "--version"]]:
        try:
            if subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    return False


def ytdlp_cmd() -> list:
    try:
        if subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=5).returncode == 0:
            return ["yt-dlp"]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return [sys.executable, "-m", "yt_dlp"]


def ffmpeg_available() -> bool:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5).returncode == 0
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
