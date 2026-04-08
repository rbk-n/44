import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
    PERMANENT_SESSION_LIFETIME = 86400 * 30  # 30 days

    DB_PATH = BASE_DIR / "platform.db"
    MONITOR_STATE_FILE = BASE_DIR / "monitor_state.json"

    DOWNLOAD_DIR = BASE_DIR / "downloads"
    SHORTS_DIR   = BASE_DIR / "shorts"
    DUBBED_DIR   = BASE_DIR / "dubbed"

    MAX_CONCURRENT = 2
