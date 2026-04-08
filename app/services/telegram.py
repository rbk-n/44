import json
import urllib.request
import urllib.error
from datetime import datetime


def tg_send(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> dict | None:
    try:
        payload = json.dumps({
            "chat_id": chat_id, "text": text,
            "parse_mode": parse_mode, "disable_web_page_preview": False
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"}, method="POST"
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
            title = job.get("meta", {}).get("title", "\u2014")
            rutube_url = job.get("rutube_url", "")
            text = (f"\u2705 <b>\u041f\u0435\u0440\u0435\u043d\u043e\u0441 \u0437\u0430\u0432\u0435\u0440\u0448\u0451\u043d</b>\n\n"
                    f"\ud83d\udcf9 <b>{title}</b>\n\u25b6\ufe0f {url}\n\ud83d\udcfa {rutube_url}\n"
                    f"\ud83d\udd50 {datetime.now().strftime('%H:%M:%S')}")
        else:
            text = (f"\u274c <b>\u041e\u0448\u0438\u0431\u043a\u0430 \u043f\u0435\u0440\u0435\u043d\u043e\u0441\u0430</b>\n\n"
                    f"\u25b6\ufe0f {url}\n\ud83d\udca5 {job.get('error', '?')[:300]}\n"
                    f"\ud83d\udd50 {datetime.now().strftime('%H:%M:%S')}")
        tg_send(token, chat_id, text)
    except Exception:
        pass
