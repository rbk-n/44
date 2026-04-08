import threading
import time
import uuid
from collections import deque


jobs: dict = {}
MAX_CONCURRENT = 2
active_count = 0
active_lock = threading.Lock()
job_queue: deque = deque()


def _run_and_release(job_id, url, cfg, user_id):
    global active_count
    from app.core.transfer import run_transfer
    try:
        run_transfer(job_id, url, cfg, user_id)
    finally:
        with active_lock:
            active_count -= 1


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


def start_queue_worker():
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
    from app.core.dubbing import run_dub_transfer
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
    from app.core.shorts import run_shorts_transfer
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "user_id": user_id, "status": "queued", "progress": 0,
        "log": [], "meta": {}, "stage": "queued", "mode": "shorts", "shorts": []
    }
    threading.Thread(
        target=run_shorts_transfer, args=(job_id, url, cfg, user_id), daemon=True
    ).start()
    return job_id
