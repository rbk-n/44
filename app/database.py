import json
import sqlite3
from datetime import datetime
from pathlib import Path
from werkzeug.security import generate_password_hash

try:
    from config import Config
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from config import Config


def get_db():
    conn = sqlite3.connect(str(Config.DB_PATH))
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
    for _d in [Config.DOWNLOAD_DIR, Config.SHORTS_DIR, Config.DUBBED_DIR]:
        Path(_d).mkdir(exist_ok=True)
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
            print("  [init] Admin created: admin / admin123  <- change this!")


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


def save_history(user_id: int, entry: dict) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO job_history (user_id,ts,youtube_url,title,rutube_url,status,mode)"
            " VALUES(?,?,?,?,?,?,?)",
            (user_id, entry.get("ts", ""), entry.get("youtube_url", ""),
             entry.get("title", ""), entry.get("rutube_url", ""),
             entry.get("status", ""), entry.get("mode", "transfer"))
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
